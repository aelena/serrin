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


#: Primitive polynomials over GF(2), as tap positions, one per width.
#:
#: A polynomial being *primitive* is what makes an LFSR maximal-length: it visits
#: all 2**n - 1 nonzero states before repeating. These are the defaults, so an
#: author who does not care about tap theory gets the longest run available at
#: their bit depth. Verified by ``test_primitive_taps_are_maximal_length``, which
#: measures the real period rather than trusting the table.
PRIMITIVE_TAPS: dict[int, list[int]] = {
    2: [1, 0],
    3: [1, 0],
    4: [1, 0],
    5: [2, 0],
    6: [1, 0],
    7: [1, 0],
    8: [4, 3, 2, 0],
    9: [4, 0],
    10: [3, 0],
    11: [2, 0],
    12: [6, 4, 1, 0],
    13: [4, 3, 1, 0],
    14: [5, 3, 1, 0],
    15: [1, 0],
    16: [5, 3, 2, 0],
}


class Lfsr:
    """Fibonacci LFSR over ``width`` bits -- the ``xor_mask`` pedal's noise source.

    Deliberately not a good PRNG: short-period LFSRs buzz and repeat, and that
    audible periodicity is the point. It is what makes an LFSR mask sound
    different from a random mask -- you can *hear* the period.

    **Why it cannot drift to zero.** The state map is
    ``a[t+n] = XOR of a[t+j] for j in taps``, whose characteristic polynomial is
    ``x^n + sum(x^j for j in taps)``. That map is linear over GF(2), and it is
    invertible exactly when the polynomial has a nonzero constant term -- i.e.
    when ``0`` is one of the taps. Recovering the discarded low bit needs the
    feedback bit, and the feedback bit only carries information about it if bit 0
    fed into it.

    An invertible map on 2**n states is a bijection, so all-zero has exactly one
    preimage: itself. A nonzero state therefore *cannot* reach zero -- not
    "usually does not", cannot. Enforcing ``0 in taps`` is the whole fix; there
    is no reachable dead state left to guard against at runtime.

    This is also the standard convention. Tap lists are written as polynomial
    exponents with the ``+ 1`` left implicit, so the design document's
    ``taps: [3, 1]`` means ``x^8 + x^3 + x + 1`` and normalizes to ``[3, 1, 0]``
    (period 42 at 8 bits -- short, buzzy, and perfectly usable).
    """

    __slots__ = ("_state", "_taps", "_width", "_mask", "_seed")

    def __init__(self, seed: int, taps: list[int] | None = None, width: int = 8) -> None:
        if width < 2:
            raise ValueError(f"an LFSR needs at least 2 bits, got {width}")
        self._width = width
        self._mask = (1 << width) - 1
        requested = list(taps) if taps else PRIMITIVE_TAPS.get(width, [1, 0])
        # The implicit `+ 1`: normalize into range, drop duplicates, force bit 0.
        self._taps = sorted({tap % width for tap in requested} | {0}, reverse=True)
        if len(self._taps) < 2:
            # Only bit 0 tapped is `x^n + 1`, which is not merely non-primitive:
            # it just rotates the register and outputs the seed forever.
            self._taps = list(PRIMITIVE_TAPS.get(width, [1, 0]))
        # Only the seed can be zero, and only here, so this is the only guard.
        state = seed & self._mask
        self._state = state if state else 1
        self._seed = self._state

    @property
    def taps(self) -> list[int]:
        """The normalized taps actually in use, bit 0 included."""
        return list(self._taps)

    def next(self) -> int:
        bit = 0
        for tap in self._taps:
            bit ^= (self._state >> tap) & 1
        self._state = ((self._state >> 1) | (bit << (self._width - 1))) & self._mask
        return self._state

    def period(self, limit: int | None = None) -> int:
        """Steps before the register returns to its seed state.

        Worth knowing rather than assuming: the period is the length of the loop
        the mask repeats on, so it is audible. At 8 bits a maximal register runs
        255 frames -- about half a minute at the default tempo -- while the
        document's example taps run 42, which reads as a rhythmic pulse instead
        of as noise. Neither is wrong; they are different pedals.

        Does not disturb this instance's state.
        """
        ceiling = limit if limit is not None else (1 << self._width)
        probe = Lfsr(self._seed, self._taps, self._width)
        for step in range(1, ceiling + 1):
            if probe.next() == self._seed:
                return step
        return -1  # longer than the ceiling; only reachable with a custom limit

    @property
    def is_maximal(self) -> bool:
        """True when the taps form a primitive polynomial for this width."""
        return self.period() == self._mask
