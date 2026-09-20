from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path

MAX_POST_BYTES = 4096
POST_READ_TIMEOUT_S = 2.0   # applies to POST requests only; see _read_json_body

class WebHandler(BaseHTTPRequestHandler):
    state = None
    publisher = None   # UdpPublisher or None (UDP push disabled)
    web_root = Path(__file__).resolve().parent

    def _send_bytes(self, content: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, obj, code: int = 200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", code)

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

        if self.path == "/subscribers":
            if self.publisher is None:
                return self._send_json({"error": "UDP push is not enabled"}, 503)
            return self._send_json({"boot": self.publisher.boot,
                                    "subscribers": self.publisher.subscribers()})

        return self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def _read_json_body(self) -> dict:
        # HTTPServer here is single-threaded, so a client that announces a body and then
        # stalls would block every other request. Limit that for POST only (GET is unchanged).
        self.connection.settimeout(POST_READ_TIMEOUT_S)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _BadRequest(411, "Content-Length required")
        if length < 0 or length > MAX_POST_BYTES:
            raise _BadRequest(413, f"body must be at most {MAX_POST_BYTES} bytes")
        try:
            raw = self.rfile.read(length)
        except OSError:
            raise _BadRequest(408, "timed out reading the request body")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _BadRequest(400, "body must be valid JSON")
        if not isinstance(body, dict):
            raise _BadRequest(400, "body must be a JSON object")
        return body

    def do_POST(self):
        if self.path not in ("/subscribe", "/unsubscribe"):
            return self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
        if self.publisher is None:
            return self._send_json({"error": "UDP push is not enabled"}, 503)
        try:
            body = self._read_json_body()
            requester = self.client_address[0]
            if self.path == "/subscribe":
                result = self.publisher.subscribe(
                    requester, body.get("port"), body.get("host"), body.get("lease_s"))
            else:
                removed = self.publisher.unsubscribe(requester, body.get("port"), body.get("host"))
                result = {"removed": removed}
            return self._send_json(result)
        except _BadRequest as e:
            return self._send_json({"error": e.message}, e.status)
        except Exception as e:
            status = getattr(e, "status", None)   # SubscribeError carries the HTTP status
            if status is None:
                print(f"[Web] POST {self.path} failed: {e!r}", flush=True)
                return self._send_json({"error": "internal error"}, 500)
            return self._send_json({"error": str(e)}, status)

class _BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

def run_web(state, port: int = 8080, publisher=None):
    WebHandler.state = state
    WebHandler.publisher = publisher
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    print(f"[Web] http://localhost:{port}")
    server.serve_forever()
