# Scoreboard UDP push, protocol v1

The scoreboard service pushes its game state as UDP datagrams to subscribed receivers.
The HTTP endpoint `GET /state` is unchanged. Receivers subscribe over HTTP, then listen on a UDP port.

Constants live at the top of `outputs/udp_publisher.py`. Values below are the defaults.

## 1. Subscribing (HTTP, same port as `/state`, default 8080)

All request bodies are JSON objects (`Content-Type` is not checked), at most 4096 bytes, and `Content-Length` is required. All responses are JSON.

### `POST /subscribe`
| field | type | required | meaning |
|---|---|---|---|
| `port` | integer | yes | UDP port the receiver listens on, 1024..65535 |
| `host` | string | no | IPv4 address to send to. Default: the IP the HTTP request came from. A host different from the requester's IP is accepted only inside `ALLOWED_SUBNETS` (empty by default), otherwise 403. Hostnames are not resolved (400). Multicast, `0.0.0.0`, `255.255.255.255` and IPv6 are rejected (400). |
| `lease_s` | integer >= 1 | no | Requested lease in seconds. Default 3600, maximum 7200. Larger values are clamped, and the response tells you the granted value. |

Response 200:
```json
{"v":1,"boot":"b6ec9594","host":"192.168.10.55","port":5005,"renewed":false,
 "lease_s":3600,"lease_expires_ts":1789934165874,"heartbeat_s":1.0}
```
- `renewed`: `true` if this host:port was already subscribed. The lease is then reset to `lease_s` from now, and no second entry is created and no snapshot is sent.
- `lease_expires_ts`: UTC milliseconds since the epoch.
- `boot`: the server's boot ID, identical to `boot` in the datagrams.

A **new** subscription immediately gets one full snapshot (`reason:"subscribe"`).

Errors: `{"error": "<text>"}` with status 400 (invalid field), 403 (host not allowed), 411 (no Content-Length), 413 (body too large), 429 (8 subscribers already), 503 (UDP push disabled on the server).

### `POST /unsubscribe`
Body: `port` (required), `host` (optional, same rules as above). Response: `{"removed": true|false}`. A datagram already in flight may still arrive right after.

### `GET /subscribers`
Read-only debugging view: `{"boot": "...", "subscribers": [{"host","port","lease_remaining_s","seq","sent","errors"}]}`.

### Lifecycle rules
- The subscriber list is **in memory only**. After a service restart it is empty, and receivers must subscribe again.
- Leases expire unless renewed. Renew by repeating `POST /subscribe` (recommended: at half the lease).
- Detect a lost subscription by **silence**. Heartbeats arrive about every second. If nothing arrives for about 5 s, POST `/subscribe` again (it is idempotent).
- At most 8 subscribers. A subscriber is dropped after 10 consecutive send errors.
- Nothing is sent to a receiver that has not subscribed. There is no multicast in v1.

## 2. Datagram

One UDP datagram = one complete JSON snapshot, UTF-8, at most 1200 bytes (typically about 500). No fragmentation and no partial updates.

```json
{"v":1,"boot":"b6ec9594","seq":1,"ts":1789930565874,"changed_ts":1789930565741,"reason":"subscribe","stale":false,"source_age_ms":132,"state":{"home":{"name":"GCZ","score":4,"timeouts":1,"penalties":["0020","    ","    "]},"away":{"name":"ZUG","score":4,"timeouts":0,"penalties":["0050","0112","    "]},"clock":"02:19","clock_running":true,"period_display":"1","period_number":1,"in_intermission":false,"horn":false,"sport":4,"summary":{"top":"RESULTAT 1. DRITTEL","bottom":"(4:4)","main":"4:4"}}}
```
This example was produced by the real code path with `main_simulator.py` (simulated scoreboard input). It is not a capture from real hardware.

### Header fields
| field | type | meaning |
|---|---|---|
| `v` | int | Schema version, currently `1`. Ignore datagrams with an unknown `v`. |
| `boot` | string | Random ID, new on every process start. A changed `boot` means the service restarted: reset your `seq` tracking and re-subscribe (your subscription is gone). |
| `seq` | int | **Per subscription.** 1 in the first (`subscribe`) datagram, then +1 for every datagram sent to you. A jump means datagrams were lost. Datagrams may also arrive out of order, so ignore any datagram whose `seq` is lower than one you already processed. Restarts at 1 with `reason:"subscribe"` for a new subscription. |
| `ts` | int | Send time, UTC milliseconds since the epoch (server clock). |
| `changed_ts` | int | UTC ms when `state` last differed from the previous snapshot (detected by the sender). At startup: the process start time. Also identical across heartbeats while nothing changes. |
| `reason` | string | `"change"`: the state changed. `"heartbeat"`: nothing changed, the last snapshot is repeated 1 s after the last send. `"subscribe"`: the initial snapshot for a new subscriber. |
| `stale` | bool | `true` if no serial frame has been parsed from the scoreboard for more than 10 s (`STALE_AFTER_S`), or none since service start. The `state` is then the last known state. Whether the real scoreboard keeps sending frames while idle or paused is **not verified**, so `stale` can also mean "board is connected but silent". |
| `source_age_ms` | int or null | Milliseconds since the last serial frame was parsed. `null` = none since service start. |
| `state` | object | See section 3. Identical to `GET /state`. Not interpreted or reformatted by the sender. |

