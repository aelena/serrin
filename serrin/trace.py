"""Recording what the pipeline did, so it can be inspected instead of guessed at.

Every stage of a render currently destroys its input. A CSV cell becomes a float
becomes a byte becomes nine successively transformed bytes becomes a frequency,
and the only thing that survives is the last one. That is fine for playing a
piece and useless for understanding it -- when a chain sounds wrong, the question
is always *which stage did that*, and there was no way to ask.

A trace answers it by keeping a window of each stage plus full statistics over
all of it. The window matters: a nine-pedal chain over eight voices and 2400
frames is 170k values per stage, and shipping all of it would make the trace
larger than the render. So values are sampled and stats are not -- the numbers
you see are a window, the numbers you measure are the whole thing, and the format
says which is which rather than leaving it to be assumed.

Entropy is included per channel because it is the one measurement this project is
actually about. A pedal that raises entropy is destroying structure; one that
lowers it is imposing some. Being able to watch that happen down a chain turns
"this sounds too chaotic" into a stage number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FORMAT = "serrin-trace/1"

#: Frames kept per channel per stage. Enough to see a pattern, small enough that
#: a full trace of a long render stays a readable file.
DEFAULT_WINDOW = 96


def entropy_bits(values: list[int], bit_depth: int = 8) -> float:
    """Shannon entropy of the value distribution, in bits.

    The headline number for a project about entropy. A uniform byte stream scores
    8.0; a constant scores 0. Reading it down a chain shows which pedals shred
    structure and which impose it -- ``delta`` on stable data drops it hard,
    ``xor_mask`` with an LFSR pushes it toward the ceiling, ``mod_reduce`` pulls
    it down by collapsing the range onto a scale.
    """
    if not values:
        return 0.0
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = len(values)
    return -sum(
        (count / total) * math.log2(count / total) for count in counts.values()
    )


def channel_stats(values: list[int], bit_depth: int = 8) -> dict:
    """Everything measurable about one channel, over *all* of it."""
    if not values:
        return {
            "frames": 0, "min": 0, "max": 0, "mean": 0.0, "variance": 0.0,
            "unique": 0, "entropy": 0.0, "change_rate": 0.0, "flat_longest": 0,
        }
    total = len(values)
    mean = sum(values) / total
    changes = sum(1 for a, b in zip(values, values[1:]) if a != b)

    longest = current = 1
    for a, b in zip(values, values[1:]):
        current = current + 1 if a == b else 1
        longest = max(longest, current)

    return {
        "frames": total,
        "min": min(values),
        "max": max(values),
        "mean": round(mean, 3),
        "variance": round(sum((v - mean) ** 2 for v in values) / total, 3),
        "unique": len(set(values)),
        # Out of bit_depth bits, so 8-bit data tops out at 8.0.
        "entropy": round(entropy_bits(values, bit_depth), 4),
        # How often anything happens at all. A voice with a low change rate is a
        # voice that mostly holds -- which `delta` reads as silence.
        "change_rate": round(changes / max(1, total - 1), 4),
        # The longest stretch of identical values: what `stutter_repeat` hunts
        # for, and what makes the visuals band.
        "flat_longest": longest,
    }


@dataclass
class Stage:
    """One step of the pipeline, as it was observed."""

    index: int
    kind: str  # ingest | pedal | mapping
    name: str
    params: dict = field(default_factory=dict)
    channels: list[dict] = field(default_factory=list)
    #: Free-form, stage-specific: the conversion table for ingest, worked
    #: examples for the mapping, the LFSR period for xor_mask.
    detail: dict = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> dict:
        out = {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "channels": self.channels,
        }
        for key, value in (("params", self.params), ("detail", self.detail), ("note", self.note)):
            if value:
                out[key] = value
        return out


class Trace:
    """Collector. Passed down the pipeline; ``None`` means "do not record"."""

    def __init__(self, window: int = DEFAULT_WINDOW, label: str = ""):
        self.window = max(0, int(window))
        self.label = label
        self.stages: list[Stage] = []

    # -- recording -----------------------------------------------------------
    def add(self, kind: str, name: str, stream=None, **kwargs) -> Stage:
        stage = Stage(index=len(self.stages), kind=kind, name=name, **kwargs)
        if stream is not None:
            stage.channels = self._snapshot(stream)
        self.stages.append(stage)
        return stage

    def _snapshot(self, stream) -> list[dict]:
        out = []
        for index, name in enumerate(stream.names):
            values = stream.data[index]
            out.append(
                {
                    "name": name,
                    "stats": channel_stats(values, stream.bit_depth),
                    # A window, and labelled as one.
                    "values": values[: self.window],
                    "truncated": len(values) > self.window,
                }
            )
        return out

    def diff_from_previous(self, stage: Stage) -> None:
        """Annotate a stage with how much it actually changed.

        The most useful single number when reading a chain: a pedal that changed
        2% of values did almost nothing, whatever its parameters claim, and one
        that changed 100% has replaced the signal rather than shaped it.
        """
        if stage.index == 0:
            return
        before = self.stages[stage.index - 1]
        if len(before.channels) != len(stage.channels):
            return
        moved = same = 0
        for old, new in zip(before.channels, stage.channels):
            for a, b in zip(old["values"], new["values"]):
                if a == b:
                    same += 1
                else:
                    moved += 1
        total = moved + same
        if total:
            stage.detail["changed_fraction"] = round(moved / total, 4)
            stage.detail["entropy_delta"] = round(
                sum(c["stats"]["entropy"] for c in stage.channels)
                / max(1, len(stage.channels))
                - sum(c["stats"]["entropy"] for c in before.channels)
                / max(1, len(before.channels)),
                4,
            )

    # -- io ------------------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "label": self.label,
            "window": self.window,
            "stages": [stage.to_json() for stage in self.stages],
        }

    def describe(self) -> str:
        """Terminal rendering. The pipeline is a workshop; looking is cheap."""
        lines = [f"trace {self.label or '(unlabelled)'} -- {len(self.stages)} stages"]
        for stage in self.stages:
            head = f"  [{stage.index}] {stage.kind:<8} {stage.name}"
            if "changed_fraction" in stage.detail:
                head += f"  ({stage.detail['changed_fraction']:.0%} of values moved"
                delta = stage.detail.get("entropy_delta", 0)
                head += f", entropy {delta:+.2f} bits)"
            lines.append(head)
            for channel in stage.channels:
                stats = channel["stats"]
                lines.append(
                    f"       {channel['name'][:22]:<22} "
                    f"min={stats['min']:>3} max={stats['max']:>3} "
                    f"uniq={stats['unique']:>4} H={stats['entropy']:>5.2f} "
                    f"chg={stats['change_rate']:>5.1%} flat={stats['flat_longest']:>4}"
                )
        return "\n".join(lines)
