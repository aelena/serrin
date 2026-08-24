"""The pedal catalog (v0.1 -- section 3.3 of the design doc).

Nine pedals, each one small and dumb on purpose. The interesting behaviour is
supposed to come out of ordering and combination, not out of any individual
pedal being clever.

Every pedal in here obeys three rules:
  1. pure: ``(stream, params, rng) -> stream``, no clock, no I/O;
  2. bit-depth honest: output is masked back into the stream's bit depth, so a
     Caesar shift on an 8-bit stream wraps at 256 and not at 2**63;
  3. length-preserving, with exactly one exception (``stutter_repeat``, whose
     whole point is to make the stream stall and grow).
"""

from __future__ import annotations

import math

from ..rng import Lfsr, Rng
from ..scales import degree_to_semitone, resolve
from ..stream import Stream
from .base import (  # noqa: F401  (re-exported)
    REGISTRY,
    Lfo,
    Pedal,
    PedalError,
    catalog,
    get,
    parse_lfo,
    register,
)


# ---------------------------------------------------------------------------
# caesar -- cyclic shift, optionally moving
# ---------------------------------------------------------------------------
@register(
    "caesar",
    {"shift": 5, "shift_lfo": None, "channels": None},
    "Cyclic shift by N. N itself can oscillate (a discrete LFO over the shift).",
)
def caesar(stream: Stream, p: dict, rng: Rng) -> Stream:
    lfo = Lfo(p["shift_lfo"], stream, rng.split("caesar"))
    span = stream.ceiling
    base = int(p["shift"])
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]
    for c in targets:
        source = stream.data[c]
        column = out[c]
        for i, value in enumerate(source):
            # The LFO swings the shift across the full value range, so a slow
            # sine on an 8-bit stream sweeps the whole byte space and back.
            shift = base + int(round(lfo.at(i) * span)) if lfo.active else base
            column[i] = (value + shift) % span
    return stream.with_data(out)


# ---------------------------------------------------------------------------
# xor_mask -- the distortion pedal
# ---------------------------------------------------------------------------
@register(
    "xor_mask",
    {
        "mask_source": "const",
        "mask": 0xA5,
        "column": 0,
        "taps": [3, 1],
        "channels": None,
    },
    "XOR against a constant, another column, or an LFSR. Breaks structure.",
)
def xor_mask(stream: Stream, p: dict, rng: Rng) -> Stream:
    source = str(p["mask_source"]).lower()
    if source not in ("const", "column", "lfsr"):
        raise PedalError(
            f"xor_mask.mask_source must be const|column|lfsr, got {source!r}"
        )

    mask_bits = stream.mask
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]

    if source == "const":
        constant = int(p["mask"]) & mask_bits
        for c in targets:
            out[c] = [v ^ constant for v in stream.data[c]]
        return stream.with_data(out)

    if source == "column":
        driver = stream.channel(p["column"])
        keys = stream.data[driver]
        for c in targets:
            if c == driver:
                continue  # x ^ x == 0; silencing the driver is never the intent
            out[c] = [v ^ (keys[i] & mask_bits) for i, v in enumerate(stream.data[c])]
        return stream.with_data(out)

    # lfsr -- one generator per channel, each seeded off the pedal's substream.
    #
    # Those seeds matter more than they look. A *primitive* polynomial has one
    # cycle through all 2**n - 1 nonzero states, so every channel runs the same
    # length and differs only in phase. A non-primitive one partitions the state
    # space into several cycles of *different* lengths -- so with the document's
    # [3, 1] taps one channel may buzz every 42 frames and its neighbour every
    # 21. That is a feature (voices drift in and out of alignment) but it means
    # "the period" is per-channel, and the pattern as a whole only repeats at the
    # least common multiple of them.
    sub = rng.split("xor_mask/lfsr")
    requested = list(p["taps"]) if p["taps"] else None  # null means "use the default"
    periods: list[int] = []
    taps: list[int] = []
    maximal = True
    for c in targets:
        lfsr = _seed_lfsr(sub, requested, stream.bit_depth)
        out[c] = [v ^ lfsr.next() for v in stream.data[c]]
        periods.append(lfsr.period())
        maximal = maximal and lfsr.is_maximal
        taps = lfsr.taps

    combined = math.lcm(*periods) if periods else 0
    result = stream.with_data(out)
    # The period is audible -- it is the loop length the mask repeats on -- so it
    # is recorded rather than left for the author to infer by listening.
    result.meta = dict(stream.meta)
    result.meta["lfsr"] = {
        "taps": taps,
        "periods": periods,
        "period_frames": combined,
        "period_seconds": round(combined / stream.rate, 3) if stream.rate else None,
        "maximal": maximal,
    }
    return result


