"""Forked export: the same transformed stream read two different ways.

Section 3.5's warning is the design constraint for this whole module: audio and
visual must not end up as "the same number disguised twice". Sharing a pedal
chain is the point; sharing a *mapping* would make the visuals a redundant
readout of the audio.

So the fork is explicit, and it forks on three axes:

  * **which derivative of the signal is read** -- audio reads absolute value
    (state), visuals read delta and local spread (change and turbulence), per
    section 5's mapping table;
  * **channel assignment** -- the visual side rotates which channel drives which
    parameter, so voice 0 is not simultaneously the leftmost bar and the lowest
    note;
  * **time behaviour** -- audio gates on stability (a held value sustains one
    note), visuals gate on change (a held value goes still and starts to band).

Output format is columnar: one array per parameter per voice, rather than an
array of per-frame objects. Roughly a third of the bytes, and it is what the JS
reader wants anyway -- it indexes ``voice.freq[i]`` and never allocates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import Envelope, voice_order
from .ingest import fingerprint
from .scales import midi_to_hz, quantize_semitone, resolve
from .stream import Stream

#: ASCII vocabulary for the visual side, ordered light -> heavy. Byte value maps
#: to a glyph by density, so a spike is literally a denser character.
GLYPHS = " .`'\",:;!i|+*=#%8&@$MW"

FORMAT_VERSION = "0.1"


@dataclass
class MappingConfig:
    """The subjective layer (section 5). Every number here is arguable."""

    # -- audio ----------------------------------------------------------------
    #: Lowest and highest MIDI note the value range maps onto. ~2.5 octaves by
    #: default: wide enough to hear contour, narrow enough not to sound random.
    note_low: int = 33
    note_high: int = 72
    #: "direct or logarithmic mapping" (section 5). Log spends more range on the
    #: low end, which reads as more musical on bursty data.
    freq_curve: str = "log"
    #: Snap to a scale on the way out. The pipeline's mod_reduce may have done
    #: this already; this is the second chance, off by default.
    quantize_to: str | None = None
    #: Note length in frames, and how much the value stretches it.
    dur_base: float = 0.9
    dur_spread: float = 0.6
    #: Amplitude floor, so a quiet voice is quiet rather than absent.
    amp_floor: float = 0.12
    #: A voice re-triggers only when it moves by at least this much (8-bit units).
    #: Section 5: "detected repetition/flatness -> silence or stutter".
    gate_threshold: int = 3
    #: Per-voice oscillator assignment, cycled. Primitive waveforms only.
    waveforms: tuple = ("sawtooth", "square", "triangle", "sine")

    # -- visual ---------------------------------------------------------------
    #: How many channels the visual side rotates by, relative to audio.
    channel_rotation: int = 3
    #: Weight of delta vs. absolute value in visual density. 1.0 = pure change.
    density_from_delta: float = 0.75
    #: Window for the local-spread ("turbulence") term.
    turbulence_window: int = 6
    #: Duotone by default (section 4.4: monochrome/duotone over complex colour).
    hue_base: float = 0.0
    hue_spread: float = 0.08
    #: Above this normalized spike level, a frame is flagged as a glitch event.
    glitch_threshold: float = 0.82

    def to_json(self) -> dict:
        return {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in self.__dict__.items()
        }

    @staticmethod
    def from_json(raw: dict | None) -> "MappingConfig":
        config = MappingConfig()
        for key, value in (raw or {}).items():
            if not hasattr(config, key):
                raise ValueError(f"unknown mapping key {key!r}")
            setattr(config, key, tuple(value) if isinstance(value, list) and key == "waveforms" else value)
        return config


# ---------------------------------------------------------------------------
# small signal helpers
# ---------------------------------------------------------------------------
def _norm(values: list[int], ceiling: int) -> list[float]:
    top = ceiling - 1 or 1
    return [v / top for v in values]


def _deltas(values: list[int], ceiling: int) -> list[float]:
    """Normalized absolute change, wrapped short-way-round.

    Wrapping matters because the pedals work modulo the bit depth: 255 -> 0 is a
    one-step move in the data, not a full-scale jump, and treating it as a jump
    would put a glitch on every wrap.
    """
    if not values:
        return []
    half = ceiling // 2
    out = [0.0]
    for previous, current in zip(values, values[1:]):
        raw = abs(current - previous)
        out.append(min(raw, ceiling - raw) / half)
    return out


def _turbulence(values: list[int], window: int, ceiling: int) -> list[float]:
    """Local peak-to-peak spread, normalized. The "is this stretch busy" signal."""
    if window <= 1 or not values:
        return [0.0] * len(values)
    top = ceiling - 1 or 1
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1) : i + 1]
        out.append((max(chunk) - min(chunk)) / top)
    return out


def _flatness(values: list[int], window: int) -> list[float]:
    """1.0 where the data is perfectly stuck, 0.0 where it moves."""
    if not values:
        return []
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1) : i + 1]
        spread = max(chunk) - min(chunk)
        out.append(1.0 if spread == 0 else max(0.0, 1.0 - spread / 32.0))
    return out


# ---------------------------------------------------------------------------
# audio fork
# ---------------------------------------------------------------------------
def to_audio(stream: Stream, config: MappingConfig | None = None) -> dict:
    """Values reinterpreted as ``{freq, amp, dur, gate}`` per event/voice."""
    cfg = config or MappingConfig()
    ceiling = stream.ceiling
    offsets, span = resolve(cfg.quantize_to) if cfg.quantize_to else (None, 12)
    note_span = cfg.note_high - cfg.note_low

    voices = []
    for index, name in enumerate(stream.names):
        raw = stream.data[index]
        values = _norm(raw, ceiling)
        changes = _deltas(raw, ceiling)
        flat = _flatness(raw, 4)

        freq, amp, dur, gate = [], [], [], []
        for i, value in enumerate(values):
            # -- pitch: absolute value, the "state" reading of the signal
            shaped = value if cfg.freq_curve == "direct" else (value**1.7)
            midi = cfg.note_low + shaped * note_span
            if offsets is not None:
                midi = cfg.note_low + quantize_semitone(
                    int(round(midi - cfg.note_low)), offsets, span
                )
            freq.append(round(midi_to_hz(midi), 3))

            # -- amplitude: change, not level. A stuck value fades; movement bites.
            loud = cfg.amp_floor + (1.0 - cfg.amp_floor) * min(1.0, changes[i] * 1.6)
            amp.append(round(loud, 4))

            # -- duration: flat data holds its note, busy data chops it
            dur.append(round(cfg.dur_base + cfg.dur_spread * flat[i], 4))

            # -- gate: re-trigger only on real movement (section 5's flatness rule)
            moved = abs(raw[i] - raw[i - 1]) if i else ceiling
            gate.append(1 if moved * 255 // (ceiling - 1 or 1) >= cfg.gate_threshold else 0)

        voices.append(
            {
                "name": name,
                "waveform": cfg.waveforms[index % len(cfg.waveforms)],
                "freq": freq,
                "amp": amp,
                "dur": dur,
                "gate": gate,
            }
        )
    return {"voices": voices}


# ---------------------------------------------------------------------------
# visual fork
# ---------------------------------------------------------------------------
def to_visual(stream: Stream, config: MappingConfig | None = None) -> dict:
    """Values reinterpreted as ``{x, y, density, hue_or_gray, glyph}`` per voice.

    The rotation is what keeps this from being the audio stream in a costume:
    voice 0's *position* comes from a different channel than voice 0's pitch did.
    """
    cfg = config or MappingConfig()
    ceiling = stream.ceiling
    count = stream.n_voices
    # The rotation must never land back on zero, or the visual side reads the
    # same channel the audio side is singing and the fork silently collapses --
    # which is exactly what happens on a 3-voice stream with the default of 3.
    rotation = 0 if count < 2 else (cfg.channel_rotation % count) or 1

    voices = []
    for index, name in enumerate(stream.names):
        own = stream.data[index]
        other = stream.data[(index + rotation) % count]

        values = _norm(own, ceiling)
        cross = _norm(other, ceiling)
        changes = _deltas(own, ceiling)
        turbulence = _turbulence(own, cfg.turbulence_window, ceiling)
        flat = _flatness(own, cfg.turbulence_window)

        x, y, density, gray, glyph, glitch = [], [], [], [], [], []
        for i in range(len(values)):
            # -- position: y from the rotated channel, x drifts with own change.
            # Voices therefore trace different paths from the ones they sing.
            x.append(round((i / max(1, len(values) - 1) + changes[i] * 0.12) % 1.0, 4))
            y.append(round(cross[i], 4))

            # -- density: mostly change, a little absolute level
            mix = cfg.density_from_delta
            density.append(round(min(1.0, changes[i] * mix + values[i] * (1.0 - mix)), 4))

            # -- brightness: turbulence. Still data goes dark and bands.
            gray.append(round(min(1.0, 0.15 + turbulence[i] * 0.95), 4))

            # -- glyph: byte value straight into the ASCII ramp by density
            level = min(1.0, 0.5 * values[i] + 0.5 * turbulence[i])
            glyph.append(int(level * (len(GLYPHS) - 1)))

            # -- glitch flag: a real spike in the data, not a decorative filter
            spike = max(changes[i], turbulence[i])
            glitch.append(1 if spike >= cfg.glitch_threshold else 0)

        voices.append(
            {
                "name": name,
                "x": x,
                "y": y,
                "density": density,
                "gray": gray,
                "glyph": glyph,
                "glitch": glitch,
                "flat": [round(f, 3) for f in flat],
            }
        )
    return {"glyphs": GLYPHS, "voices": voices}


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------
@dataclass
class Piece:
    """Everything the browser needs, and everything needed to regenerate it."""

    meta: dict
    audio: dict
    visual: dict
    envelope: dict = field(default_factory=dict)

    def audio_document(self) -> dict:
        return {"meta": self.meta, "envelope": self.envelope, **self.audio}

    def visual_document(self) -> dict:
        return {"meta": self.meta, "envelope": self.envelope, **self.visual}


def build_piece(
    stream: Stream,
    chain=None,
    envelope: Envelope | None = None,
    config: MappingConfig | None = None,
    mode: str = "closed",
    loop_policy: str = "vary",
    voice_entry: str = "variance",
    delay_note: str = "1/8.",
) -> Piece:
    cfg = config or MappingConfig()
    env = envelope or Envelope.constant(1.0)

    meta = {
        "format": FORMAT_VERSION,
        "generator": "serrin",
        # The doc's identity for a piece: source + chain + seed.
        "label": (
            f"{Path(str(stream.meta.get('source', 'stream'))).stem}"
            f"+{stream.meta.get('chain_name', 'raw')}"
            f"+{stream.meta.get('seed', 0)}"
        ),
        "seed": stream.meta.get("seed"),
        "fingerprint": fingerprint(stream),
        "rate": stream.rate,
        # The grid, named. The runtime needs it for swing, tempo-synced delay
        # and anything the panel wants to display in bars rather than seconds.
        "tempo": stream.tempo.to_json(),
        "frames": stream.length,
        "duration": round(stream.duration, 3),
        "bars": round(stream.bars, 3),
        "bit_depth": stream.bit_depth,
        "voices": stream.names,
        "voice_entry_order": voice_order(stream, voice_entry),
        "voice_entry_strategy": voice_entry,
        "mode": mode,
        "loop_policy": loop_policy,
        # A starting position for the runtime's delay, in note values rather
        # than seconds, so it stays on the grid if the tempo is changed live.
        "delay_note": delay_note,
        "source": stream.meta.get("source"),
        "columns": stream.meta.get("columns"),
        "granularity": stream.meta.get("granularity", 1),
        "chain": chain.to_json() if chain is not None else None,
        "chain_applied": stream.meta.get("chain_applied", []),
        "mapping": cfg.to_json(),
    }
    return Piece(
        meta=meta,
        audio=to_audio(stream, cfg),
        visual=to_visual(stream, cfg),
        envelope=env.to_json(),
    )


def write_json(path: str | Path, document: dict, compact: bool = True) -> int:
    """Write and return the byte count. Compact by default -- these get big."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(document, separators=(",", ":"))
        if compact
        else json.dumps(document, indent=2)
    )
    path.write_text(text + "\n", encoding="utf-8")
    return len(text)
