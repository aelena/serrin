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
#: ``"<shape>:<freq>hz[:<depth>]"`` where shape is one of the SHAPES below.
#: e.g. ``"sine:0.1hz"``, ``"square:2hz:0.5"``, ``"random:0.25hz"``.
SHAPES = ("sine", "triangle", "square", "saw", "random", "sample_hold")


def parse_lfo(spec: str | None) -> tuple[str, float, float] | None:
    """``"sine:0.1hz:0.5"`` -> ``("sine", 0.1, 0.5)``; ``None``/``"fixed"`` -> None."""
    if spec in (None, "", "fixed", "none", "off"):
        return None
    if not isinstance(spec, str):
        raise PedalError(f"lfo spec must be a string, got {spec!r}")
    parts = [p.strip().lower() for p in spec.split(":")]
    shape = parts[0]
    if shape not in SHAPES:
        raise PedalError(f"unknown lfo shape {shape!r}; use one of {SHAPES}")
    freq = 0.1
    depth = 1.0
    if len(parts) > 1 and parts[1]:
        freq = float(parts[1].removesuffix("hz").strip() or 0.1)
    if len(parts) > 2 and parts[2]:
        depth = float(parts[2])
    return shape, freq, max(0.0, min(1.0, depth))


class Lfo:
    """Bipolar oscillator in [-1, 1], sampled per frame.

    ``random`` and ``sample_hold`` need the Rng; they are still deterministic
    because that Rng is derived from the seed and the pedal's chain position.
    """

    __slots__ = ("shape", "freq", "depth", "_rng", "_held", "_period", "phase0")

    def __init__(self, spec: str | None, rate: float, rng: Rng, phase0: float = 0.0):
        parsed = parse_lfo(spec)
        self.shape = parsed[0] if parsed else None
        self.freq = parsed[1] if parsed else 0.0
        self.depth = parsed[2] if parsed else 0.0
        self._rng = rng
        self._held = 0.0
        self.phase0 = phase0
        # Frames per LFO cycle. rate is frames/second, freq is cycles/second.
        self._period = (rate / self.freq) if (parsed and self.freq > 0) else 0.0

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
