"""The pedalboard: an ordered list of (pedal, params), plus its seed policy.

Two things matter here beyond "call the pedals in order".

**Seed derivation.** Each pedal gets its own substream keyed by
``"<index>/<pedal>"``. Consequence: reordering a chain changes every downstream
pedal's randomness deterministically, rather than accidentally, and disabling
pedal 3 does not reshuffle what pedal 4 hears.

**Enable flags.** A pedal can be present but off. This is what lets the
intensity envelope (section 5.1) switch pedals on progressively instead of the
author maintaining several near-identical presets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import pedals
from .ingest import auto_seed
from .rng import Rng, seed_from_bytes
from .stream import Stream


class ChainError(ValueError):
    pass


@dataclass
class Slot:
    """One pedal on the board."""

    pedal: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    #: Intensity at which this pedal switches on (section 5.1). 0.0 = always on.
    #: The envelope reads this; a plain offline render ignores it.
    at_intensity: float = 0.0

    def to_json(self) -> dict:
        out: dict = {"pedal": self.pedal, "params": self.params}
        if not self.enabled:
            out["enabled"] = False
        if self.at_intensity:
            out["at_intensity"] = self.at_intensity
        return out

    @staticmethod
    def from_json(raw: dict) -> "Slot":
        if "pedal" not in raw:
            raise ChainError(f"chain entry missing 'pedal': {raw!r}")
        pedals.get(raw["pedal"])  # fail loudly at load time, not mid-render
        return Slot(
            pedal=raw["pedal"],
            params=dict(raw.get("params") or {}),
            enabled=bool(raw.get("enabled", True)),
            at_intensity=float(raw.get("at_intensity", 0.0) or 0.0),
        )


@dataclass
class Chain:
    name: str = "untitled"
    slots: list[Slot] = field(default_factory=list)
    seed_mode: str = "auto"  # auto | fixed
    seed_override: int | None = None
    #: Optional preset-level ingestion and mapping hints; the CLI lets flags win.
    ingest: dict = field(default_factory=dict)
    mapping: dict = field(default_factory=dict)
    envelope: dict = field(default_factory=dict)
    #: Piece-level defaults: mode (closed|endless), loop policy, voice entry
    #: order. CLI flags win over these; these win over the built-in defaults.
    piece: dict = field(default_factory=dict)
    notes: str = ""

    # -- io -----------------------------------------------------------------
    @staticmethod
    def from_json(raw: dict) -> "Chain":
        chain = Chain(
            name=raw.get("name", "untitled"),
            slots=[Slot.from_json(entry) for entry in raw.get("chain", [])],
            seed_mode=raw.get("seed_mode", "auto"),
            seed_override=raw.get("seed_override"),
            ingest=dict(raw.get("ingest") or {}),
            mapping=dict(raw.get("mapping") or {}),
            envelope=dict(raw.get("envelope") or {}),
            piece=dict(raw.get("piece") or {}),
            notes=raw.get("notes", ""),
        )
        if chain.seed_mode not in ("auto", "fixed"):
            raise ChainError(f"seed_mode must be 'auto' or 'fixed', got {chain.seed_mode!r}")
        if chain.seed_mode == "fixed" and chain.seed_override is None:
            raise ChainError("seed_mode 'fixed' needs a seed_override")
        return chain

    @staticmethod
    def load(path: str | Path) -> "Chain":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ChainError(f"{path} is not valid JSON: {exc}") from exc
        chain = Chain.from_json(raw)
        if chain.name == "untitled":
            chain.name = path.stem
        return chain

    def to_json(self) -> dict:
        out: dict = {
            "name": self.name,
            "seed_mode": self.seed_mode,
            "seed_override": self.seed_override,
            "chain": [slot.to_json() for slot in self.slots],
        }
        for key in ("ingest", "mapping", "envelope", "piece"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.notes:
            out["notes"] = self.notes
        return out

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    # -- seeds --------------------------------------------------------------
    def resolve_seed(self, source: str | Path | None = None) -> int:
        """``fixed`` wins; ``auto`` hashes the head of the source; else the name.

        The dispatch on source kind lives here rather than in each caller. It
        started out in the CLI, which meant every *other* caller -- the upload
        endpoint, a session re-render -- silently tried to read a repository
        directory as a CSV and died on a PermissionError. One place that knows
        how to seed from "whatever the source is" is the fix; three callers each
        remembering to special-case it is not.
        """
        if self.seed_override is not None:
            return int(self.seed_override) & ((1 << 64) - 1)
        if self.seed_mode == "auto" and source is not None:
            if Path(source).is_dir():
                # Imported here: chain.py is on the import path of everything,
                # and the git adapter shells out to git.
                from .ingest_git import auto_seed_repo

                return auto_seed_repo(source)
            return auto_seed(source)
        return seed_from_bytes(self.name.encode("utf-8"))

    # -- the actual work ----------------------------------------------------
    def apply(
        self,
        stream: Stream,
        seed: int | None = None,
        source: str | Path | None = None,
        intensity: float = 1.0,
        trace: bool = False,
        recorder=None,
    ) -> Stream:
        """Run the enabled pedals in order.

        ``intensity`` gates slots by their ``at_intensity`` threshold, which is
        how a single preset can render as "intro" or "climax" (section 5.1). The
        default of 1.0 means "everything the author enabled".

        ``trace`` prints a line per stage; ``recorder`` is a ``Trace`` that keeps
        the values. They are separate because the first is for watching a render
        go by and the second is for reading it afterwards.
        """
        if seed is None:
            seed = self.resolve_seed(source)
        current = stream

        if recorder is not None and not recorder.stages:
            # A chain applied to a stream nobody traced still needs a baseline,
            # or the first pedal has nothing to be a difference from.
            recorder.add("ingest", "input stream", current)
        applied: list[dict] = []

        for index, slot in enumerate(self.slots):
            if not slot.enabled or intensity < slot.at_intensity:
                continue
            pedal = pedals.get(slot.pedal)
            # Label includes the index: the same pedal twice on one board gets
            # two different substreams, and reordering is a real change.
            sub = Rng(seed ^ seed_from_bytes(f"{index}/{slot.pedal}".encode("utf-8")))
            before = current
            current = pedal(current, slot.params, sub)
            if current.n_voices != before.n_voices:
                raise ChainError(
                    f"pedal {slot.pedal!r} changed the voice count "
                    f"({before.n_voices} -> {current.n_voices}); pedals must not"
                )
            applied.append({"index": index, "pedal": slot.pedal, "params": slot.params})
            if recorder is not None:
                stage = recorder.add(
                    "pedal",
                    slot.pedal,
                    current,
                    params=dict(slot.params),
                    detail={
                        k: v
                        for k, v in (("lfsr", current.meta.get("lfsr")),
                                     ("quantized_scale", current.meta.get("quantized_scale")))
                        if v
                    },
                    note=(
                        "length changed: this is the one pedal that may stall the "
                        "stream" if current.length != before.length else ""
                    ),
                )
                recorder.diff_from_previous(stage)
            if trace:
                print(f"  [{index}] {slot.pedal:<16} -> {current.describe().splitlines()[0]}")

        result = current.copy()
        result.meta.update({"seed": seed, "chain_name": self.name, "chain_applied": applied})
        return result

    def describe(self) -> str:
        lines = [f"chain {self.name!r} ({len(self.slots)} pedals, seed_mode={self.seed_mode})"]
        for index, slot in enumerate(self.slots):
            flag = "+" if slot.enabled else "-"
            gate = f"  @{slot.at_intensity:.2f}" if slot.at_intensity else ""
            params = ", ".join(f"{k}={v!r}" for k, v in slot.params.items())
            lines.append(f" [{flag}] {index}. {slot.pedal:<16} {params}{gate}")
        return "\n".join(lines)


def default_chain() -> Chain:
    """The doc's own example (section 3.4), used when no preset is given."""
    return Chain(
        name="gritty_01",
        slots=[
            Slot("delta", {"order": 1}),
            Slot("xor_mask", {"mask_source": "lfsr", "taps": [3, 1]}),
            Slot("bitcrush", {"target_bits": 4}),
            Slot("caesar", {"shift": 5, "shift_lfo": "sine:0.1hz"}),
        ],
    )
