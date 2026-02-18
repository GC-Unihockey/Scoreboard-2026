import time
import random
from saturn import make_frame

# Simulator parameters
SIM_SPEED = 5.0            # 5x faster than real time (game seconds per real second)
PERIOD_SECONDS = 20 * 60   # 20:00 hockey period length (game seconds)
BREAK_SECONDS = 5 * 60    # 5:00 break (game seconds)
TICK_REAL = 1.0 / SIM_SPEED  # real seconds per game second

HOME_NAME = "GCZ"
AWAY_NAME = "ZUG"

# scoring
MAX_GOALS_PER_TEAM_PER_PERIOD = 15
GOAL_CHANCE_PER_GAME_SECOND = 1.0 / 45.0  # on avg ~1 goal per 45s per team (capped)

# penalties
PENALTY_CHANCE_PER_GAME_SECOND = 1.0 / 60.0  # on avg ~1 penalty per minute per team (capped by slots)
PENALTY_LENGTHS = [120, 120]  # 2:00 or 5:00 (game seconds)

def _build_T(sport: int, pnu: int) -> bytes:
    # parse_time reads msg[18]=SPORT, msg[19]=PNU after stripping 'T'
    blob = list(" " * 26)
    blob[18] = str(sport)[0]
    blob[19] = str(pnu)[0]
    return b"T" + "".join(blob).encode("ascii")

def _build_D(clock: str, hs: int, as_: int, toh: int, tov: int, per: str, running: bool, horn: bool) -> bytes:
    base = list(" " * 60)
    base[0:5] = list(clock[:5].ljust(5))
    base[5:8] = list(f"{hs:3d}"[-3:])
    base[8:11] = list(f"{as_:3d}"[-3:])
    base[13] = str(toh)[0]
    base[14] = str(tov)[0]
    base[15] = per[0]
    base[17] = "1" if running else "0"
    base[18] = "1" if horn else "0"  # SIR (horn) index for our parser mapping
    return b"D" + "".join(base).encode("ascii")

def _build_C(home_timers, away_timers) -> bytes:
    # Protocol C payload expects 6 timers x 4 bytes = 24 bytes (3 home + 3 away).
    # We only simulate 2 per team; third is blank.
    def fmt_mmss(sec):
        if sec <= 0:
            return "    "
        mm = sec // 60
        ss = sec % 60
        return f"{mm:02d}{ss:02d}"
    h = [(fmt_mmss(t) if t else "    ") for t in (home_timers + [0])[:3]]
    a = [(fmt_mmss(t) if t else "    ") for t in (away_timers + [0])[:3]]
    return b"C" + ("".join(h + a)).encode("ascii")

def _build_N(home: str, away: str) -> bytes:
    return b"N" + home.ljust(12).encode("ascii") + away.ljust(12).encode("ascii")

def _decrement_list(timers, dt=1):
    for i in range(len(timers)):
        if timers[i] > 0:
            timers[i] = max(0, timers[i] - dt)

def _maybe_add_penalty(timers):
    # timers list has 2 slots
    for i in range(len(timers)):
        if timers[i] == 0:
            timers[i] = random.choice(PENALTY_LENGTHS)
            return True
    return False

def generate_stream():
    """
    Yield bytes chunks like a serial stream.
    Behaviour:
      - Period clock counts UP from 00:00 to 20:00 (your request)
      - Intermission (PNU=0) lasts 15:00
      - Runs 5x real speed (sleep 0.2s per game second)
      - Random goals up to 6 per team per period
      - Random penalties, 2 slots per team, timers count DOWN
    """
    sport = 4  # hockey
    toh, tov = 1, 0

    # Send names once
    wire = make_frame(_build_N(HOME_NAME, AWAY_NAME))
    yield wire[:10]
    yield wire[10:]

    total_home = 0
    total_away = 0

    for period in (1, 2, 3):
        # per-period counters (cap goals per team per period)
        home_p = 0
        away_p = 0

        # two penalty slots per team, store remaining seconds
        home_pens = [0, 0]
        away_pens = [0, 0]

        # Period running
        game_sec = 0
        while game_sec <= PERIOD_SECONDS:
            # At the end (20:00), pulse horn and stop.
            horn = (game_sec == PERIOD_SECONDS)
            running = (game_sec < PERIOD_SECONDS)

            # scoring while running
            if running:
                if home_p < MAX_GOALS_PER_TEAM_PER_PERIOD and random.random() < GOAL_CHANCE_PER_GAME_SECOND:
                    home_p += 1
                    total_home += 1
                if away_p < MAX_GOALS_PER_TEAM_PER_PERIOD and random.random() < GOAL_CHANCE_PER_GAME_SECOND:
                    away_p += 1
                    total_away += 1

                # penalties while running
                if random.random() < PENALTY_CHANCE_PER_GAME_SECOND:
                    _maybe_add_penalty(home_pens)
                if random.random() < PENALTY_CHANCE_PER_GAME_SECOND:
                    _maybe_add_penalty(away_pens)

                _decrement_list(home_pens, 1)
                _decrement_list(away_pens, 1)

            mm = game_sec // 60
            ss = game_sec % 60
            clock = f"{mm:02d}:{ss:02d}"  # counts UP

            payloads = [
                _build_T(sport, period),  # PNU=period
                _build_D(clock, total_home, total_away, toh, tov, str(period), running, horn),
                _build_C(home_pens, away_pens),
            ]
            wire = b"".join(make_frame(p) for p in payloads)

            # Chunk it like serial might
            yield wire[:13]
            yield wire[13:57]
            yield wire[57:]

            if game_sec == PERIOD_SECONDS:
                # hold horn frame briefly
                time.sleep(2 * TICK_REAL)
            else:
                time.sleep(TICK_REAL)

            game_sec += 1

        # Intermission / break
        break_sec = 0
        while break_sec < BREAK_SECONDS:
            # show 00:00 stopped during break (you can change if your board shows break time)
            payloads = [
                _build_T(sport, 0),  # PNU=0 intermission
                _build_D("00:00", total_home, total_away, toh, tov, str(period), False, False),
                _build_C([0, 0], [0, 0]),
            ]
            wire = b"".join(make_frame(p) for p in payloads)
            yield wire[:13]
            yield wire[13:57]
            yield wire[57:]
            time.sleep(TICK_REAL)
            break_sec += 1
