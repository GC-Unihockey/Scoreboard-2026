from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path

class WebHandler(BaseHTTPRequestHandler):
    state = None
    web_root = Path(__file__).resolve().parent

    def _send_bytes(self, content: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            p = self.web_root / "index.html"
            return self._send_bytes(p.read_bytes(), "text/html; charset=utf-8")

        if self.path == "/app.js":
            p = self.web_root / "app.js"
            return self._send_bytes(p.read_bytes(), "application/javascript; charset=utf-8")

        if self.path == "/state":
            payload = json.dumps(self.state.as_dict(), ensure_ascii=False).encode("utf-8")
            return self._send_bytes(payload, "application/json; charset=utf-8")

        return self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

def run_web(state, port: int = 8080):
    WebHandler.state = state
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    print(f"[Web] http://localhost:{port}")
    server.serve_forever()
