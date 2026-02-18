from state import GameState

def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default

def parse_names(payload: bytes, state: GameState):
    data = payload[1:]
    if len(data) < 24:
        return
    state.home.name = data[0:12].decode("ascii", errors="ignore").strip()
    state.away.name = data[12:24].decode("ascii", errors="ignore").strip()

def parse_base(payload: bytes, state: GameState):
    msg = payload[1:].decode("ascii", errors="ignore")
    if len(msg) < 20:
        return

    state.clock = msg[0:5]
    state.home.score = _safe_int(msg[5:8].strip(), 0)
    state.away.score = _safe_int(msg[8:11].strip(), 0)

    state.home.timeouts = _safe_int(msg[13:14] or "0", 0)
    state.away.timeouts = _safe_int(msg[14:15] or "0", 0)

    per = msg[15:16].strip()
    if per in ("4","E"):
        per = "O"
    state.period_display = per

    state.clock_running = msg[17:18] in ("1","3")
    state.horn = msg[18:19] in ("1","3")  # SIR at position 21 -> index 18

def parse_time(payload: bytes, state: GameState):
    msg = payload[1:].decode("ascii", errors="ignore")
    if len(msg) < 20:
        return
    state.sport = _safe_int(msg[18:19], state.sport or 0)
    state.period_number = _safe_int(msg[19:20], state.period_number)
    state.in_intermission = (state.period_number == 0)

def parse_expulsion(payload: bytes, state: GameState):
    data = payload[1:]
    if len(data) < 24:
        return
    timers = [data[i:i+4].decode("ascii", errors="ignore") for i in range(0, 24, 4)]
    state.home.penalties = timers[:3]
    state.away.penalties = timers[3:]