# ---------------------------------------------------------------------------
# delta -- state becomes transition
# ---------------------------------------------------------------------------
@register(
    "delta",
    {"order": 1, "signed": False, "channels": None},
    "Difference from the previous value. Silence = stability, noise = change.",
)
def delta(stream: Stream, p: dict, rng: Rng) -> Stream:
    order = max(0, int(p["order"]))
    signed = bool(p["signed"])
    span = stream.ceiling
    half = span // 2
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]
    for c in targets:
        column = list(stream.data[c])
        for _ in range(order):
            previous = column[0]
            differenced = [0]
            for value in column[1:]:
                diff = value - previous
                previous = value
                # Unsigned: wrap into the byte range (a -3 reads as 253, loud).
                # Signed: centre on mid-scale, so "no change" sits at 128 and
                # the result stays readable as a waveform.
                differenced.append(diff % span if not signed else (diff + half) % span)
            column = differenced
        out[c] = column
    return stream.with_data(out)


# ---------------------------------------------------------------------------
# mod_reduce -- the bridge to musical scales
# ---------------------------------------------------------------------------
@register(
    "mod_reduce",
    {"modulus": 0, "scale": None, "octaves": 3, "channels": None},
    "Reduce into a range by modulo; with a scale, into scale degrees.",
)
def mod_reduce(stream: Stream, p: dict, rng: Rng) -> Stream:
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]

    if p["scale"] in (None, "", False):
        modulus = int(p["modulus"]) or stream.ceiling
        if modulus <= 1:
            raise PedalError("mod_reduce.modulus must be > 1")
        for c in targets:
            out[c] = [v % modulus for v in stream.data[c]]
        return stream.with_data(out)

    # Scale mode: the value picks a *degree*, and the degree resolves to a
    # semitone. Values are not "notes mod 12" -- that is the dodecaphonic
    # mush the doc warns about; they are indices into the chosen scale.
    offsets, span = resolve(p["scale"])
    octaves = max(1, int(p["octaves"]))
    degrees = len(offsets) * octaves
    for c in targets:
        out[c] = [
            degree_to_semitone(v % degrees, offsets, span) & stream.mask
            for v in stream.data[c]
        ]
    result = stream.with_data(out)
    result.meta = dict(stream.meta)
    result.meta["quantized_scale"] = {
        "scale": p["scale"],
        "offsets": offsets,
        "span": span,
        "octaves": octaves,
    }
    return result


# ---------------------------------------------------------------------------
# bit_reverse -- cheap, effective glitch
# ---------------------------------------------------------------------------
@register(
    "bit_reverse",
    {"channels": None},
    "Reverse the bit order of every value. Small changes become huge ones.",
)
def bit_reverse(stream: Stream, p: dict, rng: Rng) -> Stream:
    width = stream.bit_depth
    lookup = [_reverse_bits(v, width) for v in range(stream.ceiling)]
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]
    for c in targets:
        out[c] = [lookup[v & stream.mask] for v in stream.data[c]]
    return stream.with_data(out)


