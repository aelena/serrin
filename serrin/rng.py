"""Seed-deterministic randomness.

Everything random in serrin comes from here. The rule from the design doc is
"reproducible but not hand-predictable": same source + same chain + same seed
must regenerate byte-identically, on any machine, on any Python build.

That last part is why this does not use ``random.Random``: the stdlib Mersenne
Twister is stable in practice but its *derived* helpers (shuffle, choice,
randrange rejection loops) are not contractually frozen across versions. A
64-bit SplitMix generator is ~20 lines, fully specified, and trivially
splittable -- which matters here because every pedal wants its own independent
substream keyed by its position in the chain.
"""

from __future__ import annotations

import hashlib

MASK64 = (1 << 64) - 1


def seed_from_bytes(data: bytes) -> int:
    """Fold arbitrary bytes into a 64-bit seed (blake2b, stable across builds)."""
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")


class Rng:
    """SplitMix64. Small, deterministic, splittable."""

    __slots__ = ("_s",)

    def __init__(self, seed: int) -> None:
        self._s = seed & MASK64

    # -- core ---------------------------------------------------------------
    def next_u64(self) -> int:
        self._s = (self._s + 0x9E3779B97F4A7C15) & MASK64
        z = self._s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    # -- conveniences -------------------------------------------------------
    def float01(self) -> float:
        """Uniform in [0, 1). 53 significant bits, like a double should have."""
        return (self.next_u64() >> 11) * (1.0 / (1 << 53))

    def below(self, n: int) -> int:
        """Uniform int in [0, n). Modulo-biased by ~2^-64; irrelevant at our n."""
        if n <= 0:
            raise ValueError("below() needs n > 0")
        return self.next_u64() % n

    def between(self, lo: int, hi: int) -> int:
        """Uniform int in [lo, hi] inclusive."""
        return lo + self.below(hi - lo + 1)

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self.float01()

    def pick(self, seq):
        return seq[self.below(len(seq))]

    def chance(self, p: float) -> bool:
        return self.float01() < p

    # -- splitting ----------------------------------------------------------
    def split(self, label: str) -> "Rng":
        """Derive an independent substream from this one plus a text label.

        Used to give each pedal its own randomness keyed by name+position, so
        that reordering a chain changes the result deterministically instead of
        by luck-of-the-draw consumption order.
        """
        mixed = seed_from_bytes(label.encode("utf-8")) ^ self.next_u64()
        return Rng(mixed)


def derive(seed: int, label: str) -> Rng:
    """Standalone substream: same seed + same label always gives the same Rng."""
    return Rng(seed ^ seed_from_bytes(label.encode("utf-8")))


class Lfsr:
    """Fibonacci LFSR over ``width`` bits -- the ``xor_mask`` pedal's noise source.

    Deliberately not a good PRNG: short-period LFSRs buzz and repeat, and that
    audible periodicity is the point (it is what makes an LFSR mask sound
    different from a random mask).

    Two guards against the one failure mode that matters. All-zero is an
    absorbing state -- a dead LFSR outputs silence forever -- and an arbitrary
    author-chosen tap list can *walk into* it, not just start there. So bit 0
    (the bit being shifted out) is always part of the feedback, and a state that
    reaches zero anyway is reloaded. Without this, ``taps: [3, 1]`` on an 8-bit
    stream dies on its second call.
    """

    __slots__ = ("_state", "_taps", "_width", "_mask")

    def __init__(self, seed: int, taps: list[int] | None = None, width: int = 8) -> None:
        self._width = width
        self._mask = (1 << width) - 1
        requested = list(taps) if taps else [width - 1, 2]
        self._taps = sorted({tap % width for tap in requested} | {0})
        state = seed & self._mask
        self._state = state if state else 1

    def next(self) -> int:
        bit = 0
        for tap in self._taps:
            bit ^= (self._state >> tap) & 1
        self._state = ((self._state >> 1) | (bit << (self._width - 1))) & self._mask
        if self._state == 0:
            self._state = 1
        return self._state
