"""Sessions: keeping what you found.

A piece was already reproducible as ``source + chain + seed``. What was *not*
reproducible was everything the author changes while listening -- tempo, swing,
a hand-drawn envelope, register, balance, mutes, crush. All of it was lost on
reload, which meant the panel was a place to discover settings you could not
keep. This is the fix.

The file has three parts, and the split is the whole design:

``source``
    What was ingested, and how. Enough to re-read the CSV identically.

``preset``
    Exactly the preset schema the CLI already accepts -- chain, seed policy,
    ingest hints, mapping, envelope, piece settings. Deliberately *not* a second
    schema: a session's render layer IS a preset, so it can be lifted straight
    out and handed to ``render -c``.

``runtime``
    Everything that only exists while the piece is playing: master level,
    filter cutoff, mutes, visual toggles, keyboard settings, playback speed.
    Python does not interpret this -- it has no offline equivalent, because
    there is no offline. It is preserved byte-for-byte on a round trip so the
    browser gets back exactly what it saved.

That last part is the honest boundary of the format. Reloading a session
restores what you were hearing; re-rendering from it restores what the pipeline
would produce. Those are different things, and the file says which is which.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .chain import Chain
from .tempo import Tempo

FORMAT = "serrin-session/1"


class SessionError(ValueError):
    pass


@dataclass
class Session:
    #: Ingestion inputs: path, columns, granularity, aggregation, bit depth...
    source: dict = field(default_factory=dict)
    #: The render layer, in the preset schema the CLI already speaks.
    preset: dict = field(default_factory=dict)
    #: Live-only settings. Opaque here, by design.
    runtime: dict = field(default_factory=dict)
    #: Where the rendered pair lived, so a reload can find it again.
    streams: dict = field(default_factory=dict)
    label: str = ""
    fingerprint: str = ""
    saved_at: str | None = None
    notes: str = ""

    # -- io -----------------------------------------------------------------
    @staticmethod
    def from_json(raw: dict) -> "Session":
        declared = raw.get("format")
        if declared and declared != FORMAT:
            # Refuse rather than guess: a session from a future format would
            # half-apply and leave the author wondering what was ignored.
            raise SessionError(
                f"session format {declared!r} is not {FORMAT!r}; "
                "re-save it from a matching build"
            )
        session = Session(
            source=dict(raw.get("source") or {}),
            preset=dict(raw.get("preset") or {}),
            runtime=dict(raw.get("runtime") or {}),
            streams=dict(raw.get("streams") or {}),
            label=raw.get("label", ""),
            fingerprint=raw.get("fingerprint", ""),
            saved_at=raw.get("saved_at"),
            notes=raw.get("notes", ""),
        )
        if not session.source.get("path"):
            raise SessionError("session has no source.path -- nothing to re-render")
        # Validated eagerly: a bad pedal name should fail on load, not halfway
        # through a render.
        session.chain()
        return session

    @staticmethod
    def load(path: str | Path) -> "Session":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionError(f"{path} is not valid JSON: {exc}") from exc
        return Session.from_json(raw)

    def to_json(self) -> dict:
        return {
            "format": FORMAT,
            "label": self.label,
            "fingerprint": self.fingerprint,
            "saved_at": self.saved_at,
            "notes": self.notes,
            "source": self.source,
            "preset": self.preset,
            "runtime": self.runtime,
            "streams": self.streams,
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    # -- the render layer ---------------------------------------------------
    def chain(self) -> Chain:
        """The preset block as a Chain. Raises if the chain is not loadable."""
        return Chain.from_json(self.preset or {"name": "session", "chain": []})

    def tempo(self) -> Tempo | None:
        """The grid, from the source block or the preset's ingest hints."""
        for candidate in (self.source.get("tempo"), (self.preset.get("ingest") or {}).get("tempo")):
            if candidate:
                return Tempo.parse(candidate)
        return None

    def ingest_kwargs(self) -> dict:
        """Keyword arguments for ``ingest_csv``, straight from the source block.

        Only keys that were actually recorded are returned, so ingestion's own
        defaults still apply to anything the session did not pin down.
        """
        source = self.source
        mapping = {
            "columns": source.get("columns"),
            "bit_depth": source.get("bit_depth"),
            "granularity": source.get("granularity"),
            "aggregation": source.get("aggregation"),
            "log_scale": source.get("log_scale"),
            "limit": source.get("limit"),
            "tempo": self.tempo(),
        }
        return {key: value for key, value in mapping.items() if value not in (None, [])}

    @property
    def path(self) -> str:
        return self.source["path"]

    def describe(self) -> str:
        chain = self.chain()
        lines = [
            f"session {self.label or '(unlabelled)'}",
            f"  saved      {self.saved_at or 'unknown'}",
            f"  source     {self.path}",
            f"  columns    {self.source.get('columns') or 'auto'}",
        ]
        grid = self.tempo()
        if grid:
            lines.append(f"  tempo      {grid.describe()}")
        lines.append(f"  chain      {chain.name} ({len(chain.slots)} pedals)")
        if self.fingerprint:
            lines.append(f"  render     {self.fingerprint}")
        if self.runtime:
            # Named rather than dumped: the point is to show that live settings
            # survived, not to print forty numbers.
            lines.append(f"  runtime    {len(self.runtime)} groups: {', '.join(sorted(self.runtime))}")
        if self.streams:
            lines.append(f"  streams    {self.streams.get('audio')} + {self.streams.get('visual')}")
        return "\n".join(lines)


def promote_to_preset(session: Session, name: str | None = None) -> dict:
    """Lift the render layer out as a standalone preset.

    The inverse of the usual direction of travel: normally a preset is authored
    by hand and rendered, but tuning by ear in the browser and *then* freezing
    the result is the more useful workflow -- section 3.4's "lock/freeze pedal
    chains that work".

    What is dropped is exactly the runtime block, because none of it has an
    offline meaning. The caller should say so rather than implying the preset is
    the whole session.
    """
    preset = dict(session.preset)
    if name:
        preset["name"] = name
    preset.setdefault("name", session.label or "from_session")

    # Fold the session's own ingestion choices into the preset's ingest hints,
    # so `render -c preset.json` on the same CSV reproduces the render without
    # needing the session file too.
    ingest = dict(preset.get("ingest") or {})
    for key in ("columns", "granularity", "aggregation", "bit_depth", "log_scale"):
        value = session.source.get(key)
        if value not in (None, []):
            ingest[key] = value
    grid = session.tempo()
    if grid:
        ingest["tempo"] = grid.to_json()
    if ingest:
        preset["ingest"] = ingest

    note = f"frozen from session {session.label}" if session.label else "frozen from a session"
    preset["notes"] = f"{preset.get('notes', '')}\n{note}".strip()
    return preset
