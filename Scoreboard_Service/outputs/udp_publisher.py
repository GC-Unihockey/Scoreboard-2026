import ipaddress
import json
import secrets
import socket
import threading
import time

# UDP push of the scoreboard state to subscribed receivers.
# Schema and endpoints: docs/UDP_PUSH.md
#
# Design:
# - The main (serial) loop only calls on_frame(): it stores a snapshot in a single
#   slot (latest wins) and sets an Event. Nothing else runs there and it never raises.
# - One daemon thread does all socket work: change detection and coalescing,
#   heartbeats, subscriber leases, stale detection, sending.
# - Receivers subscribe over HTTP (see web/server.py); the list lives in memory only.

SCHEMA_VERSION = 1

HEARTBEAT_S = 1.0             # send the last snapshot again this long after the last send
COALESCE_S = 0.05             # minimum spacing between "change" datagrams
STALE_AFTER_S = 10.0          # no serial frame parsed for this long => stale
DEFAULT_LEASE_S = 3600
MAX_LEASE_S = 7200
MAX_SUBSCRIBERS = 8
MIN_PORT = 1024
MAX_SEND_ERRORS = 10          # consecutive OSErrors before a subscriber is dropped
MAX_DATAGRAM_BYTES = 1200
LOG_INTERVAL_S = 30.0         # rate limit per log key

# A subscriber may name a host other than the requester's IP only inside these
# networks, e.g. ("192.168.10.0/24",). Empty = requester's own IP only.
ALLOWED_SUBNETS = ()


