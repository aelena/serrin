"""The intensity envelope: a function ``t -> intensity`` in [0, 1].

Section 5.1 insists the curve can come from three places -- a hand-drawn stroke,
a parametric equation, or a named archetype -- and that the rest of the system
must not care which. So all three collapse into one artifact here: a sampled
curve plus linear interpolation. A stroke is already points; an equation gets
sampled into points; an archetype generates points from phase durations.

Sampling rather than keeping the callable alive is deliberate. It makes the curve
serialisable, which is what lets a *live* stroke be recorded and replayed
identically later (one of the open questions in section 8 -- the answer this
implementation gives is yes, it can).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Points in a baked curve. 512 is ~2 Hz resolution on a four-minute piece --
#: far finer than an intensity envelope needs, still small enough to ship in JSON.
DEFAULT_RESOLUTION = 512


class EnvelopeError(ValueError):
    pass


# ---------------------------------------------------------------------------
# equations
# ---------------------------------------------------------------------------
def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def sigmoid(t: float, centre: float = 0.5, steepness: float = 10.0) -> float:
    return 1.0 / (1.0 + math.exp(-steepness * (t - centre)))


#: Named parametric curves. Each takes normalized time in [0, 1] and returns
#: intensity in [0, 1]. Extra kwargs come from the preset.
EQUATIONS: dict[str, callable] = {
    "ramp": lambda t, **k: t,
    "ramp_down": lambda t, **k: 1.0 - t,
    "sigmoid": lambda t, centre=0.5, steepness=10.0, **k: sigmoid(t, centre, steepness),
    # Rise on a sigmoid, fall on a decaying exponential -- the doc's own example
    # of a build-up plus fade-out written as one formula.
    "arc": lambda t, peak=0.62, steepness=9.0, decay=3.0, **k: (
        sigmoid(t, peak * 0.55, steepness)
        if t <= peak
        else sigmoid(peak, peak * 0.55, steepness)
        * math.exp(-decay * (t - peak) / max(1e-6, 1.0 - peak))
    ),
    "plateau": lambda t, rise=0.2, fall=0.8, **k: (
        _clamp01(t / max(1e-6, rise))
        if t < rise
        else 1.0
        if t < fall
        else _clamp01((1.0 - t) / max(1e-6, 1.0 - fall))
    ),
    "pulse": lambda t, cycles=4.0, floor=0.15, **k: floor
    + (1.0 - floor) * (0.5 - 0.5 * math.cos(2.0 * math.pi * cycles * t)),
    "flat": lambda t, level=1.0, **k: level,
    "sawtooth": lambda t, cycles=3.0, **k: (t * cycles) % 1.0,
}


# ---------------------------------------------------------------------------
# archetypes
# ---------------------------------------------------------------------------
#: Section 5.1.1's named progression templates, as (label, relative duration,
#: target intensity at the end of the phase). They generate a starting curve the
#: author is expected to edit -- they are a convenience layer, not a format.
ARCHETYPES: dict[str, list[tuple[str, float, float]]] = {
    "build_up": [("intro", 0.25, 0.25), ("build", 0.45, 0.8), ("hold", 0.3, 1.0)],
    "crescendo": [("seed", 0.15, 0.1), ("crescendo", 0.7, 1.0), ("hold", 0.15, 1.0)],
    "climax": [
        ("intro", 0.15, 0.2),
        ("build", 0.35, 0.7),
        ("climax", 0.2, 1.0),
        ("fade_out", 0.2, 0.35),
        ("dismantling", 0.1, 0.0),
    ],
    "fade_out": [("hold", 0.4, 1.0), ("fade", 0.6, 0.0)],
    "dismantling": [("dense", 0.3, 1.0), ("thin", 0.4, 0.4), ("bare", 0.3, 0.05)],
    # The full five-phase sequence named in the doc, as one template.
    "full_arc": [
        ("build_up", 0.2, 0.35),
        ("crescendo", 0.25, 0.75),
        ("climax", 0.2, 1.0),
        ("fade_out", 0.2, 0.4),
        ("dismantling", 0.15, 0.0),
    ],
}


def archetype_points(name: str, curvature: float = 1.0) -> list[tuple[float, float]]:
    """Turn a named archetype into ``(t, intensity)`` points.

    ``curvature`` bends each phase: 1.0 linear, >1 slow-then-fast (a phase that
    holds back before committing), <1 fast-then-slow.
    """
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in ARCHETYPES:
        raise EnvelopeError(
            f"unknown archetype {name!r}; have {', '.join(sorted(ARCHETYPES))}"
        )
    phases = ARCHETYPES[key]
    total = sum(duration for _, duration, _ in phases)
    points: list[tuple[float, float]] = [(0.0, phases[0][2] * 0.25)]
    cursor = 0.0
    level = points[0][1]
    steps = 8  # per phase, so the bend is visible
    for _, duration, target in phases:
        width = duration / total
        for step in range(1, steps + 1):
            fraction = step / steps
            bent = fraction**curvature
            points.append((cursor + width * fraction, level + (target - level) * bent))
        cursor += width
        level = target
    return points


# ---------------------------------------------------------------------------
# the envelope itself
# ---------------------------------------------------------------------------
@dataclass
class Envelope:
    """A baked curve over normalized time, with linear interpolation between points."""

    points: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 1.0), (1.0, 1.0)])
    #: What produced it -- kept for the export so a piece stays self-describing.
    origin: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.points:
            raise EnvelopeError("an envelope needs at least one point")
        cleaned = sorted(
            (_clamp01(t), _clamp01(v)) for t, v in self.points
        )
        # A stroke can double back on itself; keep the last value per timestamp
        # so the curve stays a function of t.
        deduped: list[tuple[float, float]] = []
        for t, v in cleaned:
            if deduped and abs(deduped[-1][0] - t) < 1e-9:
                deduped[-1] = (t, v)
            else:
                deduped.append((t, v))
        if deduped[0][0] > 0.0:
            deduped.insert(0, (0.0, deduped[0][1]))
        if deduped[-1][0] < 1.0:
            deduped.append((1.0, deduped[-1][1]))
        self.points = deduped

    # -- constructors -------------------------------------------------------
    @staticmethod
    def constant(level: float = 1.0) -> "Envelope":
        """Mode B (endless stream): no arc at all."""
        return Envelope([(0.0, level), (1.0, level)], {"kind": "constant", "level": level})

    @staticmethod
    def from_equation(
        name: str, resolution: int = DEFAULT_RESOLUTION, **params
    ) -> "Envelope":
        key = str(name).strip().lower()
        if key not in EQUATIONS:
            raise EnvelopeError(
                f"unknown equation {name!r}; have {', '.join(sorted(EQUATIONS))}"
            )
        fn = EQUATIONS[key]
        points = [
            (i / (resolution - 1), _clamp01(fn(i / (resolution - 1), **params)))
            for i in range(resolution)
        ]
        return Envelope(points, {"kind": "equation", "equation": key, "params": params})

    @staticmethod
    def from_archetype(name: str, curvature: float = 1.0) -> "Envelope":
        return Envelope(
            archetype_points(name, curvature),
            {"kind": "archetype", "archetype": name, "curvature": curvature},
        )

    @staticmethod
    def from_points(points, normalize_time: bool = True, label: str = "stroke") -> "Envelope":
        """A hand-drawn stroke: raw ``(t, intensity)`` samples from a pointer.

        ``normalize_time`` rescales whatever time base the stroke was captured on
        onto [0, 1], which is what section 5.1 asks for ("normalize across total
        time"), so a stroke drawn in 3 seconds can drive a 6-minute piece.
        """
        pairs = [(float(t), float(v)) for t, v in points]
        if not pairs:
            raise EnvelopeError("empty stroke")
        if normalize_time:
            times = [t for t, _ in pairs]
            lo, hi = min(times), max(times)
            span = hi - lo
            pairs = [(((t - lo) / span) if span else 0.0, v) for t, v in pairs]
        return Envelope(pairs, {"kind": "stroke", "label": label, "captured": len(pairs)})

    @staticmethod
    def from_spec(spec: dict | str | None) -> "Envelope":
        """Build from a preset fragment or a shorthand string.

        Shorthands: ``"arc"`` (equation), ``"archetype:climax"``, ``"flat"``,
        or ``"sigmoid:centre=0.4,steepness=14"``.
        """
        if spec in (None, "", False):
            return Envelope.constant(1.0)
        if isinstance(spec, str):
            text = spec.strip()
            if text.startswith("archetype:"):
                return Envelope.from_archetype(text.split(":", 1)[1])
            if ":" in text:
                name, rest = text.split(":", 1)
                params = {}
                for chunk in rest.split(","):
                    if "=" not in chunk:
                        continue
                    key, value = chunk.split("=", 1)
                    params[key.strip()] = float(value)
                return Envelope.from_equation(name, **params)
            return Envelope.from_equation(text)

        kind = str(spec.get("kind", "")).lower()
        if kind == "points" or "points" in spec:
            return Envelope.from_points(
                spec["points"], bool(spec.get("normalize_time", True))
            )
        if kind == "archetype" or "archetype" in spec:
            return Envelope.from_archetype(
                spec.get("archetype", "full_arc"), float(spec.get("curvature", 1.0))
            )
        if kind == "constant":
            return Envelope.constant(float(spec.get("level", 1.0)))
        params = dict(spec.get("params") or {})
        return Envelope.from_equation(spec.get("equation", "arc"), **params)

    # -- evaluation ---------------------------------------------------------
    def at(self, t: float) -> float:
        """Intensity at normalized time ``t``."""
        t = _clamp01(t)
        points = self.points
        if t <= points[0][0]:
            return points[0][1]
        if t >= points[-1][0]:
            return points[-1][1]
        lo, hi = 0, len(points) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if points[mid][0] <= t:
                lo = mid
            else:
                hi = mid
        t0, v0 = points[lo]
        t1, v1 = points[hi]
        if t1 == t0:
            return v1
        return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))

    def sample(self, count: int) -> list[float]:
        """Evenly spaced values -- what actually ships in the export."""
        if count <= 1:
            return [self.at(0.0)]
        return [round(self.at(i / (count - 1)), 5) for i in range(count)]

    def to_json(self, resolution: int = DEFAULT_RESOLUTION) -> dict:
        return {
            "origin": self.origin,
            "resolution": resolution,
            "curve": self.sample(resolution),
        }

    def ascii(self, width: int = 60, height: int = 8) -> str:
        """Terminal preview. The pipeline is a workshop tool; looking is cheap."""
        blocks = " .:-=+*#%@"
        rows = []
        for row in range(height, 0, -1):
            band_hi = row / height
            band_lo = (row - 1) / height
            line = []
            for col in range(width):
                value = self.at(col / max(1, width - 1))
                if value >= band_hi:
                    line.append(blocks[-1])
                elif value > band_lo:
                    fraction = (value - band_lo) / (band_hi - band_lo)
                    line.append(blocks[min(len(blocks) - 1, int(fraction * len(blocks)))])
                else:
                    line.append(" ")
            rows.append("|" + "".join(line))
        rows.append("+" + "-" * width)
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# voice activation (section 5.1.1)
# ---------------------------------------------------------------------------
def voice_order(stream, strategy: str = "variance") -> list[int]:
    """The order in which voices enter as intensity rises.

    ``columns``  -- author order, the literal column order (predictable).
    ``variance`` -- busiest voice first, so the piece opens on something alive.
    ``sparse``   -- quietest voice first, so it opens on a lone blip and fills in.
    """
    key = str(strategy).lower()
    count = stream.n_voices
    if key in ("columns", "column", "author", "fixed"):
        return list(range(count))
    ranked = sorted(range(count), key=lambda c: -stream.variance(c))
    if key == "variance":
        return ranked
    if key in ("sparse", "reverse_variance", "quiet"):
        return list(reversed(ranked))
    raise EnvelopeError(f"unknown voice order strategy {strategy!r}")


def active_voice_count(intensity: float, total: int, minimum: int = 1) -> int:
    """How many voices are audible at this intensity.

    Deliberately non-linear: the low end is stretched so a piece spends real time
    as one or two voices instead of racing to full density (section 5.1.1's "low
    intensity -> 1 active voice").
    """
    if total <= 0:
        return 0
    shaped = _clamp01(intensity) ** 1.6
    return max(minimum, min(total, int(round(minimum + shaped * (total - minimum)))))


def voice_gates(intensity: float, order: list[int], minimum: int = 1) -> list[bool]:
    """Per-voice on/off mask for a given intensity, honouring entry order."""
    live = active_voice_count(intensity, len(order), minimum)
    allowed = set(order[:live])
    return [index in allowed for index in range(len(order))]
