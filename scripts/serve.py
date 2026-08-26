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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Uploads land here, under out/, which is gitignored.
UPLOAD_DIR = ROOT / "out" / "uploads"

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

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")

    # -- helpers ------------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    def do_POST(self):  # noqa: N802 -- the base class spells it this way
        if self.path.rstrip("/") != "/api/render":
            self._send_json(404, {"error": f"no such endpoint: {self.path}"})
            return
        try:
            payload = self._read_json()
            self._send_json(200, render_upload(payload))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            # The browser gets one line; the terminal gets the traceback, because
            # this is the author's own machine and hiding it helps nobody.
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# the one endpoint
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
    from serrin.export import MappingConfig, build_piece, trace_mapping, write_json
    from serrin.ingest import ingest_csv
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

    csv_text = payload.get("csv")
    repo_path = payload.get("repo")
    if not csv_text and not repo_path:
        raise ValueError("send either a csv body or a repo path")

    # -- the chain ---------------------------------------------------------
    preset_name = payload.get("preset")
    if payload.get("preset_json"):
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
    if csv_text:
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

    piece = build_piece(
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
        trace_mapping(transformed, piece, recorder)

    target = UPLOAD_DIR / name
    audio_path = target.with_name(f"{name}_audio.json")
    visual_path = target.with_name(f"{name}_visual.json")
    write_json(audio_path, piece.audio_document())
    write_json(visual_path, piece.visual_document())

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
        label=piece.meta["label"],
        fingerprint=piece.meta["fingerprint"],
    )
    session_path = target.with_name(f"{name}.session.json")
    session.save(session_path)

    print(
        f"  rendered {kind} {source.name}: {transformed.n_voices} voices x "
        f"{transformed.length} frames -> {audio_path.name}"
    )

    return {
        "ok": True,
        "label": piece.meta["label"],
        "fingerprint": piece.meta["fingerprint"],
        "seed": piece.meta["seed"],
        "voices": piece.meta["voices"],
        "frames": piece.meta["frames"],
        "duration": piece.meta["duration"],
        "bars": piece.meta["bars"],
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
    parser.add_argument("--preset", default=None, help="open with ?preset=NAME")
    parser.add_argument("--panel", action="store_true", help="open with the panel showing")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

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
