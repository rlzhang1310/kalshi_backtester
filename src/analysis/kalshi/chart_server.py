"""Background loopback chart server for the Streamlit game UI."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit


class ChartServer:
    def __init__(self, html: bytes, session=None) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                request = urlsplit(self.path)
                if request.path == "/":
                    body, content_type, status = html, "text/html; charset=utf-8", 200
                elif request.path == "/snapshot" and session is not None:
                    try:
                        since = int(parse_qs(request.query).get("since", ["0"])[0])
                        body = json.dumps(session.snapshot(since)).encode("utf-8")
                        status = 200
                    except Exception as exc:
                        body = json.dumps({"error": str(exc)}).encode("utf-8")
                        status = 503
                    content_type = "application/json; charset=utf-8"
                else:
                    body, content_type, status = b"Not found", "text/plain", 404
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args) -> None:
                del args

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
