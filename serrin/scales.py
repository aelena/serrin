"""Scales: the bank of names, and the whole/half-step interval notation.

Section 3.3 of the design doc allows two ways to specify a scale and insists
both resolve to the same internal format. That internal format is a list of
*semitone offsets from the root*, e.g. major -> [0, 2, 4, 5, 7, 9, 11].

The author-facing form is *intervals between consecutive degrees*, written the
way a musician says them out loud: ``1`` whole step, ``1/2`` half step,
``1 1/2`` step and a half. Harmonic minor is therefore

    "1, 1/2, 1, 1, 1/2, 1 1/2, 1/2"

which is 7 intervals summing to 12 -- one full octave, closing back on the root.
"""

from __future__ import annotations

from fractions import Fraction

# ---------------------------------------------------------------------------
# The bank. Stored as interval strings, not offsets, so the source of the file
# reads like the doc does -- offsets are derived.
# ---------------------------------------------------------------------------
SCALE_BANK: dict[str, str] = {
    # --- the seven Greek modes ---------------------------------------------
    "ionian":          "1, 1, 1/2, 1, 1, 1, 1/2",
    "dorian":          "1, 1/2, 1, 1, 1, 1/2, 1",
    "phrygian":        "1/2, 1, 1, 1, 1/2, 1, 1",
    "lydian":          "1, 1, 1, 1/2, 1, 1, 1/2",
    "mixolydian":      "1, 1, 1/2, 1, 1, 1/2, 1",
    "aeolian":         "1, 1/2, 1, 1, 1/2, 1, 1",
    "locrian":         "1/2, 1, 1, 1/2, 1, 1, 1",
    # --- minors ------------------------------------------------------------
    "harmonic_minor":  "1, 1/2, 1, 1, 1/2, 1 1/2, 1/2",
    "melodic_minor":   "1, 1/2, 1, 1, 1, 1, 1/2",
    # --- jazzy / exotic ----------------------------------------------------
    "pentatonic_major": "1, 1, 1 1/2, 1, 1 1/2",
    "pentatonic_minor": "1 1/2, 1, 1, 1 1/2, 1",
    "blues":            "1 1/2, 1, 1/2, 1/2, 1 1/2, 1",
    "altered":          "1/2, 1, 1/2, 1, 1, 1, 1",
    "bebop_dominant":   "1, 1, 1/2, 1, 1, 1/2, 1/2, 1/2",
    "whole_tone":       "1, 1, 1, 1, 1, 1",
    "diminished":       "1, 1/2, 1, 1/2, 1, 1/2, 1, 1/2",
    "chromatic":        "1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2, 1/2",
}

# Aliases for the names people actually type.
ALIASES: dict[str, str] = {
    "major": "ionian",
    "minor": "aeolian",
    "natural_minor": "aeolian",
    "octatonic": "diminished",
    "pentatonic": "pentatonic_minor",
    "super_locrian": "altered",
}

#: The doc says "prefer a concrete scale as a sane default, leaving full
#: chromaticism as a deliberate option". This is that default.
DEFAULT_SCALE = "pentatonic_minor"


class ScaleError(ValueError):
    pass


def parse_intervals(spec: str) -> list[Fraction]:
    """``"1, 1/2, 1 1/2"`` -> ``[1, 1/2, 3/2]`` in whole-step units."""
    out: list[Fraction] = []
    for raw in spec.replace(";", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        total = Fraction(0)
        for part in token.split():
            try:
                total += Fraction(part)
            except (ValueError, ZeroDivisionError) as exc:
                raise ScaleError(f"cannot read interval {token!r} in {spec!r}") from exc
        if total <= 0:
            raise ScaleError(f"interval {token!r} in {spec!r} is not positive")
        out.append(total)
    if not out:
        raise ScaleError(f"no intervals found in {spec!r}")
    return out


def intervals_to_offsets(intervals: list[Fraction]) -> list[int]:
    """Whole-step intervals -> semitone offsets from the root.

    The final interval closes the octave and is dropped: a 7-interval major
    scale yields 7 offsets (0..11), not 8. Fractional semitones are rejected --
    microtonality is a different project.
    """
    offsets = [0]
    acc = Fraction(0)
    for step in intervals:
        acc += step * 2  # whole step == 2 semitones
        if acc.denominator != 1:
            raise ScaleError(f"interval sequence lands off the semitone grid at {acc}")
        offsets.append(int(acc))
    span = offsets[-1]
    if span <= 0:
        raise ScaleError("scale spans zero semitones")
    return offsets[:-1], span  # type: ignore[return-value]


def resolve(scale: str | list | None) -> tuple[list[int], int]:
    """Accept any of the doc's forms; return ``(semitone_offsets, octave_span)``.

    Accepted:
      * ``None``            -> the default scale
      * ``"dorian"``        -> by name, from the bank (aliases honoured)
      * ``"1, 1/2, 1 1/2"`` -> by explicit whole/half-step intervals
      * ``[0, 3, 5, 7, 10]``-> raw semitone offsets, for the impatient

    ``octave_span`` is normally 12, but a scale written with intervals that do
    not sum to an octave (whole tone written short, say) transposes by its own
    real span instead of being silently forced to 12.
    """
    if scale is None:
        scale = DEFAULT_SCALE

    if isinstance(scale, (list, tuple)):
        offsets = sorted({int(v) for v in scale})
        if not offsets:
            raise ScaleError("empty scale")
        return offsets, max(12, offsets[-1] + 1)

    text = str(scale).strip().lower().replace("-", "_").replace(" ", "_")
    text = ALIASES.get(text, text)
    if text in SCALE_BANK:
        return intervals_to_offsets(parse_intervals(SCALE_BANK[text]))

    # Not a name -- must be an interval spec. Put the spaces back first.
    spec = str(scale).strip()
    if any(ch.isdigit() for ch in spec):
        return intervals_to_offsets(parse_intervals(spec))

    raise ScaleError(
        f"unknown scale {scale!r}; use an interval spec like '1, 1/2, 1' "
        f"or one of: {', '.join(sorted(SCALE_BANK))}"
    )


def degree_to_semitone(degree: int, offsets: list[int], span: int = 12) -> int:
    """Map an unbounded scale degree to a semitone, wrapping into octaves.

    Degree 7 of a 7-note scale is the root an octave up, not an error.
    """
    n = len(offsets)
    octave, idx = divmod(degree, n)
    return offsets[idx] + octave * span


def quantize_semitone(semitone: int, offsets: list[int], span: int = 12) -> int:
    """Snap an arbitrary semitone onto the nearest scale member."""
    octave, within = divmod(semitone, span)
    best = min(offsets, key=lambda o: (abs(o - within), o))
    return best + octave * span


def midi_to_hz(midi: float) -> float:
    """A4 = MIDI 69 = 440 Hz."""
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def scale_names() -> list[str]:
    return sorted(SCALE_BANK)
