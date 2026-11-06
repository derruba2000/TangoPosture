"""Local HTTP protocol fixture; no model downloads or external servers."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def ollama_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "test-model:latest"}]}).encode())

        def do_POST(self):
            server.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if server.mode == "missing":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"model not found"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            lines = [
                {"message": {"content": "## Observations\n"}, "done": False},
                {"message": {"content": "At 00:00.2 tracking was UNKNOWN.\n"}, "done": False},
                {
                    "message": {"content": "Discuss camera framing with your teacher."},
                    "done": True,
                    "done_reason": "stop",
                },
            ]
            if server.mode == "broken":
                lines = lines[:1]
            elif server.mode == "empty":
                lines = [{"message": {"content": ""}, "done": True}]
            elif server.mode == "limit":
                lines[-1]["done_reason"] = "length"
            for line in lines:
                try:
                    self.wfile.write(json.dumps(line).encode() + b"\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.mode = "normal"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