class SubscribeError(Exception):
    """Invalid subscribe/unsubscribe request. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _Subscriber:
    __slots__ = ("host", "port", "expires", "seq", "sent", "errors", "pending")

    def __init__(self, host: str, port: int, expires: float):
        self.host = host
        self.port = port
        self.expires = expires
        self.seq = 0
        self.sent = 0
        self.errors = 0
        self.pending = True   # still owed its initial "subscribe" snapshot


class UdpPublisher:
    def __init__(self, state, *, heartbeat_s=HEARTBEAT_S, coalesce_s=COALESCE_S,
                 allowed_subnets=ALLOWED_SUBNETS, sock=None,
                 monotonic=time.monotonic, wall=time.time):
        self.boot = secrets.token_hex(4)
        self.heartbeat_s = heartbeat_s
        self._coalesce_s = coalesce_s
        self._allowed = [ipaddress.ip_network(n) for n in allowed_subnets]
        self._mono = monotonic
        self._wall = wall

        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # unconnected: sendto() only
            sock.setblocking(False)
        self._sock = sock

        self._lock = threading.Lock()   # guards _subs and the fields the HTTP threads touch
        self._subs = {}                 # (host, port) -> _Subscriber

        self._slot = self._snapshot(state)   # written by the main loop, read by the sender thread
        self._last_snap = self._slot
        self._changed_ts = self._now_ms()
        self._last_frame = None              # monotonic time of the last parsed frame
        self._last_broadcast = self._mono()

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._log_last = {}

    # ---------- main-loop side ----------

    @staticmethod
    def _snapshot(state) -> dict:
        snap = state.as_dict()
        # as_dict() hands out the state's own penalty lists; copy so later changes can't leak in
        for team in ("home", "away"):
            snap[team]["penalties"] = list(snap[team]["penalties"])
        return snap

    def on_frame(self, state) -> None:
        """Called by the serial loop after frames were parsed. Never raises."""
        try:
            self._slot = self._snapshot(state)
            self._last_frame = self._mono()
            self._wake.set()
        except Exception as e:
            self._log("on_frame", f"snapshot failed: {e!r}")

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="udp-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ---------- subscriptions (called from HTTP threads) ----------

    def subscribe(self, requester_ip: str, port, host=None, lease_s=None) -> dict:
        host = self._check_host(requester_ip, host)
        port = self._check_port(port)
        lease_s = self._check_lease(lease_s)
        now = self._mono()
        with self._lock:
            sub = self._subs.get((host, port))
            renewed = sub is not None
            if sub is None:
                if len(self._subs) >= MAX_SUBSCRIBERS:
                    raise SubscribeError(429, f"subscriber limit reached ({MAX_SUBSCRIBERS})")
                sub = _Subscriber(host, port, now + lease_s)
                self._subs[(host, port)] = sub
            sub.expires = now + lease_s
            sub.errors = 0
        if not renewed:
            self._log_now(f"subscribed {host}:{port} lease {lease_s}s")
            self._wake.set()
        return {
            "v": SCHEMA_VERSION,
            "boot": self.boot,
            "host": host,
            "port": port,
            "renewed": renewed,
            "lease_s": lease_s,
            "lease_expires_ts": self._now_ms() + lease_s * 1000,
            "heartbeat_s": self.heartbeat_s,
        }

    def unsubscribe(self, requester_ip: str, port, host=None) -> bool:
        host = self._check_host(requester_ip, host)
        port = self._check_port(port)
        with self._lock:
            removed = self._subs.pop((host, port), None) is not None
        if removed:
            self._log_now(f"unsubscribed {host}:{port}")
        return removed

    def subscribers(self) -> list:
        now = self._mono()
        with self._lock:
            return [{"host": s.host, "port": s.port,
                     "lease_remaining_s": max(0, round(s.expires - now)),
                     "seq": s.seq, "sent": s.sent, "errors": s.errors}
                    for s in self._subs.values()]

    def _check_host(self, requester_ip, host) -> str:
        text = requester_ip if host is None else host
        try:
            addr = ipaddress.IPv4Address(text)
        except (ValueError, TypeError):
            raise SubscribeError(400, "host must be an IPv4 address (hostnames are not resolved)")
        if addr.is_multicast or addr.is_unspecified or addr == ipaddress.IPv4Address("255.255.255.255"):
            raise SubscribeError(400, "host must be a unicast address")
        if host is not None and str(addr) != requester_ip and not any(addr in n for n in self._allowed):
            raise SubscribeError(403, "host differs from the requester and is not in ALLOWED_SUBNETS")
        return str(addr)

    @staticmethod
    def _check_port(port) -> int:
        if isinstance(port, bool) or not isinstance(port, int):
            raise SubscribeError(400, "port is required and must be an integer")
        if not MIN_PORT <= port <= 65535:
            raise SubscribeError(400, f"port must be {MIN_PORT}..65535")
        return port

    @staticmethod
    def _check_lease(lease_s) -> int:
        if lease_s is None:
            return DEFAULT_LEASE_S
        if isinstance(lease_s, bool) or not isinstance(lease_s, int) or lease_s < 1:
            raise SubscribeError(400, "lease_s must be an integer >= 1")
        return min(lease_s, MAX_LEASE_S)

    # ---------- sender thread ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._step()
            except Exception as e:
                self._log("loop", f"sender loop error: {e!r}")
                self._stop.wait(0.5)

    def _step(self) -> None:
        self._wake.wait(max(0.0, self._last_broadcast + self.heartbeat_s - self._mono()))
        self._wake.clear()          # clear BEFORE reading the slot so no update is lost
        if self._stop.is_set():
            return

        self._expire_leases()
        self._send_pending()

        snap = self._slot
        if snap != self._last_snap:
            wait_left = self._last_broadcast + self._coalesce_s - self._mono()
            if wait_left > 0:
                self._stop.wait(wait_left)
                snap = self._slot   # latest wins
            if snap != self._last_snap:
                self._broadcast(snap, "change")
                return
        if self._mono() - self._last_broadcast >= self.heartbeat_s:
            self._broadcast(self._last_snap, "heartbeat")

    def _expire_leases(self, now=None) -> None:
        now = self._mono() if now is None else now
        with self._lock:
            gone = [k for k, s in self._subs.items() if s.expires <= now]
            for k in gone:
                del self._subs[k]
        for host, port in gone:
            self._log_now(f"lease expired {host}:{port}")

    def _send_pending(self) -> None:
        with self._lock:
            pending = [s for s in self._subs.values() if s.pending]
            for s in pending:
                s.pending = False
        if pending:
            self._deliver(pending, self._slot, "subscribe")

    def _broadcast(self, snap: dict, reason: str) -> None:
        if reason == "change":
            self._changed_ts = self._now_ms()
        self._last_snap = snap
        self._last_broadcast = self._mono()
        with self._lock:
            subs = list(self._subs.values())
        if subs:
            self._deliver(subs, snap, reason)

    def _deliver(self, subs, snap: dict, reason: str) -> None:
        now = self._mono()
        age_ms = None if self._last_frame is None else int((now - self._last_frame) * 1000)
        msg = {
            "v": SCHEMA_VERSION,
            "boot": self.boot,
            "seq": 0,
            "ts": self._now_ms(),
            "changed_ts": self._changed_ts,
            "reason": reason,
            "stale": age_ms is None or age_ms > STALE_AFTER_S * 1000,
            "source_age_ms": age_ms,
            "state": snap,
        }
        for sub in subs:
            sub.seq += 1
            msg["seq"] = sub.seq
            data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_DATAGRAM_BYTES:
                self._log("size", f"datagram {len(data)} bytes exceeds {MAX_DATAGRAM_BYTES}, not sent")
                continue
            try:
                self._sock.sendto(data, (sub.host, sub.port))
                sub.sent += 1
                sub.errors = 0
            except OSError as e:
                sub.errors += 1
                self._log(f"send {sub.host}:{sub.port}", f"send to {sub.host}:{sub.port} failed: {e!r}")
                if sub.errors >= MAX_SEND_ERRORS:
                    with self._lock:
                        self._subs.pop((sub.host, sub.port), None)
                    self._log_now(f"dropped {sub.host}:{sub.port} after {sub.errors} send errors")

    # ---------- helpers ----------

    def _now_ms(self) -> int:
        return int(self._wall() * 1000)

    def _log_now(self, text: str) -> None:
        print(f"[UDP] {text}", flush=True)

    def _log(self, key, text: str) -> None:
        now = self._mono()
        last = self._log_last.get(key)
        if last is None or now - last >= LOG_INTERVAL_S:
            self._log_last[key] = now
            print(f"[UDP] {text}", flush=True)
