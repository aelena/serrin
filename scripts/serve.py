"""Serve the repo root over HTTP so the browser will load the modules.

ES modules and fetch() both refuse to work from file:// URLs, so opening
web/index.html directly gets you a black page and a CORS error in the console.
This is the smallest thing that fixes that.

Serving the *repo root* rather than web/ is deliberate: the page loads its
streams from ../out/, which has to be reachable.

    python scripts/serve.py            # http://localhost:8000/web/
    python scripts/serve.py --port 9000 --no-open
"""

from __future__ import annotations

import argparse
import functools
import http.server
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, no caching -- a re-render should show up on reload."""

    # HTTP/1.1 so the browser can keep connections alive; safe only because the
    # server below is threaded.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):
        # One line per request, without the date noise.
        print(f"  {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
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
    url = f"http://localhost:{args.port}/web/" + (f"?{'&'.join(query)}" if query else "")

    handler = functools.partial(Handler, directory=str(ROOT))
    # Threaded, not the single-threaded TCPServer this started as. A browser
    # holding one connection open -- which is normal, and which the page itself
    # does while fetching two stream files -- blocks every other request on a
    # serial server, so the page would half-load and any second client would
    # hang outright.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    with http.server.ThreadingHTTPServer(("", args.port), handler) as httpd:
        print(f"serrin -- serving {ROOT}")
        print(f"  {url}")
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
