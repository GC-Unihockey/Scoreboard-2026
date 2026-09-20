import json
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import GameState
from outputs import udp_publisher as up
from outputs.udp_publisher import UdpPublisher, SubscribeError


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSock:
    """Records sendto() calls; raises OSError for addresses listed in `fail`."""
    def __init__(self):
        self.sent = []
        self.fail = set()

    def sendto(self, data, addr):
        if addr in self.fail:
            raise OSError("unreachable")
        self.sent.append((addr, json.loads(data.decode("utf-8")), len(data)))


def make_state():
    s = GameState()
    s.home.name, s.away.name = "GCZ", "ZUG"
    s.clock = "12:34"
    return s


def make_pub(state=None, **kw):
    state = state or make_state()
    kw.setdefault("sock", FakeSock())
    return state, UdpPublisher(state, **kw)


def worst_case_state():
    s = GameState()
    s.home.name = s.away.name = "X" * 12
    s.home.score = s.away.score = 999
    s.home.penalties = s.away.penalties = ["9999"] * 3
    s.clock = "99:59"
    s.summary.top = "RESULTAT NACH VERLAENGERUNG"
    s.summary.bottom = "(99:99, 99:99, 99:99, 99:99)"
    s.summary.main = "999:999 (n.V.)"
    s.sport, s.period_number, s.period_display = 4, 3, "O"
    return s


class SnapshotTests(unittest.TestCase):
    def test_slot_is_seeded_with_as_dict(self):
        state, pub = make_pub()
        self.assertEqual(pub._slot, state.as_dict())

    def test_on_frame_stores_latest_and_sets_event(self):
        state, pub = make_pub()
        state.home.score = 3
        pub.on_frame(state)
        self.assertEqual(pub._slot["home"]["score"], 3)
        self.assertTrue(pub._wake.is_set())
        self.assertIsNotNone(pub._last_frame)

    def test_snapshot_is_decoupled_from_state_lists(self):
        state, pub = make_pub()
        pub.on_frame(state)
        state.home.penalties[0] = "0200"     # in-place mutation of the state's list
        self.assertEqual(pub._slot["home"]["penalties"][0], "")

    def test_on_frame_never_raises(self):
        class Broken:
            def as_dict(self):
                raise RuntimeError("boom")
        _, pub = make_pub()
        pub.on_frame(Broken())               # must not raise
        self.assertIsNone(pub._last_frame)


class PayloadTests(unittest.TestCase):
    def test_header_fields_and_state(self):
        state, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        (addr, msg, size), = pub._sock.sent
        self.assertEqual(addr, ("10.0.0.5", 5005))
        self.assertEqual(list(msg), ["v", "boot", "seq", "ts", "changed_ts", "reason",
                                     "stale", "source_age_ms", "state"])
        self.assertEqual(msg["v"], up.SCHEMA_VERSION)
        self.assertEqual(msg["boot"], pub.boot)
        self.assertEqual(msg["seq"], 1)
        self.assertEqual(msg["reason"], "subscribe")
        self.assertIs(msg["stale"], True)             # no frame parsed yet
        self.assertIsNone(msg["source_age_ms"])
        self.assertEqual(msg["state"], state.as_dict())

    def test_boot_id_differs_per_instance(self):
        _, a = make_pub()
        _, b = make_pub()
        self.assertNotEqual(a.boot, b.boot)

    def test_seq_is_per_subscriber_and_increments(self):
        state, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        pub.subscribe("10.0.0.6", 5006)
        pub._send_pending()                           # only the new one gets the snapshot
        pub._broadcast(pub._last_snap, "heartbeat")
        by_addr = {}
        for addr, msg, _ in pub._sock.sent:
            by_addr.setdefault(addr, []).append((msg["seq"], msg["reason"]))
        self.assertEqual(by_addr[("10.0.0.5", 5005)], [(1, "subscribe"), (2, "heartbeat")])
        self.assertEqual(by_addr[("10.0.0.6", 5006)], [(1, "subscribe"), (2, "heartbeat")])

    def test_size_limit_worst_case_fits(self):
        _, pub = make_pub(worst_case_state())
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        (_, _, size), = pub._sock.sent
        self.assertLess(size, up.MAX_DATAGRAM_BYTES)

    def test_oversize_datagram_is_not_sent_and_does_not_raise(self):
        state = worst_case_state()
        state.summary.bottom = "Y" * 2000
        _, pub = make_pub(state)
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        self.assertEqual(pub._sock.sent, [])

    def test_stale_detection(self):
        clock = FakeClock()
        state, pub = make_pub(monotonic=clock)
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        self.assertTrue(pub._sock.sent[-1][1]["stale"])
        pub.on_frame(state)
        pub._broadcast(pub._slot, "heartbeat")
        msg = pub._sock.sent[-1][1]
        self.assertFalse(msg["stale"])
        self.assertEqual(msg["source_age_ms"], 0)
        clock.t += up.STALE_AFTER_S + 1
        pub._broadcast(pub._slot, "heartbeat")
        msg = pub._sock.sent[-1][1]
        self.assertTrue(msg["stale"])
        self.assertGreater(msg["source_age_ms"], up.STALE_AFTER_S * 1000)

    def test_changed_ts_moves_only_on_change(self):
        wall = FakeClock(100.0)
        _, pub = make_pub(wall=wall)
        pub.subscribe("10.0.0.5", 5005)
        t0 = pub._changed_ts
        wall.t = 200.0
        pub._broadcast(pub._last_snap, "heartbeat")
        self.assertEqual(pub._sock.sent[-1][1]["changed_ts"], t0)
        wall.t = 300.0
        pub._broadcast(pub._last_snap, "change")
        self.assertEqual(pub._sock.sent[-1][1]["changed_ts"], 300000)