def _reverse_bits(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


# ---------------------------------------------------------------------------
# interleave -- cross two voices
# ---------------------------------------------------------------------------
@register(
    "interleave",
    {"a": 0, "b": 1, "stride": 1, "into": None},
    "Alternate blocks of `stride` values from two channels.",
)
def interleave(stream: Stream, p: dict, rng: Rng) -> Stream:
    a = stream.channel(p["a"])
    b = stream.channel(p["b"])
    stride = max(1, int(p["stride"]))
    target = stream.channel(p["into"]) if p["into"] is not None else a
    left, right = stream.data[a], stream.data[b]
    woven = [
        (left if (i // stride) % 2 == 0 else right)[i] for i in range(stream.length)
    ]
    out = [list(ch) for ch in stream.data]
    out[target] = woven
    return stream.with_data(out)


# ---------------------------------------------------------------------------
# cross_mix -- one column drives another
# ---------------------------------------------------------------------------
@register(
    "cross_mix",
    {"driver_column": 0, "op": "xor", "targets": None, "depth": 1.0},
    "Use one column as a live seed perturbing the others. The most alive pedal.",
)
def cross_mix(stream: Stream, p: dict, rng: Rng) -> Stream:
    driver = stream.channel(p["driver_column"])
    op = str(p["op"]).lower()
    depth = max(0.0, min(1.0, float(p["depth"])))
    span = stream.ceiling
    keys = stream.data[driver]

    if p["targets"] is None:
        targets = [c for c in range(stream.n_voices) if c != driver]
    else:
        targets = [stream.channel(t) for t in p["targets"] if stream.channel(t) != driver]

    ops = {
        "xor": lambda v, k: v ^ k,
        "add": lambda v, k: (v + k) % span,
        "sub": lambda v, k: (v - k) % span,
        "mod": lambda v, k: v % k if k > 1 else v,
        "mul": lambda v, k: (v * (k or 1)) % span,
        "min": min,
        "max": max,
    }
    if op not in ops:
        raise PedalError(f"cross_mix.op must be one of {sorted(ops)}, got {op!r}")
    apply = ops[op]

    out = [list(ch) for ch in stream.data]
    for c in targets:
        column = stream.data[c]
        if depth >= 1.0:
            out[c] = [apply(v, keys[i]) & stream.mask for i, v in enumerate(column)]
        else:
            # Partial depth = crossfade between dry and wet, so cross_mix can be
            # dialled in by the intensity envelope instead of being on/off.
            out[c] = [
                int(round(v * (1.0 - depth) + (apply(v, keys[i]) & stream.mask) * depth))
                & stream.mask
                for i, v in enumerate(column)
            ]
    return stream.with_data(out)


# ---------------------------------------------------------------------------
# bitcrush -- Amiga / lo-fi
# ---------------------------------------------------------------------------
@register(
    "bitcrush",
    {"target_bits": 4, "channels": None, "bits_lfo": None},
    "Throw away low bits. 8 -> 4 -> 2. Intentional corruption.",
)
def bitcrush(stream: Stream, p: dict, rng: Rng) -> Stream:
    width = stream.bit_depth
    base = int(p["target_bits"])
    if not 1 <= base <= width:
        raise PedalError(f"bitcrush.target_bits must be 1..{width}, got {base}")
    lfo = Lfo(p["bits_lfo"], stream, rng.split("bitcrush"))
    targets = _targets(stream, p["channels"])
    out = [list(ch) for ch in stream.data]
    for c in targets:
        column = out[c]
        for i, value in enumerate(stream.data[c]):
            bits = base
            if lfo.active:
                bits = max(1, min(width, base + int(round(lfo.at(i) * (width - base)))))
            drop = width - bits
            # Shift down then back up: keeps the value in the original range but
            # snapped to a coarse grid, which is what makes it sound stepped.
            column[i] = (value >> drop) << drop
    return stream.with_data(out)


# ---------------------------------------------------------------------------
# stutter_repeat -- stuck data
# ---------------------------------------------------------------------------
@register(
    "stutter_repeat",
    {
        "threshold": 4,
        "repeats": 3,
        "block": 8,
        "detect_channel": 0,
        "max_growth": 2.0,
    },
    "When the data goes flat, repeat the block. The data's boredom becomes tension.",
)
def stutter_repeat(stream: Stream, p: dict, rng: Rng) -> Stream:
    """The one pedal that changes stream length.

    Flatness is peak-to-peak spread inside a sliding window on one detection
    channel. Below ``threshold``, that window is emitted ``repeats`` times -- on
    *every* channel, so the voices stay frame-aligned.

    The window slides rather than stepping block-by-block, which matters more
    than it sounds: a stuck collector does not politely begin its flatline on a
    multiple of eight, and an aligned scan walks straight past a flat run that
    straddles two blocks.
    """
    block = max(1, int(p["block"]))
    repeats = max(1, int(p["repeats"]))
    threshold = int(p["threshold"])
    detect = stream.channel(p["detect_channel"])
    ceiling = int(stream.length * float(p["max_growth"]))

    probe = stream.data[detect]
    out: list[list[int]] = [[] for _ in stream.data]
    written = 0
    cursor = 0
    while cursor < stream.length and written < ceiling:
        window = probe[cursor : cursor + block]
        if len(window) == block and (max(window) - min(window)) <= threshold:
            for _ in range(repeats):
                if written >= ceiling:
                    break
                for c, channel in enumerate(stream.data):
                    out[c].extend(channel[cursor : cursor + block])
                written += block
            cursor += block
        else:
            # Not flat here: pass one frame through and re-test one frame along.
            for c, channel in enumerate(stream.data):
                out[c].append(channel[cursor])
            written += 1
            cursor += 1

    # Growth cap can leave the last block ragged; trim every channel to the
    # shortest so the invariant "all channels equal length" survives.
    shortest = min(len(ch) for ch in out) if out else 0
    return stream.with_data([ch[:shortest] for ch in out])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
#: A channel whose LFSR returns to its seed this quickly is not producing noise.
#: Period 1 is a fixed point -- the register emits one value forever, which turns
#: `mask_source: lfsr` into `mask_source: const` without saying so.
MIN_LFSR_PERIOD = 4
_LFSR_SEED_ATTEMPTS = 8


def _seed_lfsr(sub, taps, width: int) -> Lfsr:
    """Seed an LFSR onto a cycle long enough to be worth hearing.

    Non-primitive tap sets partition the state space into cycles of different
    lengths, and some of those cycles are tiny: the design document's ``[3, 1]``
    on 8 bits has cycles of length 1, 2, 21 and 42. A channel unlucky enough to
    be seeded onto the length-1 cycle XORs against a constant for the whole
    piece -- the same silent-degradation failure the tap fix was about, one level
    up.

    So candidate seeds are drawn until one lands on a real cycle. Deterministic:
    the draws come from the pedal's own substream, so the same seed retries the
    same way and the render still reproduces exactly.
    """
    candidate = None
    for _ in range(_LFSR_SEED_ATTEMPTS):
        candidate = Lfsr(sub.next_u64(), taps, width=width)
        # A bounded probe -- "does it return within MIN_LFSR_PERIOD steps" -- not
        # the full period, which would cost 2**width per attempt at 16 bits.
        if candidate.period(limit=MIN_LFSR_PERIOD) < 0:
            return candidate
    return candidate  # every attempt was degenerate; the taps leave no choice


def _targets(stream: Stream, channels) -> list[int]:
    """``None`` means every channel; otherwise resolve names/indices."""
    if channels is None:
        return list(range(stream.n_voices))
    if isinstance(channels, (int, str)):
        channels = [channels]
    return [stream.channel(c) for c in channels]


__all__ = [
    "REGISTRY",
    "Lfo",
    "Pedal",
    "PedalError",
    "catalog",
    "get",
    "parse_lfo",
    "register",
]
