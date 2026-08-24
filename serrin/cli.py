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
from .export import MappingConfig, build_piece, write_json
from .ingest import IngestError, ingest_csv
from .scales import SCALE_BANK, resolve
from .stream import MAX_VOICES


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
    chain = _load_chain(args.chain)
    ingest_opts = dict(chain.ingest)

    columns = _split_columns(args.columns) or ingest_opts.get("columns")
    stream = ingest_csv(
        args.input,
        columns=columns,
        bit_depth=args.bit_depth or ingest_opts.get("bit_depth", 8),
        granularity=args.granularity or ingest_opts.get("granularity", 1),
        aggregation=args.aggregation or ingest_opts.get("aggregation", "mean"),
        rate=args.rate or ingest_opts.get("rate", 8.0),
        log_scale=args.log_scale or bool(ingest_opts.get("log_scale", False)),
        limit=args.limit,
    )

    seed = args.seed if args.seed is not None else chain.resolve_seed(args.input)
    if args.verbose:
        print(f"ingested {stream.describe()}")
        print(chain.describe())
        print(f"seed {seed}")

    transformed = chain.apply(stream, seed=seed, source=args.input, trace=args.verbose)
    envelope = _envelope_from_args(args, chain)
    mapping = _mapping_from_args(args, chain)

    # Precedence: explicit CLI flag > preset "piece" block > built-in default.
    mode = args.mode or chain.piece.get("mode", "closed")
    loop_policy = args.loop or chain.piece.get("loop", "vary")
    voice_entry = args.voice_entry or chain.piece.get("voice_entry", "variance")

    piece = build_piece(
        transformed,
        chain=chain,
        envelope=envelope,
        config=mapping,
        mode=mode,
        loop_policy=loop_policy,
        voice_entry=voice_entry,
    )

    out_audio = Path(args.out_audio)
    out_visual = Path(args.out_visual)
    audio_bytes = write_json(out_audio, piece.audio_document(), compact=not args.pretty)
    visual_bytes = write_json(out_visual, piece.visual_document(), compact=not args.pretty)

    print(f"label       {piece.meta['label']}")
    print(f"seed        {seed}")
    print(f"fingerprint {piece.meta['fingerprint']}")
    print(
        f"stream      {transformed.n_voices} voices x {transformed.length} frames "
        f"@ {transformed.rate}Hz = {transformed.duration:.1f}s"
    )
    print(f"mode        {mode} / loop={loop_policy} / entry={voice_entry}")
    print(f"audio  ->   {out_audio}  ({audio_bytes / 1024:.1f} KiB)")
    print(f"visual ->   {out_visual}  ({visual_bytes / 1024:.1f} KiB)")
    if args.verbose:
        print("\nintensity envelope:")
        print(envelope.ascii())
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
def cmd_inspect(args) -> int:
    stream = ingest_csv(
        args.input,
        columns=_split_columns(args.columns),
        bit_depth=args.bit_depth or 8,
        granularity=args.granularity or 1,
        rate=args.rate or 8.0,
        limit=args.limit,
    )
    print(stream.describe())
    print(f"\nauto seed: {Chain(name='probe').resolve_seed(args.input)}")
    for strategy in ("columns", "variance", "sparse"):
        order = voice_order(stream, strategy)
        print(f"voice entry ({strategy}): {[stream.names[i] for i in order]}")

    if args.chain:
        chain = _load_chain(args.chain)
        after = chain.apply(stream, source=args.input)
        print(f"\nafter {chain.name}:")
        print(after.describe())

    if args.head:
        print(f"\nfirst {args.head} frames:")
        for index, name in enumerate(stream.names):
            row = " ".join(f"{v:>3}" for v in stream.data[index][: args.head])
            print(f"  {name[:18]:<18} {row}")
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
    render.add_argument("--input", "-i", required=True, help="source CSV")
    render.add_argument("--chain", "-c", help="preset JSON (default: built-in gritty_01)")
    render.add_argument("--out-audio", default="out/stream_audio.json")
    render.add_argument("--out-visual", default="out/stream_visual.json")
    render.add_argument("--columns", help="comma-separated names or indices (max 8)")
    render.add_argument("--bit-depth", type=int, help="bits per value (default 8)")
    render.add_argument("--granularity", type=int, help="rows per frame (default 1)")
    render.add_argument("--aggregation", choices=["mean", "max", "min", "sum", "first", "last", "range"])
    render.add_argument("--rate", type=float, help="frames per second (default 8)")
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
    render.add_argument("--pretty", action="store_true", help="indented JSON (much bigger)")
    render.add_argument("--verbose", "-v", action="store_true")
    render.set_defaults(func=cmd_render)

    # -- inspect ------------------------------------------------------------
    inspect = sub.add_parser("inspect", help="look at a CSV without rendering")
    inspect.add_argument("--input", "-i", required=True)
    inspect.add_argument("--chain", "-c", help="also show the stream after this chain")
    inspect.add_argument("--columns")
    inspect.add_argument("--bit-depth", type=int)
    inspect.add_argument("--granularity", type=int)
    inspect.add_argument("--rate", type=float)
    inspect.add_argument("--limit", type=int)
    inspect.add_argument("--head", type=int, default=16, help="print N frames per voice")
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
    except (IngestError, ValueError, FileNotFoundError) as exc:
        print(f"serrin: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
