"""Pieces: the document Serrin works on.

Until now the central object was a *render* -- the pipeline produced two JSON
files and the browser played them, and a session was a note taken afterwards
about what you had found. That is the wrong way round for making an album. The
piece is the thing you work on; the render is an output of it, like a mixdown.

So this inverts the flow. A piece holds everything needed to *generate* itself,
plus the settings for playing it, and the render becomes an optional field
recording that it has been produced at least once.

**A piece is a folder, not a file.** The manifest is ``piece.json`` and
everything it references sits beside it by relative path:

    my-album/
      01-decay/
        piece.json        the manifest below
        data.csv          or a path to somewhere else
        samples/          audio the performance layer triggers
        out/              renders, disposable
      02-static/
      kit/                samples shared across the series

Two reasons, both practical. Samples cannot go in the JSON -- base64 audio
bloats the file and destroys the diff, which is exactly what breaks the version
control the author is meant to be doing themselves. And relative paths make the
folder portable: copy it, zip it, move it into an album, and it still resolves.

**Four blocks, and the split is the design.**

``source``
    What to ingest and how. A render input.

``preset``
    The pedal chain and mapping, in the schema the CLI already speaks. A render
    input. Deliberately not a new schema -- that lesson was learned once already.

``performance``
    The interpreted layer: which key plays what, which samples exist, what
    patterns have been written. *Not* a render input -- none of it touches the
    exported stream, because section 4.3 keeps the generated sound primitive.
    Samples live here and only here: the eight data voices stay oscillators.

``runtime``
    Live-only settings -- levels, mutes, visual toggles. Opaque to Python.

The first two decide what the pipeline produces; the last two decide what happens
on top of it. Keeping that boundary visible is what stops "load a piece" from
implying "and re-render it", which would be slow and usually not wanted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .chain import Chain
from .tempo import Tempo

FORMAT = "serrin-piece/1"

#: The previous format. A session is a piece that has already been rendered and
#: has no performance layer, so it loads without translation.
SESSION_FORMAT = "serrin-session/1"

MANIFEST = "piece.json"

#: Folders a piece owns, relative to its own directory.
SAMPLES_DIR = "samples"
RENDER_DIR = "out"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class PieceError(ValueError):
    pass


def slug(text: str, fallback: str = "piece") -> str:
    """Filesystem-safe name. Used for piece folders and sample ids."""
    cleaned = _SAFE_NAME.sub("-", str(text or "")).strip("-._")
    return (cleaned or fallback)[:60]


# ---------------------------------------------------------------------------
# the performance layer
# ---------------------------------------------------------------------------
#: Keys are stored by physical position (``KeyboardEvent.code``), never by the
#: character printed on them.
#:
#: This matters more than it looks. `event.key` depends on the layout, so a
#: keymap authored on a Spanish keyboard would land on different physical keys
#: on a US one -- and a piece is meant to be shareable. Position is also the
#: right model musically: a keyboard mapping is a *layout*, like a piano, not a
#: set of letters.
DEFAULT_KEYMAP_ROWS = (
    # Home row and the one above, which is where hands rest. Two rows gives an
    # octave and a half of scale degrees before anyone has to reach.
    ("KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyL"),
    ("KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI", "KeyO", "KeyP"),
)

BINDING_KINDS = ("note", "sample", "pattern", "degree")


def default_keymap(offsets: list[int], root: int = 48, span: int = 12) -> dict:
    """A playable starting layout: scale degrees across two rows.

    Degrees rather than fixed MIDI notes, so the map stays correct if the piece's
    scale changes -- which it does whenever the chain or the mapping changes.
    Binding to absolute pitches would silently go out of key.
    """
    if not offsets:
        offsets = [0, 2, 4, 5, 7, 9, 11]
    keymap: dict[str, dict] = {}
    degree = 0
    for row_index, row in enumerate(DEFAULT_KEYMAP_ROWS):
        for code in row:
            keymap[code] = {
                "kind": "degree",
                "degree": degree,
                # The upper row starts an octave higher, so both hands have range.
                "octave": row_index,
            }
            degree += 1
        degree = 0
    del root, span
    return keymap


@dataclass
class Sample:
    """One audio file the performance layer can trigger.

    Referenced, never embedded. ``path`` is relative to the piece folder so the
    whole thing stays portable, and a piece in an album can point at ``../kit``
    to share a sample without copying it.
    """

    id: str
    path: str
    label: str = ""
    gain: float = 1.0
    #: Trim, in seconds. Zero means "the whole file".
    start: float = 0.0
    duration: float = 0.0

    def to_json(self) -> dict:
        out = {"id": self.id, "path": self.path}
        for key, value, default in (
            ("label", self.label, ""),
            ("gain", self.gain, 1.0),
            ("start", self.start, 0.0),
            ("duration", self.duration, 0.0),
        ):
            if value != default:
                out[key] = value
        return out

    @staticmethod
    def from_json(raw: dict) -> "Sample":
        if not raw.get("id") or not raw.get("path"):
            raise PieceError(f"a sample needs an id and a path: {raw!r}")
        return Sample(
            id=str(raw["id"]),
            path=str(raw["path"]),
            label=raw.get("label", ""),
            gain=float(raw.get("gain", 1.0)),
            start=float(raw.get("start", 0.0)),
            duration=float(raw.get("duration", 0.0)),
        )


@dataclass
class Pattern:
    """A beat: hits on the tempo grid.

    Steps are indices into the piece's own subdivision, so a pattern inherits
    swing for free -- the grid that places a data frame places a pattern hit,
    and there is only one implementation of it.
    """

    id: str
    steps: int = 16
    #: ``[{"step": 0, "velocity": 1.0}, ...]`` -- sparse, because a beat is mostly
    #: silence and a dense array of zeros says less than a list of hits.
    hits: list[dict] = field(default_factory=list)
    #: What a hit triggers: ``{"kind": "sample", "sample": "kick"}`` or a note.
    target: dict = field(default_factory=dict)
    enabled: bool = True
    label: str = ""

    def to_json(self) -> dict:
        out = {"id": self.id, "steps": self.steps, "hits": self.hits, "target": self.target}
        if not self.enabled:
            out["enabled"] = False
        if self.label:
            out["label"] = self.label
        return out

    @staticmethod
    def from_json(raw: dict) -> "Pattern":
        if not raw.get("id"):
            raise PieceError(f"a pattern needs an id: {raw!r}")
        steps = int(raw.get("steps", 16))
        if steps < 1:
            raise PieceError(f"pattern {raw['id']!r} has {steps} steps")
        hits = []
        for hit in raw.get("hits") or []:
            step = int(hit.get("step", 0))
            if not 0 <= step < steps:
                raise PieceError(
                    f"pattern {raw['id']!r}: step {step} is outside its {steps} steps"
                )
            hits.append({"step": step, "velocity": float(hit.get("velocity", 1.0))})
        return Pattern(
            id=str(raw["id"]),
            steps=steps,
            hits=sorted(hits, key=lambda h: h["step"]),
            target=dict(raw.get("target") or {}),
            enabled=bool(raw.get("enabled", True)),
            label=raw.get("label", ""),
        )


@dataclass
class Performance:
    """Everything the author plays, as opposed to everything the data produces."""

    keymap: dict = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)
    #: Keyboard engine settings: mode, register, level, waveform.
    keyboard: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        out: dict = {}
        if self.keymap:
            out["keymap"] = self.keymap
        if self.samples:
            out["samples"] = [sample.to_json() for sample in self.samples]
        if self.patterns:
            out["patterns"] = [pattern.to_json() for pattern in self.patterns]
        if self.keyboard:
            out["keyboard"] = self.keyboard
        return out

    @staticmethod
    def from_json(raw: dict | None) -> "Performance":
        raw = raw or {}
        performance = Performance(
            keymap=dict(raw.get("keymap") or {}),
            samples=[Sample.from_json(s) for s in raw.get("samples") or []],
            patterns=[Pattern.from_json(p) for p in raw.get("patterns") or []],
            keyboard=dict(raw.get("keyboard") or {}),
        )
        performance.validate()
        return performance

    def validate(self) -> None:
        """Catch the mistakes that would otherwise fail silently at play time."""
        for code, binding in self.keymap.items():
            kind = binding.get("kind")
            if kind not in BINDING_KINDS:
                raise PieceError(
                    f"key {code}: unknown binding kind {kind!r}; "
                    f"use one of {BINDING_KINDS}"
                )
            if kind == "sample" and not self.sample(binding.get("sample")):
                # A key bound to a sample that is not in the piece would just do
                # nothing when pressed, with no clue why.
                raise PieceError(
                    f"key {code} is bound to sample {binding.get('sample')!r}, "
                    "which this piece does not have"
                )
            if kind == "pattern" and not self.pattern(binding.get("pattern")):
                raise PieceError(
                    f"key {code} is bound to pattern {binding.get('pattern')!r}, "
                    "which this piece does not have"
                )

        ids = [sample.id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise PieceError("two samples share an id")
        ids = [pattern.id for pattern in self.patterns]
        if len(ids) != len(set(ids)):
            raise PieceError("two patterns share an id")

        for pattern in self.patterns:
            if pattern.target.get("kind") == "sample" and not self.sample(
                pattern.target.get("sample")
            ):
                raise PieceError(
                    f"pattern {pattern.id!r} targets a sample this piece does not have"
                )

    def sample(self, sample_id) -> Sample | None:
        return next((s for s in self.samples if s.id == sample_id), None)

    def pattern(self, pattern_id) -> Pattern | None:
        return next((p for p in self.patterns if p.id == pattern_id), None)

    def describe(self) -> str:
        bits = []
        if self.keymap:
            bits.append(f"{len(self.keymap)} keys mapped")
        if self.samples:
            bits.append(f"{len(self.samples)} samples")
        if self.patterns:
            live = sum(1 for p in self.patterns if p.enabled)
            bits.append(f"{len(self.patterns)} patterns ({live} on)")
        return ", ".join(bits) or "nothing mapped yet"


# ---------------------------------------------------------------------------
# the piece
# ---------------------------------------------------------------------------
@dataclass
class Piece:
    name: str = "untitled"
    title: str = ""
    notes: str = ""
    source: dict = field(default_factory=dict)
    preset: dict = field(default_factory=dict)
    performance: Performance = field(default_factory=Performance)
    runtime: dict = field(default_factory=dict)
    #: Present once the piece has been rendered at least once. Absent is normal:
    #: a piece exists before it has been produced.
    render: dict = field(default_factory=dict)
    created_at: str | None = None
    saved_at: str | None = None
    #: Where this was loaded from, so relative paths can be resolved. Not saved.
    folder: Path | None = None

    # -- io -----------------------------------------------------------------
    @staticmethod
    def from_json(raw: dict, folder: Path | None = None) -> "Piece":
        declared = raw.get("format")
        if declared and declared not in (FORMAT, SESSION_FORMAT):
            raise PieceError(
                f"format {declared!r} is neither {FORMAT!r} nor {SESSION_FORMAT!r}"
            )

        # A session is a piece with no performance layer and its render fields
        # at the top level, so it loads by reshaping rather than by a separate
        # code path. One reader, two shapes.
        render = dict(raw.get("render") or {})
        if declared == SESSION_FORMAT or "streams" in raw:
            streams = raw.get("streams") or {}
            render = {
                "label": raw.get("label", ""),
                "fingerprint": raw.get("fingerprint", ""),
                "audio": streams.get("audio"),
                "visual": streams.get("visual"),
                "rendered_at": raw.get("saved_at"),
                **render,
            }

        piece = Piece(
            name=raw.get("name") or slug(raw.get("label") or render.get("label") or "untitled"),
            title=raw.get("title", ""),
            notes=raw.get("notes", ""),
            source=dict(raw.get("source") or {}),
            preset=dict(raw.get("preset") or {}),
            performance=Performance.from_json(raw.get("performance")),
            runtime=dict(raw.get("runtime") or {}),
            render=render,
            created_at=raw.get("created_at"),
            saved_at=raw.get("saved_at"),
            folder=Path(folder) if folder else None,
        )
        # Validated eagerly: a bad pedal name should fail on open, not halfway
        # through a render.
        piece.chain()
        return piece

    @staticmethod
    def load(path: str | Path) -> "Piece":
        """Load a piece from its folder, or from its manifest directly."""
        target = Path(path)
        folder = target if target.is_dir() else target.parent
        manifest = target / MANIFEST if target.is_dir() else target
        if not manifest.exists():
            raise PieceError(f"no {MANIFEST} in {folder}")
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PieceError(f"{manifest} is not valid JSON: {exc}") from exc
        piece = Piece.from_json(raw, folder)
        if piece.name in ("untitled", ""):
            piece.name = folder.name
        return piece

    def to_json(self) -> dict:
        out = {
            "format": FORMAT,
            "name": self.name,
            "title": self.title,
            "notes": self.notes,
            "created_at": self.created_at,
            "saved_at": self.saved_at,
            "source": self.source,
            "preset": self.preset,
        }
        performance = self.performance.to_json()
        if performance:
            out["performance"] = performance
        if self.runtime:
            out["runtime"] = self.runtime
        if self.render:
            out["render"] = self.render
        return out

    def save(self, folder: str | Path | None = None) -> Path:
        target = Path(folder) if folder else self.folder
        if target is None:
            raise PieceError("nowhere to save: pass a folder")
        target.mkdir(parents=True, exist_ok=True)
        self.folder = target
        manifest = target / MANIFEST
        # Indented, always. This file is meant to be read in a diff -- that is
        # the whole reason samples are referenced rather than embedded.
        manifest.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        return manifest

    # -- paths ---------------------------------------------------------------
    def resolve(self, relative: str | Path) -> Path:
        """Resolve a piece-relative path against the piece's folder.

        Absolute paths pass through: a piece may legitimately point at a CSV
        living somewhere else on the machine. Everything the piece *owns* --
        samples, renders -- is relative, so the folder stays portable.
        """
        candidate = Path(relative)
        if candidate.is_absolute():
            return candidate
        if self.folder is None:
            return candidate
        return (self.folder / candidate).resolve()

    @property
    def samples_dir(self) -> Path:
        return self.resolve(SAMPLES_DIR)

    @property
    def render_dir(self) -> Path:
        return self.resolve(RENDER_DIR)

    def missing_samples(self) -> list[Sample]:
        """Samples whose files are not where the manifest says.

        Reported rather than raised: a piece with a missing sample is still worth
        opening, and the author needs to know which one to go and find.
        """
        return [s for s in self.performance.samples if not self.resolve(s.path).exists()]

    # -- the render layer ---------------------------------------------------
    @property
    def kind(self) -> str:
        return self.source.get("kind", "csv")

    @property
    def source_path(self) -> Path:
        path = self.source.get("path")
        if not path:
            raise PieceError(f"piece {self.name!r} has no source.path")
        return self.resolve(path)

    def chain(self) -> Chain:
        return Chain.from_json(self.preset or {"name": self.name, "chain": []})

    def tempo(self) -> Tempo | None:
        for candidate in (
            self.source.get("tempo"),
            (self.preset.get("ingest") or {}).get("tempo"),
        ):
            if candidate:
                return Tempo.parse(candidate)
        return None

    def ingest_kwargs(self) -> dict:
        """Keyword arguments for whichever adapter this piece's source needs."""
        source = self.source
        if self.kind == "git":
            mapping = {
                "metric": source.get("metric"),
                "traversal": source.get("traversal"),
                "branch_names": source.get("branches"),
                "bit_depth": source.get("bit_depth"),
                "limit": source.get("limit"),
                "tempo": self.tempo(),
            }
        else:
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
    def rendered(self) -> bool:
        return bool(self.render.get("fingerprint"))

    def describe(self) -> str:
        lines = [f"piece {self.name}" + (f" — {self.title}" if self.title else "")]
        lines.append(f"  source       {self.source.get('path', '(none)')}  ({self.kind})")
        grid = self.tempo()
        if grid:
            lines.append(f"  tempo        {grid.describe()}")
        chain = self.chain()
        lines.append(f"  chain        {chain.name} ({len(chain.slots)} pedals)")
        lines.append(f"  performance  {self.performance.describe()}")
        if self.rendered:
            lines.append(
                f"  render       {self.render.get('fingerprint')} "
                f"({self.render.get('rendered_at') or 'unknown date'})"
            )
        else:
            lines.append("  render       not yet generated")
        missing = self.missing_samples()
        if missing:
            lines.append(
                f"  MISSING      {len(missing)} sample file(s): "
                + ", ".join(s.path for s in missing)
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# projects: a folder of pieces
# ---------------------------------------------------------------------------
def new_piece(
    folder: str | Path,
    name: str | None = None,
    source: dict | None = None,
    preset: dict | None = None,
    stamp: str | None = None,
) -> Piece:
    """Create a piece folder with a manifest and the directories it owns."""
    target = Path(folder)
    if (target / MANIFEST).exists():
        raise PieceError(f"{target} already holds a piece")
    piece = Piece(
        name=slug(name or target.name),
        source=dict(source or {}),
        preset=dict(preset or {}),
        created_at=stamp,
        saved_at=stamp,
        folder=target,
    )
    piece.save(target)
    (target / SAMPLES_DIR).mkdir(exist_ok=True)
    return piece


def list_pieces(root: str | Path) -> list[dict]:
    """Every piece under ``root``, one level down. The album view.

    Returns summaries rather than loaded pieces: listing a folder should not pay
    to validate every chain in it, and a piece that fails to load must not stop
    the others from being listed -- so a broken one is reported as broken.
    """
    base = Path(root)
    if not base.exists():
        return []
    found = []
    for manifest in sorted(base.glob(f"*/{MANIFEST}")):
        entry = {"name": manifest.parent.name, "folder": str(manifest.parent)}
        try:
            piece = Piece.load(manifest.parent)
        except (PieceError, OSError) as exc:
            entry.update({"ok": False, "error": str(exc)})
            found.append(entry)
            continue
        entry.update(
            {
                "ok": True,
                "name": piece.name,
                "title": piece.title,
                "notes": piece.notes,
                "kind": piece.kind,
                "source": piece.source.get("path"),
                "chain": piece.chain().name,
                "tempo": (piece.tempo().to_json() if piece.tempo() else None),
                "performance": piece.performance.describe(),
                "rendered": piece.rendered,
                "fingerprint": piece.render.get("fingerprint", ""),
                "saved_at": piece.saved_at,
                "missing_samples": [s.path for s in piece.missing_samples()],
            }
        )
        found.append(entry)
    return found
