"""Pedal plumbing: the signature, the registry, and the LFO helper.

The design rule from section 3.1 is the load-bearing one here: *no single pedal
should be smart*. If a pedal in this package starts needing branches inside
branches, it is two pedals wearing one name, and it should be split.

A pedal is a pure function ``(Stream, params, Rng) -> Stream``. Pure in the sense
that matters for reproducibility: it must not read the clock, the filesystem, or
any randomness other than the ``Rng`` it is handed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from ..rng import Rng
from ..stream import Stream

PedalFn = Callable[[Stream, dict, Rng], Stream]


@dataclass(frozen=True)
class Pedal:
    name: str
    fn: PedalFn
    #: Parameter name -> default. Doubles as the schema the CLI and the web
    #: panel introspect, so keep it honest.
    defaults: dict
    doc: str = ""

    def __call__(self, stream: Stream, params: dict | None, rng: Rng) -> Stream:
        merged = dict(self.defaults)
        merged.update(params or {})
        unknown = set(merged) - set(self.defaults)
        if unknown:
            raise PedalError(
                f"pedal {self.name!r} got unknown params {sorted(unknown)}; "
                f"accepts {sorted(self.defaults)}"
            )
        return self.fn(stream, merged, rng)


class PedalError(ValueError):
    pass


REGISTRY: dict[str, Pedal] = {}


def register(name: str, defaults: dict, doc: str = "") -> Callable[[PedalFn], PedalFn]:
    def decorate(fn: PedalFn) -> PedalFn:
        REGISTRY[name] = Pedal(name=name, fn=fn, defaults=defaults, doc=doc or fn.__doc__ or "")
        return fn

    return decorate


def get(name: str) -> Pedal:
    try:
        return REGISTRY[name]
    except KeyError:
        raise PedalError(
            f"unknown pedal {name!r}; catalog: {', '.join(sorted(REGISTRY))}"
        ) from None


def catalog() -> list[Pedal]:
    return [REGISTRY[k] for k in sorted(REGISTRY)]


# ---------------------------------------------------------------------------
# LFO -- shared by every pedal whose parameter is allowed to move over time
# ---------------------------------------------------------------------------
#: Accepted forms: ``None`` / ``"fixed"`` (no movement), or
#: ``"<shape>:<period>[:<depth>]"`` where shape is one of the SHAPES below and
#: the period is written in one of three units:
#:
#:   * hertz     -- ``"sine:0.1hz"``          absolute, ignores tempo
#:   * beats     -- ``"square:4beats"``       one cycle every four beats
#:   * bars      -- ``"saw:2bars"``           one cycle every two bars
#:
#: Beats and bars are the interesting ones: an LFO written in hertz drifts across
#: the grid, while one written in beats stays locked to it however the tempo
#: changes. Fractions are allowed -- ``"triangle:1/2beat"`` is twice a beat.
SHAPES = ("sine", "triangle", "square", "saw", "random", "sample_hold")

#: Suffix -> unit, longest first so "beats" is matched before "bea"... and so
#: that "bars" never gets mistaken for "bar".
_UNITS = (("beats", "beat"), ("beat", "beat"), ("bars", "bar"), ("bar", "bar"), ("hz", "hz"))


def _parse_number(text: str) -> float:
    """Accept ``2``, ``0.5`` or ``1/4`` -- musicians write the last one."""
    text = text.strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        return float(numerator) / float(denominator)
    return float(text)


def parse_lfo(spec: str | None) -> tuple[str, float, str, float] | None:
    """``"sine:1/2beat:0.5"`` -> ``("sine", 0.5, "beat", 0.5)``.

    Returns ``(shape, value, unit, depth)``, or None for a fixed parameter. The
    value means cycles-per-second for ``hz`` and *period* for ``beat``/``bar``
    -- "0.1 hz" and "4 beats" both read naturally, and they point opposite ways.
    """
    if spec in (None, "", "fixed", "none", "off"):
        return None
    if not isinstance(spec, str):
        raise PedalError(f"lfo spec must be a string, got {spec!r}")
    parts = [p.strip().lower() for p in spec.split(":")]
    shape = parts[0]
    if shape not in SHAPES:
        raise PedalError(f"unknown lfo shape {shape!r}; use one of {SHAPES}")

    value, unit = 0.1, "hz"
    if len(parts) > 1 and parts[1]:
        body = parts[1]
        for suffix, resolved in _UNITS:
            if body.endswith(suffix):
                unit = resolved
                body = body[: -len(suffix)]
                break
        try:
            value = _parse_number(body or "0.1")
        except (ValueError, ZeroDivisionError) as exc:
            raise PedalError(f"cannot read lfo rate in {spec!r}") from exc
    if value <= 0:
        raise PedalError(f"lfo rate must be positive in {spec!r}")

    depth = 1.0
    if len(parts) > 2 and parts[2]:
        depth = float(parts[2])
    return shape, value, unit, max(0.0, min(1.0, depth))


class Lfo:
    """Bipolar oscillator in [-1, 1], sampled per frame.

    ``random`` and ``sample_hold`` need the Rng; they are still deterministic
    because that Rng is derived from the seed and the pedal's chain position.
    """

    __slots__ = ("shape", "unit", "value", "depth", "_rng", "_held", "_period", "phase0")

    def __init__(self, spec: str | None, stream, rng: Rng, phase0: float = 0.0):
        parsed = parse_lfo(spec)
        self.shape = parsed[0] if parsed else None
        self.value = parsed[1] if parsed else 0.0
        self.unit = parsed[2] if parsed else "hz"
        self.depth = parsed[3] if parsed else 0.0
        self._rng = rng
        self._held = 0.0
        self.phase0 = phase0
        self._period = self._period_frames(stream) if parsed else 0.0

    def _period_frames(self, stream) -> float:
        """Frames per LFO cycle, in whichever unit the author used."""
        tempo = stream.tempo
        if self.unit == "beat":
            return self.value * tempo.steps_per_beat
        if self.unit == "bar":
            return self.value * tempo.steps_per_bar
        # hertz: cycles per second against frames per second
        return stream.rate / self.value if self.value > 0 else 0.0

    @property
    def active(self) -> bool:
        return self.shape is not None and self._period > 0

    def at(self, frame: int) -> float:
        if not self.active:
            return 0.0
        phase = ((frame / self._period) + self.phase0) % 1.0
        shape = self.shape
        if shape == "sine":
            value = math.sin(2.0 * math.pi * phase)
        elif shape == "triangle":
            value = 4.0 * abs(phase - 0.5) - 1.0
        elif shape == "square":
            value = 1.0 if phase < 0.5 else -1.0
        elif shape == "saw":
            value = 2.0 * phase - 1.0
        elif shape == "random":
            value = self._rng.uniform(-1.0, 1.0)
        else:  # sample_hold -- new value once per cycle, held in between
            if frame % max(1, int(self._period)) == 0:
                self._held = self._rng.uniform(-1.0, 1.0)
            value = self._held
        return value * self.depth


def bits_mask(bit_depth: int) -> int:
    return (1 << bit_depth) - 1
