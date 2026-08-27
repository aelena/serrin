"""The offline workshop, as a command line.

Section 3.6 asks for a minimal CLI and section 7 says loose scripts are fine at
this stage. This is a middle path: one entry point with subcommands, because the
workshop needs more than "render" -- it needs to look at things (``inspect``,
``catalog``, ``curve``) without leaving the terminal.

    python -m serrin render --input data.csv --chain presets/gritty_01.json \
        --out-audio out/stream_audio.json --out-visual out/stream_visual.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pedals
from .chain import Chain, default_chain
from .envelope import ARCHETYPES, EQUATIONS, Envelope, voice_order
from .export import MappingConfig, build_render, trace_mapping, write_json
from .ingest import IngestError, ingest_csv
from .graph import (
    GraphError,
    auto_seed_graph,
    describe_graph,
    export_graph,
    graph_facts,
    ingest_graph,
    load_graph,
    validate_graph,
)
from .ingest_git import (
    METRICS,
    TRAVERSALS,
    GitError,
    auto_seed_repo,
    describe_repo,
    ingest_repo,
)
from .piece import MANIFEST, Piece, PieceError, list_pieces, new_piece, slug
from .scales import SCALE_BANK, resolve
from .session import Session, SessionError, promote_to_preset
from .stream import MAX_VOICES
from .trace import DEFAULT_WINDOW, Trace
from .tempo import NOTE_FRACTIONS, SUBDIVISIONS, Tempo, TempoError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _split_columns(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_chain(path: str | None) -> Chain:
    if not path:
        return default_chain()
    return Chain.load(path)


def _envelope_from_args(args, chain: Chain) -> Envelope:
    if args.mode == "endless" and not args.envelope:
        # Mode B has no imposed arc (section 5.1-B).
        return Envelope.constant(1.0)
    if args.envelope:
        return Envelope.from_spec(args.envelope)
    if args.archetype:
        return Envelope.from_archetype(args.archetype, args.curvature)
    if args.envelope_points:
        points = json.loads(Path(args.envelope_points).read_text(encoding="utf-8"))
        if isinstance(points, dict):
            points = points.get("points", points.get("curve"))
        if points and not isinstance(points[0], (list, tuple)):
            # A bare list of intensities: assume even spacing.
            points = [(i / max(1, len(points) - 1), v) for i, v in enumerate(points)]
        return Envelope.from_points(points)
    if chain.envelope:
        return Envelope.from_spec(chain.envelope)
    return Envelope.constant(1.0)


def _read_source(args, ingest_opts: dict, source: str, kind: str, recorder=None):
    """Ingest from wherever the source lives. Everything downstream is identical.

    This function is the entire cost of section 6.3's claim that a commit graph
    "fits as an ingestion adapter, not a separate system" -- the branch below is
    the only place in serrin that knows there is more than one kind of source.

    Flags are read with getattr because `render` and `inspect` share this
    function and do not offer the same set: inspect has no --aggregation, and
    reaching for it directly makes the shared helper depend on which subcommand
    happened to call it.
    """
    def flag(name, default=None):
        return getattr(args, name, None) or default
    if kind == "graph":
        stream = ingest_graph(
            source,
            metric=flag("metric", ingest_opts.get("metric", "hash")),
            traversal=flag("traversal") or ingest_opts.get("traversal"),
            branch_names=_split_columns(flag("branches")) or ingest_opts.get("branches"),
            bit_depth=flag("bit_depth", ingest_opts.get("bit_depth", 8)),
            tempo=_tempo_from_args(args, ingest_opts),
            limit=flag("limit"),
        )
        if recorder is not None:
            recorder.add(
                "ingest",
                f"read {Path(source).name} (history file)",
                stream,
                params={
                    "metric": stream.meta["git"]["metric"],
                    "traversal": stream.meta["git"]["traversal"],
                },
                detail=dict(stream.meta["git"]),
                note=(
                    "an exported history: ownership was decided when the file was "
                    "written, since it needs the repository to compute"
                ),
            )
        return stream
    if kind == "git":
        stream = ingest_repo(
            source,
            metric=flag("metric", ingest_opts.get("metric", "hash")),
            traversal=flag("traversal", ingest_opts.get("traversal", "chrono")),
            branch_names=_split_columns(flag("branches")) or ingest_opts.get("branches"),
            bit_depth=flag("bit_depth", ingest_opts.get("bit_depth", 8)),
            tempo=_tempo_from_args(args, ingest_opts),
            limit=flag("limit"),
        )
        if recorder is not None:
            # The graph adapter has no cell-to-byte table to show; what it has
            # instead is branch ownership, which is the equivalent question.
            recorder.add(
                "ingest",
                f"read {Path(source).name} (git)",
                stream,
                params={"metric": stream.meta["git"]["metric"],
                        "traversal": stream.meta["git"]["traversal"]},
                detail=dict(stream.meta["git"]),
                note=(
                    "each branch takes a value at its own commits and holds "
                    "between them, so a quiet branch becomes a quiet voice"
                ),
            )
        return stream
    return ingest_csv(
        source,
        columns=_split_columns(flag("columns")) or ingest_opts.get("columns"),
        bit_depth=flag("bit_depth", ingest_opts.get("bit_depth", 8)),
        granularity=flag("granularity", ingest_opts.get("granularity", 1)),
        aggregation=flag("aggregation", ingest_opts.get("aggregation", "mean")),
        tempo=_tempo_from_args(args, ingest_opts),
        log_scale=flag("log_scale", bool(ingest_opts.get("log_scale", False))),
        limit=flag("limit"),
        trace=recorder,
    )


def _piece_relative(piece: Piece, path: Path) -> str:
    """Store paths relative to the piece folder, so it stays portable."""
    try:
        return str(Path(path).resolve().relative_to(piece.folder.resolve())).replace(
            "\\", "/"
        )
    except (ValueError, AttributeError):
        # Rendered somewhere outside the folder: keep the absolute path rather
        # than an unresolvable relative one.
        return str(path).replace("\\", "/")


def _tempo_from_args(args, ingest_opts: dict) -> Tempo:
    """Precedence: CLI tempo > CLI rate > preset tempo > preset rate > default.

    Both units describe the same grid, so whichever the author reached for last
    wins outright rather than being merged -- half a tempo from the flags and
    half from the preset would be nobody's intent.
    """
    if getattr(args, "tempo", None):
        grid = Tempo.parse(args.tempo)
    elif getattr(args, "rate", None):
        grid = Tempo.from_rate(args.rate)
    elif ingest_opts.get("tempo"):
        grid = Tempo.parse(ingest_opts["tempo"])
    elif ingest_opts.get("rate"):
        grid = Tempo.from_rate(ingest_opts["rate"])
    else:
        grid = Tempo()

    # The finer-grained flags refine whatever came back, so `--swing 0.3` alone
    # is meaningful without having to restate the tempo.
    overrides = {}
    if getattr(args, "subdivision", None):
        overrides["subdivision"] = args.subdivision
    if getattr(args, "swing", None) is not None:
        overrides["swing"] = args.swing
    if getattr(args, "beats_per_bar", None):
        overrides["beats_per_bar"] = args.beats_per_bar
    if overrides:
        grid = Tempo(
            bpm=overrides.get("bpm", grid.bpm),
            subdivision=overrides.get("subdivision", grid.subdivision),
            swing=overrides.get("swing", grid.swing),
            beats_per_bar=overrides.get("beats_per_bar", grid.beats_per_bar),
        )
    return grid


def _mapping_from_args(args, chain: Chain) -> MappingConfig:
    cfg = MappingConfig.from_json(chain.mapping)
    if args.scale:
        cfg.quantize_to = args.scale
    if args.note_low is not None:
        cfg.note_low = args.note_low
    if args.note_high is not None:
        cfg.note_high = args.note_high
    if args.freq_curve:
        cfg.freq_curve = args.freq_curve
    return cfg


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def cmd_render(args) -> int:
    # A session carries both halves -- what was ingested and how it was
    # transformed -- so it stands in for --input and --chain together. Explicit
    # flags still win, which is what makes "re-render that session but faster"
    # a one-flag change rather than an edit.
    # A piece supersedes both --input and --chain: it holds the source and the
    # chain together, which is the whole point of it being the document.
    piece = Piece.load(args.piece) if args.piece else None
    if piece is not None:
        chain = piece.chain()
        source = args.repo or args.graph or args.input or piece.source_path
        kind = "git" if args.repo else ("graph" if args.graph else piece.kind)
        overrides = piece.ingest_kwargs()
        session = None
    elif args.session:
        session = Session.load(args.session)
    else:
        session = None

    if piece is not None:
        pass
    elif session is not None:
        chain = session.chain()
        source = args.repo or args.input or session.path
        kind = "git" if args.repo else session.source.get("kind", "csv")
        overrides = session.ingest_kwargs()
    else:
        chain = _load_chain(args.chain)
        source = args.repo or args.graph or args.input
        kind = "git" if args.repo else ("graph" if args.graph else "csv")
        overrides = {}

    if not source:
        raise IngestError(
            "nothing to render: pass --piece, --input, --repo or --session"
        )

    # A piece renders into its own folder unless told otherwise, so the outputs
    # travel with the document rather than piling up in a shared out/.
    if piece is not None:
        piece.render_dir.mkdir(parents=True, exist_ok=True)
        if args.out_audio == "out/stream_audio.json":
            args.out_audio = str(piece.render_dir / "audio.json")
        if args.out_visual == "out/stream_visual.json":
            args.out_visual = str(piece.render_dir / "visual.json")

    ingest_opts = {**dict(chain.ingest), **overrides}
    if args.chain and session is not None:
        # An explicit --chain alongside --session means "that data, this chain".
        chain = _load_chain(args.chain)

    recorder = (
        Trace(window=args.trace_window or DEFAULT_WINDOW, label=str(source))
        if (args.trace or args.verbose)
        else None
    )
    stream = _read_source(args, ingest_opts, source, kind, recorder)
    columns = stream.names
    args.input = source

    # Chain.resolve_seed dispatches on the source kind itself: the CSV rule is
    # "hash the first N rows", and the graph equivalent is the first N commit
    # ids, so the seed still changes when the head of the data changes.
    seed = args.seed if args.seed is not None else chain.resolve_seed(source)

    if kind == "git":
        print(describe_repo(stream))
    elif kind == "graph":
        print(describe_graph(stream))
    if args.verbose:
        print(f"ingested {stream.describe()}")
        print(chain.describe())
        print(f"seed {seed}")

    transformed = chain.apply(
        stream, seed=seed, source=source, trace=args.verbose, recorder=recorder
    )
    envelope = _envelope_from_args(args, chain)
    mapping = _mapping_from_args(args, chain)

    # Precedence: explicit CLI flag > preset "piece" block > built-in default.
    mode = args.mode or chain.piece.get("mode", "closed")
    loop_policy = args.loop or chain.piece.get("loop", "vary")
    voice_entry = args.voice_entry or chain.piece.get("voice_entry", "variance")

    rendered = build_render(
        transformed,
        chain=chain,
        envelope=envelope,
        config=mapping,
        mode=mode,
        loop_policy=loop_policy,
        voice_entry=voice_entry,
        delay_note=args.delay_note or chain.piece.get("delay_note", "1/8."),
    )

    if recorder is not None:
        trace_mapping(transformed, rendered, recorder)

    out_audio = Path(args.out_audio)
    out_visual = Path(args.out_visual)
    audio_bytes = write_json(out_audio, rendered.audio_document(), compact=not args.pretty)
    visual_bytes = write_json(
        out_visual, rendered.visual_document(), compact=not args.pretty
    )

    print(f"label       {rendered.meta['label']}")
    print(f"seed        {seed}")
    print(f"fingerprint {rendered.meta['fingerprint']}")
    print(f"tempo       {transformed.tempo.describe()}")
    print(
        f"stream      {transformed.n_voices} voices x {transformed.length} frames "
        f"= {transformed.duration:.1f}s / {transformed.bars:.1f} bars"
    )
    if "lfsr" in transformed.meta:
        lfsr = transformed.meta["lfsr"]
        kind = "maximal" if lfsr["maximal"] else "short"
        print(
            f"lfsr        taps {lfsr['taps']} -- {kind} period "
            f"{lfsr['period_frames']} frames ({lfsr['period_seconds']}s)"
        )
    print(f"mode        {mode} / loop={loop_policy} / entry={voice_entry}")
    print(f"audio  ->   {out_audio}  ({audio_bytes / 1024:.1f} KiB)")
    print(f"visual ->   {out_visual}  ({visual_bytes / 1024:.1f} KiB)")

    if piece is not None and not args.no_save:
        # The piece records that it has been produced. This is the only field the
        # pipeline writes back, which keeps "render" from quietly editing the
        # author's configuration.
        piece.render = {
            "label": rendered.meta["label"],
            "fingerprint": rendered.meta["fingerprint"],
            "seed": rendered.meta["seed"],
            "audio": _piece_relative(piece, out_audio),
            "visual": _piece_relative(piece, out_visual),
            "frames": rendered.meta["frames"],
            "duration": rendered.meta["duration"],
            "voices": rendered.meta["voices"],
            "rendered_at": args.stamp,
        }
        manifest = piece.save()
        print(f"piece ->    {manifest}")

    if args.out_session:
        # The runtime block is carried over from an input session if there was
        # one: a re-render should not silently discard the author's live
        # settings just because the pipeline ran again.
        source_block = {
            "path": str(source),
            "kind": kind,
            "columns": columns,
            "bit_depth": stream.bit_depth,
            "limit": args.limit,
            "tempo": stream.tempo.to_json(),
        }
        if kind == "git":
            # A graph's inputs are metric/traversal/branches, and without them a
            # re-render would silently fall back to the defaults -- producing a
            # different piece from a file that claims to describe this one.
            git_meta = stream.meta.get("git", {})
            source_block.update(
                {
                    "metric": git_meta.get("metric"),
                    "traversal": git_meta.get("traversal"),
                    "branches": git_meta.get("branches"),
                }
            )
        else:
            source_block.update(
                {
                    "granularity": stream.meta.get("granularity", 1),
                    "aggregation": stream.meta.get("aggregation"),
                    "log_scale": stream.meta.get("log_scale", False),
                }
            )

        saved = Session(
            source=source_block,
            preset=chain.to_json(),
            runtime=dict(session.runtime) if session else {},
            streams={"audio": str(out_audio), "visual": str(out_visual)},
            label=rendered.meta["label"],
            fingerprint=rendered.meta["fingerprint"],
            saved_at=session.saved_at if session else None,
        )
        saved.save(args.out_session)
        print(f"session ->  {args.out_session}")
    if args.trace:
        write_json(args.trace, recorder.to_json(), compact=False)
        print(f"trace ->    {args.trace}")
    if args.verbose and recorder is not None:
        print()
        print(recorder.describe())
        print("\nintensity envelope:")
        print(envelope.ascii())
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
def cmd_inspect(args) -> int:
    source = args.repo or args.graph or args.input
    if not source:
        raise IngestError(
            "pass --input for a CSV, --repo for a repository, or --graph for an "
            "exported history"
        )
    kind = "git" if args.repo else ("graph" if args.graph else "csv")
    recorder = Trace(window=args.head or 16, label=str(source)) if args.trace else None
    stream = _read_source(args, {}, source, kind, recorder)
    if kind == "git":
        print(describe_repo(stream))
        print()
    elif kind == "graph":
        print(describe_graph(stream))
        print()
    print(stream.describe())
    if kind == "git":
        seed = auto_seed_repo(source)
    elif kind == "graph":
        seed = auto_seed_graph(source)
    else:
        seed = Chain(name="probe").resolve_seed(source)
    print(f"\nauto seed: {seed}")
    for strategy in ("columns", "variance", "sparse"):
        order = voice_order(stream, strategy)
        print(f"voice entry ({strategy}): {[stream.names[i] for i in order]}")

    if args.chain:
        chain = _load_chain(args.chain)
        after = chain.apply(stream, source=source)
        print(f"\nafter {chain.name}:")
        print(after.describe())

    if args.head:
        print(f"\nfirst {args.head} frames:")
        for index, name in enumerate(stream.names):
            row = " ".join(f"{v:>3}" for v in stream.data[index][: args.head])
            print(f"  {name[:18]:<18} {row}")

    if recorder is not None:
        print()
        print(recorder.describe())
        for stage in recorder.stages:
            for conversion in stage.detail.get("conversions", []):
                print(f"\n  {conversion['name']}  (column {conversion['column_index']})")
                lo, hi = conversion["range"]["low"], conversion["range"]["high"]
                print(f"    normalized against its own range {lo:g} .. {hi:g}")
                print(f"    {'cell':>12}  {'parsed':>12}  {'byte':>5}")
                for cell, parsed, value in list(
                    zip(conversion["cells"], conversion["parsed"], conversion["bytes"])
                )[:8]:
                    print(f"    {str(cell)[:12]:>12}  {parsed:>12g}  {value:>5}")
    return 0


# ---------------------------------------------------------------------------
# catalog / scales / curve
# ---------------------------------------------------------------------------
def cmd_catalog(args) -> int:
    print(f"serrin pedal catalog ({len(pedals.catalog())} pedals, max {MAX_VOICES} voices)\n")
    for pedal in pedals.catalog():
        summary = (pedal.doc or "").strip().splitlines()[0] if pedal.doc else ""
        print(f"{pedal.name}")
        print(f"  {summary}")
        for key, value in pedal.defaults.items():
            print(f"    {key:<16} = {value!r}")
        print()
    return 0


def cmd_scales(args) -> int:
    if args.name:
        offsets, span = resolve(args.name)
        print(f"{args.name}: offsets={offsets} span={span}")
        return 0
    width = max(len(name) for name in SCALE_BANK)
    for name, intervals in sorted(SCALE_BANK.items()):
        offsets, _ = resolve(name)
        print(f"{name:<{width}}  {intervals:<44}  {offsets}")
    return 0


def cmd_curve(args) -> int:
    if args.archetype:
        envelope = Envelope.from_archetype(args.archetype, args.curvature)
    elif args.spec:
        envelope = Envelope.from_spec(args.spec)
    else:
        print("equations: " + ", ".join(sorted(EQUATIONS)))
        print("archetypes: " + ", ".join(sorted(ARCHETYPES)))
        return 0
    print(json.dumps(envelope.origin))
    print(envelope.ascii(width=args.width, height=args.height))
    if args.out:
        write_json(args.out, envelope.to_json(), compact=False)
        print(f"-> {args.out}")
    return 0


def cmd_graph(args) -> int:
    """Export a repository's history to a portable JSON file, or check one.

    The format has to be producible or it is useless -- a source nobody can
    create is not a source. This is that half.
    """
    if args.check:
        document = load_graph(args.check)
        problems = validate_graph(document)
        facts = graph_facts(document)
        print(f"history {args.check}")
        print(f"  repo       {facts['repo'] or '(unnamed)'}")
        print(f"  commits    {facts['commits']}  ({facts['merges']} merges, "
              f"{facts['authors']} authors)")
        print(f"  traversal  {facts['traversal']}")
        print(f"  stats      {'yes' if facts['has_stats'] else 'no'}")
        if facts["span_seconds"]:
            print(f"  spans      {facts['span_seconds'] / 86400:.1f} days")
        for ref, count in facts["owned"].items():
            print(f"    {ref:<28} owns {count:>5} commits")
        if problems:
            print("\n  problems:")
            for problem in problems:
                print(f"    - {problem}")
            return 1
        print("\n  no problems")
        return 0

    if not args.repo:
        raise GraphError("pass --repo to export, or --check to inspect a file")

    document = export_graph(
        args.repo,
        branch_names=_split_columns(args.branches),
        traversal=args.traversal or "chrono",
        with_stats=not args.no_stats,
        limit=args.limit,
        stamp=args.stamp,
    )
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Indented: a history is meant to be committable and readable in a diff.
    target.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")

    facts = graph_facts(document)
    print(f"exported {facts['commits']} commits from {args.repo}")
    print(f"  branches   {', '.join(facts['branches'])}")
    print(f"  merges     {facts['merges']}  authors {facts['authors']}")
    print(f"  stats      {'yes' if facts['has_stats'] else 'no'}")
    print(f"  -> {target}  ({target.stat().st_size / 1024:.1f} KiB)")
    for problem in validate_graph(document):
        print(f"  note: {problem}")
    return 0


def cmd_new(args) -> int:
    """Create a piece: a folder with a manifest and the directories it owns."""
    folder = Path(args.folder)
    source: dict = {}
    if args.input:
        source = {"kind": "csv", "path": _relative_to(args.input, folder)}
    elif args.repo:
        source = {"kind": "git", "path": _relative_to(args.repo, folder), "metric": "hash"}
    elif args.graph:
        source = {"kind": "graph", "path": _relative_to(args.graph, folder), "metric": "hash"}
    if args.tempo:
        source["tempo"] = Tempo.parse(args.tempo).to_json()

    preset = _load_chain(args.chain).to_json() if args.chain else default_chain().to_json()
    piece = new_piece(folder, name=args.name, source=source, preset=preset, stamp=args.stamp)
    if args.title:
        piece.title = args.title
        piece.save()

    print(piece.describe())
    print(f"\n  -> {folder / MANIFEST}")
    if not source:
        print("  no source yet: edit source.path, or re-run with --input/--repo")
    return 0


def _relative_to(target: str, folder: Path) -> str:
    """Express a source path relative to the piece folder when it makes sense.

    A CSV sitting inside the piece is referenced relatively so the folder can be
    moved; one living elsewhere on the machine keeps its absolute path, because a
    relative path out of the folder would break the moment it moved anyway.
    """
    candidate = Path(target).resolve()
    try:
        return str(candidate.relative_to(folder.resolve())).replace("\\", "/")
    except ValueError:
        return str(candidate).replace("\\", "/")


def cmd_pieces(args) -> int:
    """List the pieces in a folder. The album view."""
    entries = list_pieces(args.folder)
    if not entries:
        print(f"no pieces in {args.folder}")
        print(f"  make one:  python -m serrin new {args.folder}/01-first -i data.csv")
        return 0

    print(f"{len(entries)} piece(s) in {args.folder}\n")
    for entry in entries:
        if not entry.get("ok"):
            print(f"  {entry['name']:<20} BROKEN: {entry['error']}")
            continue
        state = entry["fingerprint"][:12] if entry["rendered"] else "not rendered"
        title = f" — {entry['title']}" if entry["title"] else ""
        print(f"  {entry['name']:<20} {state:<14} {entry['kind']:<4} {entry['chain']}{title}")
        if entry["performance"] != "nothing mapped yet":
            print(f"  {'':<20} {entry['performance']}")
        if entry["missing_samples"]:
            print(f"  {'':<20} MISSING: {', '.join(entry['missing_samples'])}")
    return 0


def cmd_piece(args) -> int:
    """Inspect one piece, or import a session as one."""
    if args.from_session:
        session = Session.load(args.from_session)
        # A session is a piece that has already been rendered, so importing is
        # reshaping rather than converting -- one reader handles both formats.
        piece = Piece.from_json(session.to_json(), folder=Path(args.folder))
        # The folder name wins: the author chose it, and a session label is a
        # generated "source+chain+seed" string that makes a poor piece name.
        piece.name = slug(args.name or Path(args.folder).name or session.label)
        piece.save(Path(args.folder))
        (Path(args.folder) / "samples").mkdir(exist_ok=True)
        print(f"imported {args.from_session} as a piece\n")
        print(piece.describe())
        return 0

    piece = Piece.load(args.folder)
    print(piece.describe())

    if args.keymap and piece.performance.keymap:
        print("\n  keymap (by physical key position, so it survives layout changes)")
        for code, binding in sorted(piece.performance.keymap.items()):
            detail = " ".join(f"{k}={v}" for k, v in binding.items() if k != "kind")
            print(f"    {code:<10} {binding['kind']:<8} {detail}")

    for sample in piece.performance.samples:
        exists = "ok " if piece.resolve(sample.path).exists() else "MISSING"
        print(f"    sample {sample.id:<12} {exists} {sample.path}")

    for pattern in piece.performance.patterns:
        grid = ["."] * pattern.steps
        for hit in pattern.hits:
            grid[hit["step"]] = "x"
        flag = " " if pattern.enabled else "x"
        print(f"    [{flag}] {pattern.id:<12} {''.join(grid)}")
    return 0


def cmd_session(args) -> int:
    """Look at a session, or freeze its render layer into a preset."""
    session = Session.load(args.file)
    print(session.describe())

    if args.to_preset:
        preset = promote_to_preset(session, args.name)
        target = Path(args.to_preset)
        if target.exists() and not args.force:
            print(f"\n{target} exists; pass --force to overwrite", file=sys.stderr)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
        print(f"\npreset ->   {target}")
        # Said plainly, because it is the one thing about the format that can
        # surprise: a preset is the half of a session that has an offline
        # meaning, and the live settings are not it.
        if session.runtime:
            print(
                "note: the runtime block (levels, mutes, visual toggles, keyboard) "
                "is not part of a preset -- keep the session file for those."
            )
    return 0


def cmd_preset(args) -> int:
    """Write the built-in example chain out as an editable preset file."""
    chain = default_chain()
    target = Path(args.out)
    if target.exists() and not args.force:
        print(f"{target} exists; pass --force to overwrite", file=sys.stderr)
        return 1
    chain.save(target)
    print(f"wrote {target}")
    return 0


# ---------------------------------------------------------------------------
# argument wiring
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serrin",
        description="Opaque noise translator: data -> pedal chain -> audio/visual streams.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- render -------------------------------------------------------------
    render = sub.add_parser("render", help="run the pipeline and export both streams")
    render.add_argument("--input", "-i", help="source CSV (or take it from --session)")
    render.add_argument(
        "--piece",
        "-p",
        help="render a piece: its source, its chain, into its own out/",
    )
    render.add_argument(
        "--no-save",
        action="store_true",
        help="do not write the render back into the piece manifest",
    )
    render.add_argument(
        "--stamp",
        help="timestamp recorded on the piece (default: none, for reproducible files)",
    )
    render.add_argument(
        "--repo",
        "-r",
        help="a git repository as the source instead of a CSV (section 6.3)",
    )
    render.add_argument(
        "--graph",
        "-g",
        help="an exported history JSON as the source (see `serrin graph`)",
    )
    render.add_argument(
        "--metric",
        choices=sorted(METRICS),
        help="what a commit contributes: hash (default), interval, churn, parents, hour...",
    )
    render.add_argument(
        "--traversal", choices=list(TRAVERSALS), help="commit order (default chrono)"
    )
    render.add_argument("--branches", help="comma-separated branches to use as voices")
    render.add_argument(
        "--session", help="re-render a saved session: its source and its chain"
    )
    render.add_argument(
        "--out-session", help="also write a session file describing this render"
    )
    render.add_argument("--chain", "-c", help="preset JSON (default: built-in gritty_01)")
    render.add_argument("--out-audio", default="out/stream_audio.json")
    render.add_argument("--out-visual", default="out/stream_visual.json")
    render.add_argument("--columns", help="comma-separated names or indices (max 8)")
    render.add_argument("--bit-depth", type=int, help="bits per value (default 8)")
    render.add_argument("--granularity", type=int, help="rows per frame (default 1)")
    render.add_argument("--aggregation", choices=["mean", "max", "min", "sum", "first", "last", "range"])
    render.add_argument("--rate", type=float, help="frames per second; --tempo is usually clearer")
    render.add_argument(
        "--tempo",
        help="musical grid: '120', '96/8' (BPM/subdivision), '128/16+0.3' (with swing)",
    )
    render.add_argument(
        "--subdivision",
        type=int,
        choices=list(SUBDIVISIONS),
        help="note value of one frame: 4=quarter, 8=eighth, 16=sixteenth (default 16)",
    )
    render.add_argument("--swing", type=float, help="0 = straight, 1 = triplet feel")
    render.add_argument("--beats-per-bar", type=int, help="metre numerator (default 4)")
    render.add_argument(
        "--delay-note",
        choices=sorted(NOTE_FRACTIONS),
        help="tempo-synced delay time the runtime should start on (default 1/8.)",
    )
    render.add_argument("--log-scale", action="store_true", help="log-normalize on ingest")
    render.add_argument("--limit", type=int, help="read at most N rows")
    render.add_argument("--seed", type=int, help="override the seed")
    render.add_argument("--mode", choices=["closed", "endless"])
    render.add_argument(
        "--loop",
        choices=["vary", "loop", "pingpong", "once"],
        help="what happens when the stream runs out (section 5.1.2)",
    )
    render.add_argument("--voice-entry", choices=["columns", "variance", "sparse"])
    render.add_argument("--envelope", help="'arc', 'sigmoid:centre=0.4', 'archetype:climax'")
    render.add_argument("--archetype", choices=sorted(ARCHETYPES))
    render.add_argument("--curvature", type=float, default=1.0)
    render.add_argument("--envelope-points", help="JSON file of drawn (t, intensity) points")
    render.add_argument("--scale", help="quantize output pitches to this scale")
    render.add_argument("--note-low", type=int)
    render.add_argument("--note-high", type=int)
    render.add_argument("--freq-curve", choices=["direct", "log"])
    render.add_argument(
        "--trace",
        help="write a stage-by-stage trace of the render (what each pedal did)",
    )
    render.add_argument(
        "--trace-window",
        type=int,
        help=f"frames kept per channel per stage (default {DEFAULT_WINDOW})",
    )
    render.add_argument("--pretty", action="store_true", help="indented JSON (much bigger)")
    render.add_argument("--verbose", "-v", action="store_true")
    render.set_defaults(func=cmd_render)

    # -- inspect ------------------------------------------------------------
    inspect = sub.add_parser("inspect", help="look at a CSV without rendering")
    inspect.add_argument("--input", "-i", help="source CSV")
    inspect.add_argument("--repo", "-r", help="a git repository instead")
    inspect.add_argument("--graph", "-g", help="an exported history JSON instead")
    inspect.add_argument("--metric", choices=sorted(METRICS))
    inspect.add_argument("--traversal", choices=list(TRAVERSALS))
    inspect.add_argument("--branches")
    inspect.add_argument("--chain", "-c", help="also show the stream after this chain")
    inspect.add_argument("--columns")
    inspect.add_argument("--bit-depth", type=int)
    inspect.add_argument("--granularity", type=int)
    inspect.add_argument("--rate", type=float)
    inspect.add_argument("--tempo")
    inspect.add_argument("--subdivision", type=int, choices=list(SUBDIVISIONS))
    inspect.add_argument("--swing", type=float)
    inspect.add_argument("--beats-per-bar", type=int)
    inspect.add_argument("--limit", type=int)
    inspect.add_argument("--head", type=int, default=16, help="print N frames per voice")
    inspect.add_argument(
        "--trace",
        action="store_true",
        help="show the cell-to-byte conversion and per-channel entropy",
    )
    inspect.set_defaults(func=cmd_inspect)

    # -- catalog / scales / curve / preset ----------------------------------
    catalog = sub.add_parser("catalog", help="list pedals and their parameters")
    catalog.set_defaults(func=cmd_catalog)

    scales = sub.add_parser("scales", help="list the scale bank, or resolve one")
    scales.add_argument("name", nargs="?", help="scale name or interval spec")
    scales.set_defaults(func=cmd_scales)

    curve = sub.add_parser("curve", help="preview an intensity envelope as ASCII")
    curve.add_argument("spec", nargs="?", help="equation spec, e.g. 'arc' or 'sigmoid:steepness=14'")
    curve.add_argument("--archetype", choices=sorted(ARCHETYPES))
    curve.add_argument("--curvature", type=float, default=1.0)
    curve.add_argument("--width", type=int, default=72)
    curve.add_argument("--height", type=int, default=12)
    curve.add_argument("--out", help="write the baked curve to JSON")
    curve.set_defaults(func=cmd_curve)

    graph = sub.add_parser(
        "graph", help="export a repository's history to JSON, or check a file"
    )
    graph.add_argument("--repo", "-r", help="repository to export")
    graph.add_argument("--out", "-o", default="history.json")
    graph.add_argument("--branches")
    graph.add_argument("--traversal", choices=list(TRAVERSALS))
    graph.add_argument("--limit", type=int)
    graph.add_argument(
        "--no-stats",
        action="store_true",
        help="skip the --numstat pass; churn and friends will read flat",
    )
    graph.add_argument("--stamp")
    graph.add_argument("--check", help="inspect and validate an existing history file")
    graph.set_defaults(func=cmd_graph)

    fresh = sub.add_parser("new", help="create a piece folder")
    fresh.add_argument("folder", help="where to create it")
    fresh.add_argument("--name")
    fresh.add_argument("--title")
    fresh.add_argument("--input", "-i", help="source CSV")
    fresh.add_argument("--repo", "-r", help="source git repository")
    fresh.add_argument("--graph", "-g", help="source history JSON")
    fresh.add_argument("--chain", "-c", help="preset to start from")
    fresh.add_argument("--tempo")
    fresh.add_argument("--stamp")
    fresh.set_defaults(func=cmd_new)

    pieces = sub.add_parser("pieces", help="list the pieces in a folder (an album)")
    pieces.add_argument("folder", nargs="?", default="pieces")
    pieces.set_defaults(func=cmd_pieces)

    one = sub.add_parser("piece", help="inspect a piece, or import a session as one")
    one.add_argument("folder")
    one.add_argument("--keymap", action="store_true", help="print the key bindings")
    one.add_argument("--from-session", help="import this session file as a piece")
    one.add_argument("--name")
    one.set_defaults(func=cmd_piece)

    session = sub.add_parser("session", help="inspect a session, or freeze it as a preset")
    session.add_argument("file", help="session JSON, as saved from the panel")
    session.add_argument("--to-preset", help="write the render layer out as a preset")
    session.add_argument("--name", help="name for the frozen preset")
    session.add_argument("--force", action="store_true")
    session.set_defaults(func=cmd_session)

    preset = sub.add_parser("preset", help="write the built-in chain out as a preset file")
    preset.add_argument("--out", default="presets/gritty_01.json")
    preset.add_argument("--force", action="store_true")
    preset.set_defaults(func=cmd_preset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        GitError,
        GraphError,
        IngestError,
        PieceError,
        SessionError,
        TempoError,
        ValueError,
        FileNotFoundError,
    ) as exc:
        print(f"serrin: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
