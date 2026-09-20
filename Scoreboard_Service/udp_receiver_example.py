#!/usr/bin/env python3
"""Standalone receiver for the scoreboard UDP push (standard library only).

Subscribes via HTTP, keeps the lease renewed, listens on a UDP port and prints every
datagram. Protocol: docs/UDP_PUSH.md

Example (on the receiving machine):
    python3 udp_receiver_example.py --server http://SCOREBOARD_IP:8080 --port 5005
"""
import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request

SILENCE_RESUBSCRIBE_S = 5.0   # heartbeats arrive every ~1 s; silence means we lost our subscription


def post(server, path, body):
    req = urllib.request.Request(server.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def subscribe(args):
    body = {"port": args.port}
    if args.host:
        body["host"] = args.host
    if args.lease:
        body["lease_s"] = args.lease
    return post(args.server, "/subscribe", body)


def summary(msg):
    st = msg["state"]
    pens = lambda team: ",".join(p.strip() for p in st[team]["penalties"] if p.strip()) or "-"
    return (f"{st['home']['name'] or '?'} {st['home']['score']}:{st['away']['score']} {st['away']['name'] or '?'} | "
            f"clock {st['clock']!r} {'run' if st['clock_running'] else 'stop'} | period {st['period_display']!r} | "
            f"pen H[{pens('home')}] A[{pens('away')}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="scoreboard HTTP base URL, e.g. http://192.168.10.20:8080")
    ap.add_argument("--port", type=int, required=True, help="local UDP port to listen on (>= 1024)")
    ap.add_argument("--host", help="IP the scoreboard should send to (default: this machine's IP as seen by the server)")
    ap.add_argument("--lease", type=int, help="requested lease in seconds (server default 3600, max 7200)")
    ap.add_argument("--json", action="store_true", help="pretty-print the full JSON of every datagram")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(1.0)

    lease_s, renew_at, boot, last_seq, last_rx = 0, 0.0, None, {}, time.monotonic()

    def do_subscribe():
        nonlocal lease_s, renew_at
        info = subscribe(args)
        lease_s = info["lease_s"]
        renew_at = time.monotonic() + lease_s / 2
        print(f"[subscribed] {info['host']}:{info['port']} lease {lease_s}s "
              f"({'renewed' if info['renewed'] else 'new'}), boot {info['boot']}")

    try:
        do_subscribe()
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"subscribe failed: {e}")

    try:
        while True:
            now = time.monotonic()
            if now >= renew_at or now - last_rx > SILENCE_RESUBSCRIBE_S:
                try:
                    do_subscribe()
                    last_rx = now            # avoid re-subscribing every second while waiting
                except (urllib.error.URLError, OSError, ValueError) as e:
                    print(f"[resubscribe failed] {e}")
                    renew_at = now + 5
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            last_rx = time.monotonic()
            msg = json.loads(data.decode("utf-8"))
            if msg.get("v") != 1:
                print(f"[unsupported schema version {msg.get('v')}]")
                continue
            if msg["boot"] != boot:
                print(f"[server boot id {msg['boot']}]")
                boot = msg["boot"]
                last_seq = {}
            gap = ""
            if msg["reason"] != "subscribe" and "seq" in last_seq and msg["seq"] != last_seq["seq"] + 1:
                gap = f"  !! seq gap {last_seq['seq']} -> {msg['seq']}"
            last_seq["seq"] = msg["seq"]
            if args.json:
                print(json.dumps(msg, indent=2, ensure_ascii=False))
            else:
                print(f"#{msg['seq']:<5} {msg['reason']:<9} {'STALE ' if msg['stale'] else ''}"
                      f"age={msg['source_age_ms']}ms {summary(msg)}{gap}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            post(args.server, "/unsubscribe", {"port": args.port, **({"host": args.host} if args.host else {})})
            print("[unsubscribed]")
        except Exception:
            pass


if __name__ == "__main__":
    main()
