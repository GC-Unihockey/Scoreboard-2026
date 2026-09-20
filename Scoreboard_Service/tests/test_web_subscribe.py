import http.client
import json
import os
import sys
import threading
import unittest
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import GameState
from outputs.udp_publisher import UdpPublisher
from web.server import WebHandler, MAX_POST_BYTES


class NullSock:
    def sendto(self, data, addr):
        pass


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = GameState()
        cls.pub = UdpPublisher(cls.state, sock=NullSock())     # thread not started
        WebHandler.state = cls.state
        WebHandler.publisher = cls.pub
        cls.server = HTTPServer(("127.0.0.1", 0), WebHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        WebHandler.publisher = None

    def setUp(self):
        with self.pub._lock:
            self.pub._subs.clear()

    def req(self, method, path, body=None, raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, payload

    def post(self, path, body):
        status, payload = self.req("POST", path, body)
        return status, json.loads(payload)

    def test_state_endpoint_unchanged(self):
        status, payload = self.req("GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), self.state.as_dict())
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/nope")[0], 404)

    def test_subscribe_defaults_host_to_requester_and_renews(self):
        status, r = self.post("/subscribe", {"port": 5005})
        self.assertEqual(status, 200)
        self.assertEqual((r["host"], r["port"], r["renewed"], r["lease_s"]), ("127.0.0.1", 5005, False, 3600))
        self.assertEqual(r["boot"], self.pub.boot)
        self.assertEqual(r["v"], 1)
        status, r = self.post("/subscribe", {"port": 5005, "lease_s": 120})
        self.assertTrue(r["renewed"])
        self.assertEqual(r["lease_s"], 120)
        status, payload = self.req("GET", "/subscribers")
        subs = json.loads(payload)["subscribers"]
        self.assertEqual([(s["host"], s["port"]) for s in subs], [("127.0.0.1", 5005)])

    def test_unsubscribe(self):
        self.post("/subscribe", {"port": 5005})
        self.assertEqual(self.post("/unsubscribe", {"port": 5005}), (200, {"removed": True}))
        self.assertEqual(self.post("/unsubscribe", {"port": 5005}), (200, {"removed": False}))

    def test_errors(self):
        self.assertEqual(self.post("/subscribe", {})[0], 400)
        self.assertEqual(self.post("/subscribe", {"port": 80})[0], 400)
        self.assertEqual(self.post("/subscribe", {"port": 5005, "host": "10.9.9.9"})[0], 403)
        self.assertEqual(self.req("POST", "/subscribe", raw=b"not json")[0], 400)
        self.assertEqual(self.req("POST", "/subscribe", raw=b"[1]")[0], 400)
        self.assertEqual(self.req("POST", "/subscribe", raw=b"x" * (MAX_POST_BYTES + 1))[0], 413)
        self.assertEqual(self.req("POST", "/other", {"port": 5005})[0], 404)

    def test_missing_content_length(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/subscribe")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 411)
        c.close()

    def test_disabled_publisher_answers_503_and_state_still_works(self):
        WebHandler.publisher = None
        try:
            self.assertEqual(self.req("POST", "/subscribe", {"port": 5005})[0], 503)
            self.assertEqual(self.req("GET", "/subscribers")[0], 503)
            self.assertEqual(self.req("GET", "/state")[0], 200)
        finally:
            WebHandler.publisher = self.pub


if __name__ == "__main__":
    unittest.main()