### Timing
- A `change` datagram is sent for every change, at least 50 ms apart. Changes inside that window are merged and the latest state wins, so intermediate states can be skipped.
- With the clock running a `change` arrives about once per clock step.
- Heartbeat: 1 s after the last send. Missing heartbeats for more than about 3 s mean the service or the network path is down.
- Delivery is UDP: datagrams can be lost, duplicated or reordered. Every datagram is a complete snapshot, so keep the newest one.

## 3. `state` fields

Source: `state.py` (`GameState.as_dict`), filled by `parser.py` from four serial frame types. Initial values apply until a frame of that type has arrived.

| field | type | initial | set by | meaning |
|---|---|---|---|---|
| `home.name`, `away.name` | string | `""` | frame **N** | 12 ASCII characters each, whitespace stripped. |
| `home.score`, `away.score` | int | 0 | frame **D** | Parsed from 3 characters. Unparsable input becomes 0. |
| `home.timeouts`, `away.timeouts` | int | 0 | frame **D** | One digit each. Unparsable input becomes 0. |
| `home.penalties`, `away.penalties` | list of 3 strings | `["","",""]` | frame **C** | See "Penalty strings". |
| `clock` | string | `""` | frame **D** | Raw 5-character field, not stripped or interpreted. |
| `clock_running` | bool | false | frame **D** | `true` if the status character is `"1"` or `"3"`. |
| `period_display` | string | `""` | frame **D** | One character, stripped. `"4"` and `"E"` are mapped to `"O"` (overtime). Otherwise as sent by the scoreboard, typically `"1"`, `"2"`, `"3"`. |
| `horn` | bool | false | frame **D** | `true` if the horn character is `"1"` or `"3"`. |
| `period_number` | int | 0 | frame **T** | One digit. `0` = intermission. Keeps its previous value if unparsable. |
| `in_intermission` | bool | false | frame **T** | `period_number == 0`. Stays `false` until the first T frame, even though `period_number` is 0 then. |
| `sport` | int or null | null | frame **T** | One digit, meaning unknown (see below). `null` until the first T frame. |
| `summary.top`, `.bottom`, `.main` | string | see below | computed | Not sent by the scoreboard. |

**Clock format.** The code copies exactly 5 characters and does not parse them. The simulator sends `"MM:SS"` and counts **up**. The code never handles tenths of a second: if the real scoreboard shows tenths in some situations, they would arrive as part of this raw 5-character string. **Not verified against real hardware.** Count direction on the real board is also unverified. Receivers should not assume anything beyond "5-character string, possibly blank or padded".

**Penalty strings.** Each of the 3 slots per team is a raw 4-character string (not stripped), taken from frame C (6 slots of 4 characters: 3 home, then 3 away). The simulator sends `"MMSS"` digits without a colon, e.g. `"0142"` = 1:42 remaining, counting down, and 4 spaces for an empty slot. The existing vMix code treats a slot as **inactive** if it is blank or `"0000"`, `"00:00"` or `"0:00"`, and as active otherwise, so a `"MMSS"` (or `"MM:SS"`) reading with those exceptions matches what the code already assumes. The real board's exact encoding is **not verified**. The simulator only fills 2 slots per team, and the vMix overlay only shows 2.

**`sport`.** Read as one digit from frame T. There is no mapping table anywhere in the repository. The simulator sends `4` (commented as "hockey"). Treat it as an opaque number.

**`summary`.** Computed by `summary.py` from the scores and `period_display`, using German text. `main` is the score `"H:A"` (plus `" (n.V.)"` after a decided overtime), `bottom` lists the per-period scores such as `"(4:4)"` or `"(2:1, 1:0)"`, and `top` is the caption, e.g. `"RESULTAT 1. DRITTEL"` or `"SCHLUSSRESULTAT"`. Initial: `top:"RESULTAT 1. DRITTEL"`, `bottom:"(0:0)"`, `main:"0:0"` (after the first computation). Derived per-period scores assume periods are played in order.

### Frames (what sets what)
| frame | sets |
|---|---|
| **N** (24 bytes) | `home.name`, `away.name` |
| **T** | `sport`, `period_number`, `in_intermission` |
| **D** | `clock`, `home.score`, `away.score`, `home.timeouts`, `away.timeouts`, `period_display`, `clock_running`, `horn` |
| **C** (24 bytes) | `home.penalties`, `away.penalties` |

Frames failing the length check or CRC are ignored. There are no goal or penalty *events*: derive them by comparing snapshots (e.g. score increased).

## 4. Trying it

```
python3 udp_receiver_example.py --server http://SCOREBOARD_IP:8080 --port 5005
```
The script subscribes, renews at half the lease, re-subscribes after 5 s of silence, prints each datagram (`--json` for the full JSON), flags `seq` gaps and unsubscribes on Ctrl+C. It uses the Python standard library only. The receiver machine must allow inbound UDP on the chosen port.
