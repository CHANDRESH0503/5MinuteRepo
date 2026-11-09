"""Read-only dashboard over the append-only log.

    PYTHONPATH=src python -m fivemin.dashboard --log logs/phase1.jsonl --port 8765

Serves one static page and `/api/state`, which is `report.state()` of the log.
The server keeps no state of its own besides a parse cache of the file tail.
It binds to 127.0.0.1 unless --host says otherwise, and has no endpoint that writes or sends anything.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fivemin.report import state

PAGE = Path(__file__).parent / "ui" / "index.html"


class Tail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.records: list[dict] = []
        self.lock = threading.Lock()

    def read(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return self.records
            size = self.path.stat().st_size
            if size < self.offset:  # rotated or truncated
                self.offset, self.records = 0, []
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
            end = chunk.rfind(b"\n") + 1  # leave a half-written line for next time
            for line in chunk[:end].splitlines():
                if line.strip():
                    try:
                        self.records.append(json.loads(line))
                    except json.JSONDecodeError:
                        self.records.append({"type": "corrupt", "raw": line[:200].decode(errors="replace")})
            self.offset += end
            return self.records


def handler(tail: Tail) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/state"):
                body = json.dumps({**state(tail.read()), "log": str(tail.path)}, default=str).encode()
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=Path("logs/phase1.jsonl"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="bind address; the page is read-only either way")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), handler(Tail(args.log)))
    print(f"dashboard on http://{args.host}:{args.port}  log={args.log}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
