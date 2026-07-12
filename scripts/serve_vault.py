#!/usr/bin/env python3
"""
Local viewer server for the FinOKF Markdown vault.

Serves the ``ui/`` app and the ``data/processed/`` vault over HTTP, and exposes a small
``POST /api/save`` endpoint so the browser can write edited Markdown back to disk. Editing needs a
server because a static ``file://`` page cannot persist changes.

Run:
    python3 scripts/serve_vault.py
then open the printed URL (default http://127.0.0.1:8770/ui/index.html).

Safety: saves are restricted to existing ``*.md`` files inside the processed vault; any path that
resolves outside it is rejected.
"""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PROCESSED: Path = ROOT / "data" / "processed"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        # Editing is live: never cache markdown/index responses.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/save":
            self.send_error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            rel = str(payload["path"])
            content = str(payload["content"])
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return

        target = (PROCESSED / rel).resolve()
        if target != PROCESSED and PROCESSED not in target.parents:
            self._json(403, {"ok": False, "error": "path outside vault"})
            return
        if target.suffix != ".md":
            self._json(403, {"ok": False, "error": "only .md files are editable"})
            return
        if not target.exists():
            self._json(404, {"ok": False, "error": "file not found"})
            return
        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "bytes": len(content.encode("utf-8"))})

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # keep the console quiet
        return


def main() -> int:
    global PROCESSED
    parser = argparse.ArgumentParser(description="Serve the FinOKF vault viewer with markdown saving.")
    parser.add_argument("--processed-dir", default="data/processed", help="Processed vault directory.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    PROCESSED = (ROOT / args.processed_dir).resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/ui/index.html"
    print(f"FinOKF vault viewer running at:\n    {url}\nEditing writes to: {PROCESSED}\nPress Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
