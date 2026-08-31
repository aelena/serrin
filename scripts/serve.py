"""Serve the repo over HTTP, and render uploads on request.

ES modules and ``fetch`` both refuse ``file://`` URLs, so opening
``web/index.html`` directly gets you a black page and a console full of CORS
errors. This is the smallest thing that fixes that -- plus one endpoint, because
the pipeline is Python and the browser cannot run it.

The upload path is the honest middle option. The alternative was porting the
pedal chain to JavaScript (roadmap step 5), which would duplicate nine pedals,
ingestion and the export mapping, and create a *second source of truth for the
aesthetic* -- two implementations that would drift, with the sound as the thing
that drifts. Sending the file to the one real pipeline keeps a single
implementation and costs an endpoint.

**This binds to localhost.** It writes files it is given and runs renders on
request, which is fine for a tool on your own machine and not fine on a shared
network. ``--host 0.0.0.0`` is available and says what it is doing.

    python scripts/serve.py                       # http://127.0.0.1:8000/web/
    python scripts/serve.py --port 9000 --no-open
    python scripts/serve.py --host 0.0.0.0        # deliberately reachable
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import sys
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Uploads land here, under out/, which is gitignored.
UPLOAD_DIR = ROOT / "out" / "uploads"

#: Where pieces live. Point this at your album folder with --pieces.
#:
#: Every write goes through `_piece_folder`, which refuses anything outside this
#: root. Reads are confined the same way. The server binds to localhost and this
#: is an author's tool, but an endpoint that writes JSON to a path supplied over
#: HTTP wants a boundary regardless -- and "the folder you pointed me at" is a
#: boundary the author already understands.
PIECES_DIR = ROOT / "pieces"

#: A CSV bigger than this is refused rather than buffered. Generous for
#: telemetry, small enough that a stray POST cannot exhaust memory.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str, fallback: str = "upload") -> str:
    cleaned = _SAFE_NAME.sub("_", str(text or "")).strip("._")
    return (cleaned or fallback)[:60]


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files with no caching, plus POST /api/render."""

    # HTTP/1.1 so browsers can keep connections alive. Safe only because the
    # server below is threaded -- on a serial server one held connection stalls
    # every other request, which is exactly the bug this started with.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        """Serve /pieces/... from the pieces root, wherever that is.

        Needed because an album can live anywhere on disk -- that is the point of
        pieces being portable folders -- but the browser can only fetch what the
        server exposes. Without this, a rendered piece kept outside the repo had
        no URL and could not be played.

        Sanitized by hand rather than by the base class, which resolves against
        its own single directory: components are filtered, so `..` and absolute
        segments cannot climb out of the pieces root.
        """
        route = path.partition("?")[0].partition("#")[0]
        if not route.startswith("/pieces/"):
            return super().translate_path(path)
        target = PIECES_DIR.resolve()
        for part in route[len("/pieces/") :].split("/"):
            part = unquote(part)
            if not part or part in (".", ".."):
                continue
            target = target / part
        return str(target)

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")

    # -- helpers ------------------------------------------------------------
    def _send_json(
        self,
        status: int,
        payload: dict,
        headers: dict[str, str] | None = None,
        body: bool = True,
    ) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded) if body else 0))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(encoded)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty request body")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"{length / 1048576:.1f} MB is over the "
                f"{MAX_UPLOAD_BYTES / 1048576:.0f} MB upload limit"
            )
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc

    # -- routing ------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        route, _, query = self.path.partition("?")
        route = route.rstrip("/")
        if not route.startswith("/api/"):
            # Static, including /pieces/... which translate_path redirects.
            super().do_GET()
            return
        self._dispatch(get_handlers(parse_qs(query)), route, "GET")

    def do_POST(self):  # noqa: N802 -- the base class spells it this way
        route = self.path.partition("?")[0].rstrip("/")
        self._dispatch(post_handlers(self._read_json), route, "POST")

    def do_HEAD(self):  # noqa: N802
        route = self.path.partition("?")[0].rstrip("/")
        if not route.startswith("/api/"):
            super().do_HEAD()
            return
        # Without this, HEAD on an API route fell through to the static handler
        # and answered about a file called "api/source" -- text/html, and nothing
        # to do with the endpoint. A HEAD has no body, so the honest answer is the
        # status and the headers a GET would send.
        status = self._method_error(route, "GET")
        self._send_json(status or 200, {}, body=False)

    def _method_error(self, route: str, method: str) -> int | None:
        """404 if the route does not exist, 405 if it does but not this way."""
        allowed = API_ROUTES.get(route)
        if allowed is None:
            self._send_json(404, {"error": f"no such endpoint: {self.path}"})
            return 404
        if method not in allowed:
            # The old code answered 404 here, with "no such endpoint" -- which is
            # a lie about a route that exists, and cost an afternoon of looking
            # for a missing endpoint that was never missing. 405 says "it is
            # there, not like that", and Allow says how.
            self._send_json(
                405,
                {
                    "error": (
                        f"{route} does not answer {method}; "
                        f"it answers {', '.join(allowed)}"
                    ),
                    "allow": list(allowed),
                },
                headers={"Allow": ", ".join(allowed)},
            )
            return 405
        return None

    def _dispatch(self, handlers: dict, route: str, method: str) -> None:
        if self._method_error(route, method) is not None:
            return
        handler = handlers.get(route)
        if handler is None:
            # Declared in API_ROUTES with no handler behind it. That is a wiring
            # bug in serrin, not a caller mistake, so it is a 500 and it says so
            # rather than pretending the route is absent.
            self._send_json(
                500,
                {"error": f"{route} is declared for {method} but has no handler"},
            )
            return
        try:
            self._send_json(200, handler())
        except ValueError as exc:
            # PieceError and TempoError are ValueErrors, so a bad manifest or a
            # bad tempo reads as a client mistake rather than a server fault.
            self._send_json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            # The browser gets one line; the terminal gets the traceback, because
            # this is the author's own machine and hiding it helps nobody.
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# the routes, declared once
# ---------------------------------------------------------------------------
#: Which routes answer GET, and which answer POST.
#:
#: Declared here rather than implied by the handler dictionaries because three
#: things need to agree: the dispatcher, the 405 that tells a caller it used the
#: wrong method, and the list the catalog advertises so the page can detect a
#: server older than itself. Deriving all three from one table is the only way
#: they cannot drift; a hand-maintained copy of a route list is a lie waiting
#: for a release.
GET_ROUTES = frozenset(
    {"/api/catalog", "/api/source", "/api/pieces", "/api/piece"}
)
POST_ROUTES = frozenset(
    {"/api/render", "/api/piece", "/api/piece/new", "/api/piece/data", "/api/piece/graph"}
)

#: route -> the methods it allows, in the order an ``Allow`` header wants them.
API_ROUTES: dict[str, tuple[str, ...]] = {
    route: tuple(
        method
        for method, routes in (("GET", GET_ROUTES), ("POST", POST_ROUTES))
        if route in routes
    )
    for route in sorted(GET_ROUTES | POST_ROUTES)
}


def get_handlers(params: dict, read_json=None) -> dict:
    """The GET handlers. A function so tests can check it against GET_ROUTES."""
    return {
        "/api/pieces": lambda: {"pieces": list_pieces_api(params.get("folder", [None])[0])},
        "/api/piece": lambda: open_piece_api(params.get("folder", [None])[0]),
        "/api/catalog": catalog_api,
        "/api/source": lambda: source_api(
            piece_folder=params.get("piece", [None])[0],
            path=params.get("path", [None])[0],
            kind=params.get("kind", [None])[0],
        ),
    }


def post_handlers(read_json) -> dict:
    """The POST handlers. ``read_json`` is called lazily, inside the handler, so
    a bad body reports as 400 through the dispatcher rather than at wiring time.
    """
    return {
        "/api/render": lambda: render_upload(read_json()),
        "/api/piece": lambda: save_piece_api(read_json()),
        "/api/piece/new": lambda: new_piece_api(read_json()),
        "/api/piece/data": lambda: put_data_api(read_json()),
        "/api/piece/graph": lambda: export_graph_api(read_json()),
    }


# ---------------------------------------------------------------------------
# the catalog: what the studio builds its forms from
# ---------------------------------------------------------------------------
def source_api(
    piece_folder: str | None = None,
    path: str | None = None,
    kind: str | None = None,
) -> dict:
    """What a source offers, and everything wrong with it, without rendering.

    Two things this fixes about the endpoint it replaces.

    **It reads the path it is given.** The old one loaded the piece from disk and
    used its *saved* source path, so typing a new path in the studio re-read the
    old file and the column list never changed. A working copy is unsaved by
    definition; asking about it has to mean asking about what is on screen.

    **It reports problems, not just columns.** Serrin does not clean data, so the
    only useful thing it can do about a bad source is name the problem precisely
    enough to be fixed upstream -- per column for a CSV, per document for a
    history file, per repository for a clone.
    """
    from serrin.piece import Piece  # noqa: PLC0415

    piece = Piece.load(_piece_folder(piece_folder)) if piece_folder else None

    # The given path wins over the saved one: that is the whole point.
    if path:
        source = piece.resolve(path) if piece else Path(path).expanduser()
    elif piece:
        source = piece.source_path
    else:
        raise ValueError("pass a path, a piece folder, or both")

    if not kind:
        kind = piece.kind if piece else _guess_kind(source)

    if not source.exists():
        return {
            "kind": kind,
            "path": str(source),
            "exists": False,
            "problems": [f"nothing at {source}"],
        }

    if kind == "graph":
        return _graph_source(source)
    if kind == "git":
        return _git_source(source)
    return _csv_source(source)


def _guess_kind(source: Path) -> str:
    """Infer the kind from what is actually there, for a bare `?path=`."""
    if source.is_dir():
        return "git" if (source / ".git").exists() else "csv"
    return "graph" if source.suffix.lower() == ".json" else "csv"


def _csv_source(source: Path) -> dict:
    from serrin.ingest import (  # noqa: PLC0415
        _parse_number,
        monotonicity,
        numeric_columns,
        read_rows,
        select_columns,
    )

    problems: list[str] = []
    table: dict = {}
    try:
        header, rows = read_rows(source, report=table)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "csv", "path": str(source), "exists": True, "problems": [str(exc)]}

    # Skipping a preamble is a judgement, and a judgement made silently is
    # indistinguishable from a bug -- so it is reported as something the author
    # can check rather than trusted quietly.
    if table.get("preamble_lines"):
        problems.append(
            f"skipped {table['preamble_lines']} line(s) of preamble; the header "
            f"looks like line {table['header_line']}: {', '.join(header[:6])}"
            + (" …" if len(header) > 6 else "")
        )
    if table.get("dropped_rows"):
        problems.append(
            f"dropped {table['dropped_rows']} line(s) that did not have "
            f"{table['columns']} fields — usually a footer"
        )
    if not table.get("named_header", True):
        problems.append(
            "the table starts straight into numbers, so the columns have no "
            "names and are called col0, col1 …"
        )

    numeric = set(numeric_columns(header, rows))
    try:
        chosen = set(select_columns(header, rows, None))
    except Exception as exc:  # noqa: BLE001
        chosen = set()
        problems.append(str(exc))

    columns = []
    for index, name in enumerate(header):
        # The pipeline's own parser, not a stricter one. It pulls a number out of
        # "45 ms" and out of "row12", so a column this endpoint called
        # unparseable while ingestion happily read it would send the author
        # hunting for a problem that is not there.
        values = []
        for row in rows:
            if index < len(row):
                parsed = _parse_number(row[index])
                if parsed is not None:
                    values.append(parsed)
        reason = ""
        if index not in numeric:
            reason = "not numeric"
        elif len(values) < 2:
            reason = "too few values"
        elif max(values) == min(values):
            reason = "constant - a flat line is not a voice"
        elif monotonicity(values) > 0.98:
            reason = "monotonic - variance but no shape, like a timestamp"
        columns.append(
            {
                "index": index,
                "name": name,
                "numeric": index in numeric,
                "chosen": index in chosen,
                "reason": reason,
                "low": min(values) if values else None,
                "high": max(values) if values else None,
            }
        )

    if len(rows) < 2:
        problems.append(f"only {len(rows)} data row(s): there is no shape to read")
    if not chosen:
        problems.append(
            "no column survives automatic selection - every one is constant, "
            "monotonic or unparseable. Serrin does not clean data; fix it upstream "
            "or name columns explicitly."
        )

    return {
        "kind": "csv",
        "path": str(source),
        "exists": True,
        "rows": len(rows),
        "columns": columns,
        "auto_chosen": [header[i] for i in sorted(chosen)],
        "table": table,
        "problems": problems,
    }


def _git_source(source: Path) -> dict:
    from serrin.ingest_git import GitError, branches  # noqa: PLC0415

    if not (source / ".git").exists() and not (source / "HEAD").exists():
        return {
            "kind": "git",
            "path": str(source),
            "exists": True,
            "problems": [f"{source} is not a git repository (no .git)"],
        }
    try:
        found = branches(source)
    except GitError as exc:
        return {"kind": "git", "path": str(source), "exists": True, "problems": [str(exc)]}

    problems = []
    if len(found) > 8:
        problems.append(
            f"{len(found)} branches but the voice ceiling is 8 - the most recently "
            "touched will be kept"
        )
    return {
        "kind": "git",
        "path": str(source),
        "exists": True,
        "branches": [{"name": name, "tip": tip} for name, tip in found],
        "problems": problems,
    }


def _graph_source(source: Path) -> dict:
    from serrin.graph import (  # noqa: PLC0415
        GraphError,
        graph_facts,
        load_graph,
        validate_graph,
    )

    try:
        document = load_graph(source)
    except GraphError as exc:
        return {"kind": "graph", "path": str(source), "exists": True, "problems": [str(exc)]}

    facts = graph_facts(document)
    return {
        "kind": "graph",
        "path": str(source),
        "exists": True,
        "graph": facts,
        "branches": [{"name": name, "tip": 0} for name in facts["branches"]],
        "problems": validate_graph(document),
    }


def export_graph_api(payload: dict) -> dict:
    """Read a repository and leave its history in a piece folder.

    The bridge between the two graph sources: point at a clone once, and the
    piece keeps a portable copy that travels with the folder. Without this the
    graph format would only be reachable from the CLI, which makes it a format
    the studio can read and not write.
    """
    from serrin.graph import export_graph, graph_facts, validate_graph  # noqa: PLC0415

    folder = _piece_folder(payload.get("piece"))
    repo = Path(str(payload.get("repo") or "")).expanduser()
    if not repo.exists():
        raise ValueError(f"no such repository: {repo}")

    document = export_graph(
        repo,
        traversal=payload.get("traversal") or "chrono",
        with_stats=payload.get("with_stats", True),
        limit=payload.get("limit"),
        stamp=payload.get("stamp"),
    )
    name = f"{_slug(repo.resolve().name, 'history')}.history.json"
    target = folder / name
    target.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")

    facts = graph_facts(document)
    print(f"  exported {facts['commits']} commits from {repo.name} into {folder.name}")
    return {
        "ok": True,
        "path": name,
        "kind": "graph",
        "graph": facts,
        "problems": validate_graph(document),
    }


# ---------------------------------------------------------------------------
# uploads into a piece
# ---------------------------------------------------------------------------
#: What a piece will accept as a data file, and where it lands.
DATA_SUFFIXES = {".csv": "csv", ".tsv": "csv", ".json": "graph"}


def put_data_api(payload: dict) -> dict:
    """Write a CSV or a history file into a piece folder.

    A browser cannot hand over a filesystem path -- a file input gives contents,
    not a location -- so "choose a file" necessarily means "copy it in". That is
    also the better default for a piece: the data travels with the folder.

    Pointing at a path elsewhere on the machine stays available as a text field,
    for a file too large to want two copies of.
    """
    folder = _piece_folder(payload.get("piece"))
    raw_name = str(payload.get("filename") or "data.csv")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no file contents in the request")

    suffix = Path(raw_name).suffix.lower()
    if suffix not in DATA_SUFFIXES:
        raise ValueError(
            f"{suffix or 'that'} is not a data file Serrin reads; "
            f"expected one of {', '.join(sorted(DATA_SUFFIXES))}"
        )
    kind = DATA_SUFFIXES[suffix]

    # Validated before it is written, so a piece never ends up pointing at a file
    # that cannot be read. A rejected upload is better than a broken source.
    if kind == "csv":
        # This used to be true only of histories: a CSV was written first and
        # complained about afterwards, so an unparseable one still became the
        # piece's source. Note what is *not* checked here -- a metadata preamble,
        # a semicolon, a footer, a decimal comma are all read fine and are none of
        # the uploader's business. The bar is "can the table be found at all".
        from serrin.ingest import IngestError, rows_from_text  # noqa: PLC0415

        try:
            rows_from_text(text, label=raw_name)
        except IngestError as exc:
            raise ValueError(str(exc)) from exc

    if kind == "graph":
        from serrin.graph import validate_graph  # noqa: PLC0415

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}") from exc
        fatal = [
            problem
            for problem in validate_graph(document)
            if "no commits" in problem or "timestamp" in problem or "not a JSON" in problem
        ]
        if fatal:
            raise ValueError("; ".join(fatal))

    name = _slug(Path(raw_name).stem) + suffix
    target = folder / name
    target.write_text(text, encoding="utf-8")
    print(f"  wrote {kind} into {folder.name}: {name} ({len(text) / 1024:.1f} KiB)")

    return {
        "ok": True,
        # Relative, so the piece folder stays portable.
        "path": name,
        "kind": kind,
        "bytes": len(text.encode("utf-8")),
        "source": source_api(piece_folder=payload.get("piece"), path=name, kind=kind),
    }


def catalog_api() -> dict:
    """Every list the studio needs, from the modules that own them.

    Served rather than hardcoded in JavaScript. Duplicating these would mean a
    pedal added in Python is silently unreachable from the studio -- the same
    drift the tempo and voice-activation cross-checks exist to prevent, except
    here it would be invisible rather than merely untested.
    """
    from serrin import pedals  # noqa: PLC0415
    from serrin.pedals import base as pedal_base  # noqa: PLC0415
    from serrin.envelope import ARCHETYPES, EQUATIONS  # noqa: PLC0415
    from serrin.export import GLYPHS, MappingConfig  # noqa: PLC0415
    from serrin.ingest_git import METRICS, TRAVERSALS  # noqa: PLC0415
    from serrin.piece import BINDING_KINDS, DEFAULT_KEYMAP_ROWS  # noqa: PLC0415
    from serrin.scales import DEFAULT_SCALE, SCALE_BANK, resolve  # noqa: PLC0415
    from serrin.stream import MAX_VOICES  # noqa: PLC0415
    from serrin.tempo import NOTE_FRACTIONS, SUBDIVISIONS  # noqa: PLC0415

    return {
        "pedals": [
            {
                "name": pedal.name,
                # First line only: the studio shows it as help text, and the full
                # docstring is a paragraph.
                "summary": (pedal.doc or "").strip().splitlines()[0] if pedal.doc else "",
                # Doubles as the parameter schema -- the defaults dict is the
                # honest description of what a pedal accepts.
                "params": pedal.defaults,
            }
            for pedal in pedals.catalog()
        ],
        "lfo_shapes": list(pedal_base.SHAPES),
        "scales": {
            name: {"intervals": intervals, "offsets": resolve(name)[0]}
            for name, intervals in sorted(SCALE_BANK.items())
        },
        "default_scale": DEFAULT_SCALE,
        "archetypes": sorted(ARCHETYPES),
        "equations": sorted(EQUATIONS),
        "git": {"metrics": METRICS, "traversals": list(TRAVERSALS)},
        "tempo": {
            "subdivisions": list(SUBDIVISIONS),
            "note_fractions": NOTE_FRACTIONS,
        },
        "mapping_defaults": MappingConfig().to_json(),
        "glyphs": GLYPHS,
        "max_voices": MAX_VOICES,
        "binding_kinds": list(BINDING_KINDS),
        "keymap_rows": [list(row) for row in DEFAULT_KEYMAP_ROWS],
        # What this build actually serves, read off the route table rather than
        # written out again. The page compares it against what it needs and says
        # so if the server is older -- otherwise a renamed or added endpoint shows
        # up as a bare 404 in the console and nothing in the UI, which is exactly
        # how an afternoon gets lost.
        "endpoints": sorted(API_ROUTES),
        "routes": {route: list(methods) for route, methods in API_ROUTES.items()},
        "aggregations": ["mean", "max", "min", "sum", "first", "last", "range"],
        "loop_policies": ["vary", "loop", "pingpong", "once"],
        "voice_entry": ["variance", "columns", "sparse"],
        "modes": ["closed", "endless"],
        "waveforms": ["sawtooth", "square", "triangle", "sine"],
    }


# ---------------------------------------------------------------------------
# pieces
# ---------------------------------------------------------------------------
def _piece_folder(raw: str | None, must_exist: bool = True) -> Path:
    """Resolve a piece folder, refusing anything outside the pieces root.

    The check is on the *resolved* path, so `../` and symlinks cannot walk out.
    Without that, an endpoint that writes JSON would happily write it anywhere
    the process can reach.
    """
    if not raw:
        raise ValueError("no folder given")
    root = PIECES_DIR.resolve()
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(
            f"{candidate} is outside the pieces root ({root}). "
            "Start the server with --pieces pointing at your album folder."
        )
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"no such piece folder: {candidate}")
    return candidate


def _piece_payload(piece) -> dict:
    """A piece as the browser wants it: the manifest plus what it can infer."""
    root = PIECES_DIR.resolve()
    try:
        relative = str(piece.folder.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        relative = str(piece.folder)
    render = dict(piece.render)
    # Rewritten as URLs the page can fetch, so loading a rendered piece uses the
    # same path as loading a preset. Served through /pieces/, not /out/, because
    # an album can live anywhere -- see translate_path.
    for key in ("audio", "visual"):
        if render.get(key):
            try:
                on_disk = piece.resolve(render[key]).resolve()
                inside = str(on_disk.relative_to(root)).replace("\\", "/")
                render[f"{key}_url"] = f"/pieces/{inside}"
            except ValueError:
                render[f"{key}_url"] = None
    return {
        "folder": relative,
        "absolute": str(piece.folder),
        "manifest": piece.to_json(),
        "render": render,
        "missing_samples": [s.path for s in piece.missing_samples()],
        "source_exists": bool(piece.source.get("path")) and piece.source_path.exists(),
    }


def list_pieces_api(folder: str | None) -> list[dict]:
    from serrin.piece import list_pieces  # noqa: PLC0415

    base = _piece_folder(folder) if folder else PIECES_DIR
    if not base.exists():
        return []
    return list_pieces(base)


def open_piece_api(folder: str | None) -> dict:
    from serrin.piece import Piece  # noqa: PLC0415

    return _piece_payload(Piece.load(_piece_folder(folder)))


def new_piece_api(payload: dict) -> dict:
    """Create a piece. The name is required, and has to survive being a folder.

    It used to default to "untitled", so a POST with an empty body created a
    piece. An endpoint that makes something on the disk without being told what
    to call it is a sharp edge: the accident is silent, it looks like a real
    piece in the album, and the author did not ask for it.

    A name that slugs away to nothing is refused for the same reason. Quietly
    renaming "???" to "piece" is the same silent judgement in a smaller coat.
    """
    from serrin.chain import Chain, default_chain  # noqa: PLC0415
    from serrin.piece import new_piece, slug  # noqa: PLC0415

    raw = str(payload.get("name") or "").strip()
    if not raw:
        raise ValueError("a new piece needs a name -- it becomes the folder name")
    name = slug(raw, fallback="")
    if not name:
        raise ValueError(
            f"{raw!r} leaves nothing usable as a folder name; "
            "letters, digits, - and _ survive"
        )
    folder = _piece_folder(payload.get("folder") or name, must_exist=False)
    preset = (
        Chain.from_json(payload["preset"]).to_json()
        if payload.get("preset")
        else default_chain().to_json()
    )
    piece = new_piece(
        folder,
        name=name,
        source=dict(payload.get("source") or {}),
        preset=preset,
        stamp=payload.get("stamp"),
    )
    if payload.get("title"):
        piece.title = payload["title"]
        piece.save()
    print(f"  created piece {folder}")
    return _piece_payload(piece)


def save_piece_api(payload: dict) -> dict:
    """Write a manifest back.

    Validated by loading it before it touches the disk: an invalid manifest that
    got saved would make the piece unopenable, which is a worse failure than a
    rejected save.
    """
    from serrin.piece import Piece  # noqa: PLC0415

    folder = _piece_folder(payload.get("folder"))
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("no manifest in the request")

    piece = Piece.from_json(manifest, folder=folder)
    piece.saved_at = payload.get("stamp") or piece.saved_at
    piece.save(folder)
    print(f"  saved piece {folder}")
    return _piece_payload(piece)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def render_upload(payload: dict) -> dict:
    """Render a CSV posted from the browser, or a local repository path.

    Accepts either ``{"csv": "<text>", "name": "..."}`` or ``{"repo": "<path>"}``,
    plus optional ``preset``, ``tempo``, ``columns``, ``metric``, ``traversal``.

    Text rather than multipart: the browser reads the file with FileReader and
    posts JSON, which avoids hand-rolling a multipart parser for a one-field
    form. The cost is that the CSV is base64-free but still fully buffered, hence
    the size cap.

    Returns the URLs the page should load, so the result is consumed exactly like
    a preset -- no second code path in the runtime.
    """
    # Imported here, not at module scope: `serve.py --help` should not pay for
    # loading the pipeline, and an import error in serrin should not stop the
    # static server from working.
    from serrin.chain import Chain, default_chain
    from serrin.envelope import Envelope
    from serrin.export import MappingConfig, build_render, trace_mapping, write_json
    from serrin.ingest import ingest_csv
    from serrin.graph import ingest_graph
    from serrin.ingest_git import ingest_repo
    from serrin.session import Session
    from serrin.tempo import Tempo
    from serrin.trace import DEFAULT_WINDOW, Trace

    # Tracing is opt-in per request: it roughly doubles the work and the reply
    # size, and most renders are not being debugged.
    recorder = (
        Trace(window=int(payload.get("trace_window") or DEFAULT_WINDOW))
        if payload.get("trace")
        else None
    )

    from serrin.piece import Piece  # noqa: PLC0415

    piece = Piece.load(_piece_folder(payload["piece"])) if payload.get("piece") else None

    csv_text = payload.get("csv")
    repo_path = payload.get("repo")
    if piece is None and not csv_text and not repo_path:
        raise ValueError("send a piece folder, a csv body or a repo path")

    # -- the chain ---------------------------------------------------------
    preset_name = payload.get("preset")
    if piece is not None and not preset_name and not payload.get("preset_json"):
        # The piece holds its own chain. That is the point of it.
        chain = piece.chain()
    elif payload.get("preset_json"):
        chain = Chain.from_json(payload["preset_json"])
    elif preset_name:
        candidate = ROOT / "presets" / f"{_slug(preset_name)}.json"
        if not candidate.exists():
            raise ValueError(f"no preset named {preset_name!r}")
        chain = Chain.load(candidate)
    else:
        chain = default_chain()

    ingest_opts = dict(chain.ingest)
    tempo = Tempo.parse(payload["tempo"]) if payload.get("tempo") else (
        Tempo.parse(ingest_opts["tempo"]) if ingest_opts.get("tempo") else None
    )

    # -- the source --------------------------------------------------------
    if piece is not None and not csv_text and not repo_path:
        source = piece.source_path
        kind = piece.kind
        name = piece.name
        overrides = piece.ingest_kwargs()
        if tempo is not None:
            overrides["tempo"] = tempo
        if kind == "graph":
            stream = ingest_graph(source, **overrides)
            if recorder is not None:
                recorder.add(
                    "ingest",
                    f"read {source.name} (history file)",
                    stream,
                    params={"metric": stream.meta["git"]["metric"]},
                    detail=dict(stream.meta["git"]),
                )
        elif kind == "git":
            stream = ingest_repo(source, **overrides)
            if recorder is not None:
                recorder.add(
                    "ingest",
                    f"read {source.name} (git)",
                    stream,
                    params={
                        "metric": stream.meta["git"]["metric"],
                        "traversal": stream.meta["git"]["traversal"],
                    },
                    detail=dict(stream.meta["git"]),
                )
        else:
            stream = ingest_csv(source, trace=recorder, **overrides)
    elif csv_text:
        name = _slug(Path(str(payload.get("name") or "upload.csv")).stem)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        source = UPLOAD_DIR / f"{name}.csv"
        source.write_text(csv_text, encoding="utf-8")
        kind = "csv"
        stream = ingest_csv(
            source,
            columns=payload.get("columns") or ingest_opts.get("columns"),
            bit_depth=int(payload.get("bit_depth") or ingest_opts.get("bit_depth", 8)),
            granularity=int(payload.get("granularity") or ingest_opts.get("granularity", 1)),
            aggregation=payload.get("aggregation") or ingest_opts.get("aggregation", "mean"),
            tempo=tempo,
            log_scale=bool(payload.get("log_scale", ingest_opts.get("log_scale", False))),
            limit=payload.get("limit"),
            trace=recorder,
        )
    else:
        source = Path(str(repo_path)).expanduser()
        if not source.exists():
            raise ValueError(f"no such path: {source}")
        name = _slug(source.name, "repo")
        kind = "git"
        stream = ingest_repo(
            source,
            metric=payload.get("metric") or ingest_opts.get("metric", "hash"),
            traversal=payload.get("traversal") or ingest_opts.get("traversal", "chrono"),
            branch_names=payload.get("branches") or ingest_opts.get("branches"),
            bit_depth=int(payload.get("bit_depth") or ingest_opts.get("bit_depth", 8)),
            tempo=tempo,
            limit=payload.get("limit"),
        )
        if recorder is not None:
            recorder.add(
                "ingest",
                f"read {source.name} (git)",
                stream,
                params={
                    "metric": stream.meta["git"]["metric"],
                    "traversal": stream.meta["git"]["traversal"],
                },
                detail=dict(stream.meta["git"]),
                note=(
                    "each branch takes a value at its own commits and holds "
                    "between them, so a quiet branch becomes a quiet voice"
                ),
            )

    # -- render ------------------------------------------------------------
    seed = payload.get("seed")
    transformed = chain.apply(
        stream,
        seed=int(seed) if seed is not None else None,
        source=source,
        recorder=recorder,
    )
    mapping = MappingConfig.from_json(chain.mapping)
    envelope = Envelope.from_spec(chain.envelope) if chain.envelope else Envelope.constant(1.0)
    piece_opts = dict(chain.piece)

    rendered = build_render(
        transformed,
        chain=chain,
        envelope=envelope,
        config=mapping,
        mode=piece_opts.get("mode", "closed"),
        loop_policy=piece_opts.get("loop", "vary"),
        voice_entry=piece_opts.get("voice_entry", "variance"),
        delay_note=piece_opts.get("delay_note", "1/8."),
    )

    # The served URLs are ROOT-relative, so an upload directory outside ROOT
    # could never be fetched by the page. Said plainly here rather than failing
    # later inside relative_to() with a confusing message.
    if ROOT not in UPLOAD_DIR.resolve().parents and UPLOAD_DIR.resolve() != ROOT:
        raise ValueError(f"upload directory {UPLOAD_DIR} is outside the served root {ROOT}")

    if recorder is not None:
        trace_mapping(transformed, rendered, recorder)

    if piece is not None and not csv_text and not repo_path:
        piece.render_dir.mkdir(parents=True, exist_ok=True)
        audio_path = piece.render_dir / "audio.json"
        visual_path = piece.render_dir / "visual.json"
        write_json(audio_path, rendered.audio_document())
        write_json(visual_path, rendered.visual_document())

        piece.render = {
            "label": rendered.meta["label"],
            "fingerprint": rendered.meta["fingerprint"],
            "seed": rendered.meta["seed"],
            "audio": "out/audio.json",
            "visual": "out/visual.json",
            "frames": rendered.meta["frames"],
            "duration": rendered.meta["duration"],
            "voices": rendered.meta["voices"],
            "rendered_at": payload.get("stamp"),
        }
        piece.save()
        print(
            f"  rendered piece {piece.name}: {transformed.n_voices} voices x "
            f"{transformed.length} frames"
        )
        result = _piece_payload(piece)
        result.update(
            {
                "ok": True,
                "kind": piece.kind,
                "chain": chain.name,
                "label": rendered.meta["label"],
                "fingerprint": rendered.meta["fingerprint"],
                "seed": rendered.meta["seed"],
                "voices": rendered.meta["voices"],
                "frames": rendered.meta["frames"],
                "duration": rendered.meta["duration"],
                "bars": rendered.meta["bars"],
                "git": stream.meta.get("git"),
                "audio": result["render"].get("audio_url"),
                "visual": result["render"].get("visual_url"),
                "trace": recorder.to_json() if recorder is not None else None,
            }
        )
        return result

    target = UPLOAD_DIR / name
    audio_path = target.with_name(f"{name}_audio.json")
    visual_path = target.with_name(f"{name}_visual.json")
    write_json(audio_path, rendered.audio_document())
    write_json(visual_path, rendered.visual_document())

    session = Session(
        source={
            "path": str(source),
            "kind": kind,
            "columns": stream.names,
            "bit_depth": stream.bit_depth,
            "tempo": stream.tempo.to_json(),
            **(
                {
                    "metric": stream.meta.get("git", {}).get("metric"),
                    "traversal": stream.meta.get("git", {}).get("traversal"),
                    "branches": stream.meta.get("git", {}).get("branches"),
                }
                if kind == "git"
                else {
                    "granularity": stream.meta.get("granularity", 1),
                    "aggregation": stream.meta.get("aggregation"),
                    "log_scale": stream.meta.get("log_scale", False),
                }
            ),
        },
        preset=chain.to_json(),
        streams={
            "audio": str(audio_path.relative_to(ROOT)).replace("\\", "/"),
            "visual": str(visual_path.relative_to(ROOT)).replace("\\", "/"),
        },
        label=rendered.meta["label"],
        fingerprint=rendered.meta["fingerprint"],
    )
    session_path = target.with_name(f"{name}.session.json")
    session.save(session_path)

    print(
        f"  rendered {kind} {source.name}: {transformed.n_voices} voices x "
        f"{transformed.length} frames -> {audio_path.name}"
    )

    return {
        "ok": True,
        "label": rendered.meta["label"],
        "fingerprint": rendered.meta["fingerprint"],
        "seed": rendered.meta["seed"],
        "voices": rendered.meta["voices"],
        "frames": rendered.meta["frames"],
        "duration": rendered.meta["duration"],
        "bars": rendered.meta["bars"],
        "kind": kind,
        "chain": chain.name,
        "git": stream.meta.get("git"),
        # Paths relative to the server root, so the page loads them like any
        # other rendered pair.
        "audio": "/" + str(audio_path.relative_to(ROOT)).replace("\\", "/"),
        "visual": "/" + str(visual_path.relative_to(ROOT)).replace("\\", "/"),
        "session": "/" + str(session_path.relative_to(ROOT)).replace("\\", "/"),
        "trace": recorder.to_json() if recorder is not None else None,
    }


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default localhost; this server accepts uploads)",
    )
    parser.add_argument(
        "--pieces",
        default=None,
        help="the folder your pieces live in (default: ./pieces). Writes are "
        "confined to it.",
    )
    parser.add_argument("--preset", default=None, help="open with ?preset=NAME")
    parser.add_argument("--panel", action="store_true", help="open with the panel showing")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    global PIECES_DIR  # noqa: PLW0603 -- one setting, decided at startup
    if args.pieces:
        PIECES_DIR = Path(args.pieces).expanduser().resolve()
    PIECES_DIR.mkdir(parents=True, exist_ok=True)

    if not (ROOT / "out").exists():
        print("note: out/ does not exist yet -- render a stream first:")
        print("  python -m serrin render -i data/monitoring.csv -c presets/gritty_01.json")

    query = []
    if args.preset:
        query.append(f"preset={args.preset}")
    if args.panel:
        query.append("panel=1")
    shown = "localhost" if args.host in ("127.0.0.1", "") else args.host
    url = f"http://{shown}:{args.port}/web/" + (f"?{'&'.join(query)}" if query else "")

    handler = functools.partial(Handler, directory=str(ROOT))
    # Threaded, not the single-threaded TCPServer this started as: a browser
    # holding one connection open -- which is normal, and which the page does
    # while fetching two stream files -- blocks every other request on a serial
    # server, so the page half-loads and a second client hangs outright.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"serrin -- serving {ROOT}")
        print(f"  pieces in {PIECES_DIR}")
        print(f"  {url}")
        if args.host not in ("127.0.0.1", "localhost"):
            print(f"  reachable on {args.host}: this server accepts uploads and runs renders")
        print("  ctrl-c to stop")
        if not args.no_open:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