class LeaseTests(unittest.TestCase):
    def test_default_and_clamped_lease(self):
        _, pub = make_pub()
        self.assertEqual(pub.subscribe("10.0.0.5", 5005)["lease_s"], 3600)
        self.assertEqual(pub.subscribe("10.0.0.5", 5005, lease_s=10**6)["lease_s"], 7200)
        self.assertEqual(pub.subscribe("10.0.0.5", 5005, lease_s=30)["lease_s"], 30)

    def test_expiry(self):
        clock = FakeClock()
        _, pub = make_pub(monotonic=clock)
        pub.subscribe("10.0.0.5", 5005, lease_s=60)
        clock.t += 59
        pub._expire_leases()
        self.assertEqual(len(pub.subscribers()), 1)
        clock.t += 2
        pub._expire_leases()
        self.assertEqual(pub.subscribers(), [])

    def test_renewal_extends_and_does_not_duplicate(self):
        clock = FakeClock()
        _, pub = make_pub(monotonic=clock)
        first = pub.subscribe("10.0.0.5", 5005, lease_s=60)
        clock.t += 50
        second = pub.subscribe("10.0.0.5", 5005, lease_s=60)
        self.assertFalse(first["renewed"])
        self.assertTrue(second["renewed"])
        self.assertEqual(len(pub.subscribers()), 1)
        clock.t += 50                                  # 100 s after start, 50 s after renewal
        pub._expire_leases()
        self.assertEqual(len(pub.subscribers()), 1)

    def test_renewal_does_not_resend_snapshot(self):
        _, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        self.assertEqual(len(pub._sock.sent), 1)

    def test_unsubscribe(self):
        _, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        self.assertTrue(pub.unsubscribe("10.0.0.5", 5005))
        self.assertFalse(pub.unsubscribe("10.0.0.5", 5005))
        self.assertEqual(pub.subscribers(), [])


class ValidationTests(unittest.TestCase):
    def status(self, pub, *a, **kw):
        with self.assertRaises(SubscribeError) as cm:
            pub.subscribe(*a, **kw)
        return cm.exception.status

    def test_subscriber_cap(self):
        _, pub = make_pub()
        for i in range(up.MAX_SUBSCRIBERS):
            pub.subscribe("10.0.0.5", 5000 + 1024 + i)
        self.assertEqual(self.status(pub, "10.0.0.5", 9000), 429)
        pub.subscribe("10.0.0.5", 5000 + 1024)         # renewing an existing one still works
        self.assertEqual(len(pub.subscribers()), up.MAX_SUBSCRIBERS)

    def test_port_rules(self):
        _, pub = make_pub()
        for bad in (None, "5005", 1023, 0, 65536, True, 5005.0):
            self.assertEqual(self.status(pub, "10.0.0.5", bad), 400, bad)
        pub.subscribe("10.0.0.5", 1024)
        pub.subscribe("10.0.0.5", 65535)

    def test_lease_rules(self):
        _, pub = make_pub()
        for bad in (0, -5, "60", 1.5, True):
            self.assertEqual(self.status(pub, "10.0.0.5", 5005, lease_s=bad), 400, bad)

    def test_host_must_be_unicast_ipv4(self):
        _, pub = make_pub(allowed_subnets=("0.0.0.0/0",))
        for bad in ("example.com", "localhost", "224.0.0.1", "255.255.255.255", "0.0.0.0", "::1", 5, ""):
            self.assertEqual(self.status(pub, "10.0.0.5", 5005, host=bad), 400, bad)

    def test_foreign_host_needs_allowed_subnet(self):
        _, pub = make_pub()
        self.assertEqual(self.status(pub, "10.0.0.5", 5005, host="10.0.0.9"), 403)
        pub.subscribe("10.0.0.5", 5005, host="10.0.0.5")      # naming yourself is fine
        _, pub = make_pub(allowed_subnets=("10.0.0.0/24",))
        self.assertEqual(pub.subscribe("10.0.0.5", 5005, host="10.0.0.9")["host"], "10.0.0.9")
        self.assertEqual(self.status(pub, "10.0.0.5", 5005, host="10.0.1.9"), 403)

    def test_non_ipv4_requester_is_rejected(self):
        _, pub = make_pub()
        self.assertEqual(self.status(pub, "::1", 5005), 400)


