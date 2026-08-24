"""The one data structure the whole pipeline passes around.

Channel-major, not row-major. A CSV arrives row-major, but every pedal either
walks one channel along time (``delta``, ``caesar``, ``bitcrush``) or reads one
channel while writing another (``cross_mix``, ``interleave``). Both are ugly
over rows of tuples and obvious over a list of channels.

Values are plain ints, already quantized to ``bit_depth`` bits, so XOR and
Caesar mean what the doc says they mean.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass
class Stream:
    #: One entry per voice, in author-chosen order. ``len(names) <= MAX_VOICES``.
    names: list[str]
    #: ``data[channel][frame]`` -- every channel is the same length.
    data: list[list[int]]
    #: Bits per value. Everything downstream masks to this.
    bit_depth: int = 8
    #: Frames per second, so an LFO written as "0.1hz" means 0.1 Hz.
    rate: float = 8.0
    #: Free-form breadcrumbs (source file, columns, aggregation) for the export.
    meta: dict = field(default_factory=dict)

    # -- shape --------------------------------------------------------------
    @property
    def n_voices(self) -> int:
        return len(self.data)

    @property
    def length(self) -> int:
        return len(self.data[0]) if self.data else 0

    @property
    def ceiling(self) -> int:
        """Exclusive upper bound of a value at this bit depth."""
        return 1 << self.bit_depth

    @property
    def mask(self) -> int:
        return self.ceiling - 1

    @property
    def duration(self) -> float:
        return self.length / self.rate if self.rate else 0.0

    # -- copies -------------------------------------------------------------
    def with_data(self, data: list[list[int]]) -> "Stream":
        """New Stream, same settings, different samples."""
        return replace(self, data=data)

    def copy(self) -> "Stream":
        return replace(self, data=[list(ch) for ch in self.data], meta=dict(self.meta))

    def channel(self, ref: int | str) -> int:
        """Resolve a channel by index or by name. Indices wrap, both ways.

        Wrapping rather than raising is what makes presets portable. A chain
        written against an 8-column monitoring dump names ``driver_column: 7``;
        pointed at a 3-column CSV that would otherwise crash mid-render, and the
        author would have to maintain one preset per dataset width. Wrapping
        keeps the pedalboard reusable -- the voice it lands on is arbitrary, but
        so was the choice of column 7 in the first place.

        Names are not wrapped: a named column that is absent is a mistake, not
        a shape mismatch.
        """
        if isinstance(ref, int):
            if not self.n_voices:
                raise IndexError("stream has no channels")
            return ref % self.n_voices
        if ref in self.names:
            return self.names.index(ref)
        raise KeyError(f"no channel named {ref!r}; have {self.names}")

    def clamp(self, value: int) -> int:
        return int(value) & self.mask

    # -- cheap stats, used by mappings and by voice-entry ordering ----------
    def variance(self, channel: int) -> float:
        col = self.data[channel]
        if len(col) < 2:
            return 0.0
        mean = sum(col) / len(col)
        return sum((v - mean) ** 2 for v in col) / len(col)

    def describe(self) -> str:
        rows = [
            f"{self.n_voices} voices x {self.length} frames @ {self.rate}Hz "
            f"({self.duration:.1f}s), {self.bit_depth}-bit"
        ]
        for i, name in enumerate(self.names):
            col = self.data[i]
            rows.append(
                f"  [{i}] {name:<24} min={min(col):>4} max={max(col):>4} "
                f"var={self.variance(i):>9.1f}"
            )
        return "\n".join(rows)


#: Design constraint from section 3.2, not a technical one. Raising it is a
#: decision about the piece, so it lives here where it can be argued with.
MAX_VOICES = 8
