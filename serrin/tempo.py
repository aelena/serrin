"""Tempo: giving the frame grid a name.

Section 4.2 offered two options for tick resolution -- a fixed musical grid, or
one driven by the data -- and serrin picks data-driven: one row (or one
aggregated window) is one frame. That does not change here. What changes is that
the frame rate stops being a bare number.

The insight is that the two options were never opposed. A stream at 8 frames per
second *is* sixteenth notes at 120 BPM; it just had no name, so nothing could be
expressed in terms of it. Naming it costs nothing at render time and buys:

  * an author-facing unit that means something ("100 BPM in eighths" rather than
    "3.33 frames per second");
  * LFOs that can be written in beats and bars instead of hertz, so a tremolo
    lands on the grid instead of drifting across it;
  * a delay whose time is a dotted eighth rather than "about three frames";
  * swing, which is the difference between a grid and a tempo.

The data still decides *what* happens on each step. Tempo only decides when the
steps are, which is the one thing the data has no opinion about -- a CSV row has
no duration.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Note values that make sense as a step. 4 = quarter, 8 = eighth, 16 = sixteenth.
#: Triplet values are the 12/24 entries: a step of 12 is an eighth-note triplet.
SUBDIVISIONS = (1, 2, 4, 6, 8, 12, 16, 24, 32)

#: Delay times available to the audio engine, as fractions of a beat.
NOTE_FRACTIONS: dict[str, float] = {
    "1/1": 4.0,
    "1/2": 2.0,
    "1/4.": 1.5,  # dotted quarter
    "1/4": 1.0,
    "1/4t": 2.0 / 3.0,  # quarter triplet
    "1/8.": 0.75,  # dotted eighth -- the classic delay setting
    "1/8": 0.5,
    "1/8t": 1.0 / 3.0,
    "1/16": 0.25,
    "1/16t": 1.0 / 6.0,
    "1/32": 0.125,
}

#: Swing at 1.0 is a triplet feel: the pair splits 2:1 instead of 1:1, which
#: means the offbeat step is pushed by a third of a step. Anything beyond that
#: stops being swing and starts being a different rhythm.
MAX_SWING_OFFSET = 1.0 / 3.0


class TempoError(ValueError):
    pass


@dataclass(frozen=True)
class Tempo:
    """A frame grid with a musical name.

    ``subdivision`` is a *note value*, not a count: 16 means each frame is a
    sixteenth note. That is the unit musicians actually speak in, and it makes
    the default self-explanatory -- 120 BPM in sixteenths is 8 frames a second,
    which is exactly the rate serrin used before tempo existed.
    """

    bpm: float = 120.0
    subdivision: int = 16
    swing: float = 0.0
    beats_per_bar: int = 4

    def __post_init__(self) -> None:
        if self.bpm <= 0:
            raise TempoError(f"bpm must be positive, got {self.bpm}")
        if self.subdivision <= 0:
            raise TempoError(f"subdivision must be positive, got {self.subdivision}")
        if not 0.0 <= self.swing <= 1.0:
            raise TempoError(f"swing runs 0..1 (1 = triplet feel), got {self.swing}")
        if self.beats_per_bar <= 0:
            raise TempoError(f"beats_per_bar must be positive, got {self.beats_per_bar}")

    # -- the grid -----------------------------------------------------------
    @property
    def steps_per_beat(self) -> float:
        """Frames per beat. A quarter-note beat, so subdivision 16 gives 4."""
        return self.subdivision / 4.0

    @property
    def rate(self) -> float:
        """Frames per second -- what the rest of the pipeline consumes."""
        return self.bpm / 60.0 * self.steps_per_beat

    @property
    def seconds_per_step(self) -> float:
        return 1.0 / self.rate

    @property
    def seconds_per_beat(self) -> float:
        return 60.0 / self.bpm

    @property
    def steps_per_bar(self) -> float:
        return self.steps_per_beat * self.beats_per_bar

    def note_seconds(self, note: str) -> float:
        """Seconds for a note value like ``"1/8."`` -- used for the delay time."""
        if note not in NOTE_FRACTIONS:
            raise TempoError(f"unknown note value {note!r}; have {sorted(NOTE_FRACTIONS)}")
        return NOTE_FRACTIONS[note] * self.seconds_per_beat

    # -- swing --------------------------------------------------------------
    def swing_offset(self, index: int) -> float:
        """Seconds to push frame ``index`` later.

        Offbeat steps only, and never by a full step, so onsets stay strictly
        increasing -- the transport's scheduler walks frames in order and would
        drop any frame whose onset went backwards.
        """
        if not self.swing or index % 2 == 0:
            return 0.0
        return self.swing * MAX_SWING_OFFSET * self.seconds_per_step

    def onset(self, index: int, speed: float = 1.0) -> float:
        """When frame ``index`` sounds, in seconds of transport time."""
        base = index * self.seconds_per_step
        return (base + self.swing_offset(index)) / (speed or 1.0)

    # -- reading a position -------------------------------------------------
    def position(self, index: int) -> tuple[int, int, int]:
        """``(bar, beat, step)``, all 1-based, the way a DAW displays it."""
        steps_per_bar = self.steps_per_bar
        bar, within_bar = divmod(index, steps_per_bar)
        beat, step = divmod(within_bar, self.steps_per_beat)
        return int(bar) + 1, int(beat) + 1, int(step) + 1

    def format_position(self, index: int) -> str:
        bar, beat, step = self.position(index)
        return f"{bar}.{beat}.{step}"

    def bars(self, frames: int) -> float:
        return frames / self.steps_per_bar

    # -- construction -------------------------------------------------------
    @staticmethod
    def from_rate(rate: float, subdivision: int = 16, **kwargs) -> "Tempo":
        """Infer a tempo from a raw frame rate.

        Every stream has a tempo whether or not the author named one, so a plain
        ``--rate 6`` still lands in musical units rather than being a special
        case the rest of the code has to check for.
        """
        if rate <= 0:
            raise TempoError(f"rate must be positive, got {rate}")
        steps_per_beat = subdivision / 4.0
        return Tempo(bpm=rate * 60.0 / steps_per_beat, subdivision=subdivision, **kwargs)

    @staticmethod
    def parse(spec: str | dict | float | None) -> "Tempo":
        """Accept the shorthand, a dict from a preset, or a bare BPM number.

        Shorthand is ``"<bpm>[/<subdivision>][+<swing>]"``:
        ``"120"``, ``"96/8"``, ``"128/16+0.3"``.
        """
        if spec is None:
            return Tempo()
        if isinstance(spec, Tempo):
            return spec
        if isinstance(spec, (int, float)):
            return Tempo(bpm=float(spec))
        if isinstance(spec, dict):
            return Tempo(
                bpm=float(spec.get("bpm", 120.0)),
                subdivision=int(spec.get("subdivision", 16)),
                swing=float(spec.get("swing", 0.0)),
                beats_per_bar=int(spec.get("beats_per_bar", 4)),
            )

        text = str(spec).strip().lower().replace("bpm", "")
        swing = 0.0
        if "+" in text:
            text, swing_text = text.split("+", 1)
            swing = float(swing_text)
        subdivision = 16
        if "/" in text:
            text, sub_text = text.split("/", 1)
            subdivision = int(sub_text)
        try:
            bpm = float(text)
        except ValueError as exc:
            raise TempoError(
                f"cannot read tempo {spec!r}; use '120', '96/8' or '128/16+0.3'"
            ) from exc
        return Tempo(bpm=bpm, subdivision=subdivision, swing=swing)

    # -- io -----------------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "bpm": round(self.bpm, 4),
            "subdivision": self.subdivision,
            "swing": self.swing,
            "beats_per_bar": self.beats_per_bar,
            "rate": round(self.rate, 6),
            "steps_per_beat": self.steps_per_beat,
        }

    def describe(self) -> str:
        note = {4: "quarters", 8: "eighths", 16: "sixteenths", 32: "thirty-seconds"}.get(
            self.subdivision, f"1/{self.subdivision} notes"
        )
        swing = f", swing {self.swing:.2f}" if self.swing else ""
        return (
            f"{self.bpm:g} BPM in {note} "
            f"({self.rate:g} frames/s, {self.beats_per_bar}/4{swing})"
        )