class ErrorIsolationTests(unittest.TestCase):
    def test_one_failing_target_does_not_affect_others(self):
        _, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub.subscribe("10.0.0.6", 5006)
        pub._sock.fail.add(("10.0.0.5", 5005))
        pub._send_pending()
        pub._broadcast(pub._last_snap, "heartbeat")
        good = [a for a, _, _ in pub._sock.sent]
        self.assertEqual(good, [("10.0.0.6", 5006)] * 2)
        by = {s["port"]: s for s in pub.subscribers()}
        self.assertEqual(by[5005]["errors"], 2)
        self.assertEqual(by[5006]["errors"], 0)

    def test_dropped_after_max_consecutive_errors(self):
        _, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub.subscribe("10.0.0.6", 5006)
        pub._sock.fail.add(("10.0.0.5", 5005))
        pub._send_pending()
        for _ in range(up.MAX_SEND_ERRORS):
            pub._broadcast(pub._last_snap, "heartbeat")
        self.assertEqual([s["port"] for s in pub.subscribers()], [5006])

    def test_success_resets_error_count(self):
        _, pub = make_pub()
        pub.subscribe("10.0.0.5", 5005)
        pub._send_pending()
        pub._sock.fail.add(("10.0.0.5", 5005))
        for _ in range(up.MAX_SEND_ERRORS - 1):
            pub._broadcast(pub._last_snap, "heartbeat")
        pub._sock.fail.clear()
        pub._broadcast(pub._last_snap, "heartbeat")
        self.assertEqual(pub.subscribers()[0]["errors"], 0)
        pub._sock.fail.add(("10.0.0.5", 5005))
        for _ in range(up.MAX_SEND_ERRORS - 1):
            pub._broadcast(pub._last_snap, "heartbeat")
        self.assertEqual(len(pub.subscribers()), 1)


class ThreadTests(unittest.TestCase):
    """Real sender thread + real UDP on loopback, with short timings."""

    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.port = self.rx.getsockname()[1]
        self.state = make_state()
        self.pub = UdpPublisher(self.state, heartbeat_s=0.2, coalesce_s=0.05)
        self.pub.start()

    def tearDown(self):
        self.pub.stop()
        self.rx.close()

    def recv(self, timeout=1.0):
        self.rx.settimeout(timeout)
        return json.loads(self.rx.recvfrom(4096)[0].decode("utf-8"))

    def drain(self, duration=0.4):
        """Collect everything that arrives during `duration` seconds (heartbeats keep coming)."""
        out = []
        end = time.monotonic() + duration
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return out
            try:
                out.append(self.recv(left))
            except socket.timeout:
                return out

    def test_subscribe_change_and_heartbeat(self):
        if self.port < up.MIN_PORT:
            self.skipTest("ephemeral port below MIN_PORT")
        self.pub.subscribe("127.0.0.1", self.port)
        first = self.recv()
        self.assertEqual(first["reason"], "subscribe")
        self.assertEqual(first["state"]["clock"], "12:34")

        self.state.clock = "12:35"
        self.pub.on_frame(self.state)
        got = self.recv()
        while got["reason"] == "heartbeat":            # a heartbeat may slip in first
            got = self.recv()
        self.assertEqual(got["reason"], "change")
        self.assertEqual(got["state"]["clock"], "12:35")
        self.assertFalse(got["stale"])

        self.pub.on_frame(self.state)                  # same content again: no "change"
        msgs = self.drain(0.5)
        self.assertTrue(msgs)
        self.assertTrue(all(m["reason"] == "heartbeat" for m in msgs))
        seqs = [m["seq"] for m in [got] + msgs]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))

    def test_coalescing_latest_wins(self):
        if self.port < up.MIN_PORT:
            self.skipTest("ephemeral port below MIN_PORT")
        self.pub.subscribe("127.0.0.1", self.port)
        self.recv()
        for i in range(30):
            self.state.home.score = i + 1
            self.pub.on_frame(self.state)
            time.sleep(0.002)
        msgs = [m for m in self.drain(0.5) if m["reason"] == "change"]
        self.assertLess(len(msgs), 30)
        self.assertEqual(msgs[-1]["state"]["home"]["score"], 30)
        for a, b in zip(msgs, msgs[1:]):
            self.assertGreaterEqual(b["ts"] - a["ts"], 40)   # ~50 ms minimum spacing

    def test_lease_expiry_stops_sending(self):
        if self.port < up.MIN_PORT:
            self.skipTest("ephemeral port below MIN_PORT")
        self.pub.subscribe("127.0.0.1", self.port, lease_s=1)
        self.recv()
        time.sleep(1.5)
        self.drain(0.1)
        self.assertEqual(self.pub.subscribers(), [])
        with self.assertRaises(socket.timeout):
            self.recv(0.5)


if __name__ == "__main__":
    unittest.main()
