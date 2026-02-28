#!/usr/bin/env python3
import csv
import re
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import requests
import obsws_python as obs


# -------------------------
# CONFIG
# -------------------------
TIMEZONE = "Europe/Zurich"

# Companion HTTP API
COMPANION_IP = "192.168.10.111"
COMPANION_PORT = 8000
COMPANION_VAR_STREAM = "Stream_Auto"
COMPANION_VAR_RECORD = "Record_Auto"

# Update this variable with today's leagues list
COMPANION_VAR_GAME_LIST_TODAY = "Game_List_Today"

# CSV
STREAM_PLANNING_FILE = "/mnt/One_Drive/01_GC_Streaming_Sharing/Stream_Planning.csv"
CSV_SEPARATOR = ";"  # can be "," or ";"

# Timing (minutes)
LEAD_MINUTES = 20
DURATION_MINUTES = 200

# Reload CSV only when OBS is idle (unless OBS is down/unreachable)
CSV_RELOAD_IDLE_EVERY_SECONDS = 5 * 60

# Loop / logging
MAIN_LOOP_SECONDS = 10
UPCOMING_LIST_COUNT = 5

# Check game status every 5 minutes to early-stop if FINISHED
GAME_STATUS_CHECK_EVERY_SECONDS = 5 * 60
UNIHOCKEY_GAME_API_BASE = "https://www.unihockey.live/api/game"
UNIHOCKEY_HTTP_TIMEOUT = 10

# OBS (websocket v5)
OBS_HOST = "192.168.10.208"  # Change to Unattended System on big System
OBS_PORT = 4455
OBS_PASSWORD = ""
OBS_SCENE = "Game"

# If OBS is not reachable: log + retry after 10 minutes
OBS_RETRY_SECONDS = 10 * 60

# Wait after setting RTMP before starting stream
WAIT_AFTER_SET_RTMP_SECONDS = 2.0

# HTTP behavior (Companion)
HTTP_CONNECT_TIMEOUT = 0.7
HTTP_READ_TIMEOUT = 0.7

# Start verification (more debug, verify started)
START_VERIFY_TIMEOUT_SECONDS = 8.0
START_VERIFY_INTERVAL_SECONDS = 0.5

# Log game status from the web every time we poll (even if unchanged)
LOG_GAME_STATUS_EVERY_POLL = True


# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("AutoStream")


# -------------------------
# DATA MODEL
# -------------------------
@dataclass(frozen=True)
class GamePlan:
    liga: str
    short_title: str
    game_id: str
    game_start: datetime
    start_time: datetime
    stop_time: datetime
    stream_server: Optional[str]
    stream_key: Optional[str]


STREAM_URL_RE = re.compile(r"Stream\s*URL:\s*(\S+)", re.IGNORECASE)
STREAM_KEY_RE = re.compile(r"Stream\s*key:\s*(\S+)", re.IGNORECASE)


# -------------------------
# HELPERS
# -------------------------
def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def fmt_td(seconds: float) -> str:
    s = int(seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{sign}{h}h {m:02d}m {sec:02d}s"
    return f"{sign}{m}m {sec:02d}s"


def onoff(v: bool | int) -> str:
    return "ON" if bool(v) else "OFF"


def auto_mode(stream_auto: int, record_auto: int) -> str:
    if stream_auto and record_auto:
        return "STREAM+RECORD"
    if stream_auto:
        return "STREAM"
    if record_auto:
        return "RECORD"
    return "NONE"


def mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 6:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


# -------------------------
# COMPANION
# -------------------------
_session = requests.Session()
_last_companion_ok: Optional[bool] = None
_last_companion_err: str = ""


def companion_get_custom_var(name: str) -> Optional[str]:
    url = f"http://{COMPANION_IP}:{COMPANION_PORT}/api/custom-variable/{name}/value"
    try:
        r = _session.get(url, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url} body={r.text!r}")
        return r.text.strip()
    except Exception as e:
        global _last_companion_err
        _last_companion_err = str(e)
        return None


def companion_set_custom_var(name: str, value: str) -> bool:
    """
    Set a Companion custom variable using the documented query-param method:
      POST /api/custom-variable/<name>/value?value=<value>

    Works with empty strings too (sends ...?value=).
    """
    url = f"http://{COMPANION_IP}:{COMPANION_PORT}/api/custom-variable/{name}/value"

    # Required debug log line: how we update the variable
    log.info(
        "COMPANION SET: $(custom:%s) <= %d chars (%d lines) via POST query-param",
        name,
        len(value),
        0 if not value else (value.count("\n") + 1),
    )
    log.info("COMPANION SET URL: %s", url)
    log.info("COMPANION SET VALUE (repr): %r", value)

    try:
        # params handles URL encoding and keeps empty value as "value="
        r = _session.post(
            url,
            params={"value": value},
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
        )
        log.info("COMPANION SET HTTP: %s body=%r", r.status_code, r.text)

        ok = r.status_code in (200, 204)
        if ok:
            rb = companion_get_custom_var(name)
            log.info("COMPANION READBACK: $(custom:%s) => %r", name, rb)
        else:
            log.warning("COMPANION SET FAILED: status=%s body=%r", r.status_code, r.text)

        return ok

    except Exception as e:
        global _last_companion_err
        _last_companion_err = str(e)
        log.error("COMPANION SET EXCEPTION: %s", e)
        return False


def companion_get_flags_and_health() -> Tuple[int, int, bool, str]:
    s = companion_get_custom_var(COMPANION_VAR_STREAM)
    r = companion_get_custom_var(COMPANION_VAR_RECORD)

    def to01(x: Optional[str]) -> int:
        if x is None:
            return 0
        x = x.strip().strip('"')
        return 1 if x in ("1", "true", "True", "TRUE", "on", "ON") else 0

    ok = (s is not None) and (r is not None)
    err = "" if ok else _last_companion_err
    return to01(s), to01(r), ok, err


# -------------------------
# GAME LIST (TODAY)
# -------------------------
def build_game_list_today_value(plans: List[GamePlan], today_local: date) -> str:
    """
    newline-separated league names ("liga") for all games whose game_start is today (local TZ).
    Duplicates are kept (one line per game).
    """
    todays = [p.liga for p in plans if p.game_start.date() == today_local]
    return "\n".join(todays)


# -------------------------
# CSV
# -------------------------
def parse_stream_details(details: str) -> Tuple[Optional[str], Optional[str]]:
    d = (details or "").strip()
    if not d:
        return None, None
    m_url = STREAM_URL_RE.search(d)
    m_key = STREAM_KEY_RE.search(d)
    server = m_url.group(1).strip() if m_url else None
    key = m_key.group(1).strip() if m_key else None
    return server, key


def load_game_plans(path: str, sep: str, tz: ZoneInfo) -> List[GamePlan]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    plans: List[GamePlan] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=sep)
        for row in reader:
            if not row or len(row) < 6:
                continue
            if row[0].strip().lower() in ("liga", "league"):
                continue

            liga = row[0].strip()
            short_title = row[1].strip()
            game_id = row[2].strip()
            date_s = row[3].strip()
            time_s = row[4].strip()
            details = row[5].strip()

            dt_naive = datetime.strptime(f"{date_s} {time_s}", "%d.%m.%Y %H:%M:%S")
            game_start = dt_naive.replace(tzinfo=tz)

            start_time = game_start - timedelta(minutes=LEAD_MINUTES)
            stop_time = start_time + timedelta(minutes=DURATION_MINUTES)

            server, key = parse_stream_details(details)

            plans.append(
                GamePlan(
                    liga=liga,
                    short_title=short_title,
                    game_id=game_id,
                    game_start=game_start,
                    start_time=start_time,
                    stop_time=stop_time,
                    stream_server=server,
                    stream_key=key,
                )
            )

    plans.sort(key=lambda x: x.start_time)
    return plans


# -------------------------
# OBS
# -------------------------
class OBSController:
    def __init__(self):
        self.client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)

    def get_status(self) -> Tuple[bool, bool]:
        stream = self.client.get_stream_status()
        record = self.client.get_record_status()
        return bool(stream.output_active), bool(record.output_active)

    def set_scene(self, scene_name: str) -> None:
        self.client.set_current_program_scene(scene_name)

    def set_rtmp_destination(self, server: str, key: str) -> None:
        self.client.set_stream_service_settings("rtmp_custom", {"server": server, "key": key})

    def start_stream(self) -> None:
        self.client.start_stream()

    def stop_stream(self) -> None:
        self.client.stop_stream()

    def start_record(self) -> None:
        self.client.start_record()

    def stop_record(self) -> None:
        self.client.stop_record()


def try_connect_obs() -> Optional[OBSController]:
    """
    Try connecting to OBS once. Return OBSController if OK, else None.
    Non-blocking (no long sleep here).
    """
    try:
        log.info("Connecting to OBS %s:%s ...", OBS_HOST, OBS_PORT)
        obsctl = OBSController()
        obsctl.get_status()  # test call
        log.info("OBS connection established")
        log.info("")
        return obsctl
    except Exception as e:
        log.warning("OBS not reachable (%s). Will retry later.", e)
        log.info("")
        return None


def wait_for_obs_output(
    obsctl: OBSController,
    want_streaming: Optional[bool] = None,
    want_recording: Optional[bool] = None,
    timeout_s: float = START_VERIFY_TIMEOUT_SECONDS,
    interval_s: float = START_VERIFY_INTERVAL_SECONDS,
) -> Tuple[bool, bool]:
    deadline = time.time() + timeout_s
    last_streaming = False
    last_recording = False
    while True:
        last_streaming, last_recording = obsctl.get_status()

        ok = True
        if want_streaming is not None:
            ok = ok and (last_streaming == want_streaming)
        if want_recording is not None:
            ok = ok and (last_recording == want_recording)

        if ok:
            return last_streaming, last_recording

        if time.time() >= deadline:
            return last_streaming, last_recording

        time.sleep(interval_s)


# -------------------------
# UNIHOCKEY GAME STATUS
# -------------------------
_api_session = requests.Session()
_last_api_ok: Optional[bool] = None
_last_api_err: str = ""


def fetch_game_status(game_id: str) -> Optional[str]:
    url = f"{UNIHOCKEY_GAME_API_BASE}/{game_id}"
    try:
        r = _api_session.get(url, headers={"accept": "application/json"}, timeout=UNIHOCKEY_HTTP_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url} body={r.text!r}")
        data = r.json()
        return (data.get("status") or "").strip() or None
    except Exception as e:
        global _last_api_err
        _last_api_err = str(e)
        return None


# -------------------------
# SCHEDULE
# -------------------------
def find_active_plan(plans: List[GamePlan], now: datetime) -> Optional[GamePlan]:
    for p in plans:
        if p.start_time <= now < p.stop_time:
            return p
    return None


def upcoming_plans(plans: List[GamePlan], now: datetime, n: int) -> List[GamePlan]:
    future = [p for p in plans if p.start_time > now]
    return future[:n]


def log_upcoming(plans: List[GamePlan], now: datetime, n: int) -> None:
    up = upcoming_plans(plans, now, n)
    if not up:
        log.info("UPCOMING: none")
        return
    for i, p in enumerate(up, start=1):
        secs_to = (p.start_time - now).total_seconds()
        has_url = "yes" if p.stream_server else "no"
        has_key = "yes" if p.stream_key else "no"
        log.info(
            "UPCOMING #%d: %s %s (Game_ID=%s) start=%s (in %s) stop=%s url=%s key=%s",
            i,
            p.liga,
            p.short_title,
            p.game_id,
            fmt_dt(p.start_time),
            fmt_td(secs_to),
            fmt_dt(p.stop_time),
            has_url,
            has_key,
        )


# -------------------------
# MAIN
# -------------------------
def main():
    tz = ZoneInfo(TIMEZONE)

    log.info(
        "Starting. Companion=%s:%s OBS=%s:%s Scene=%r",
        COMPANION_IP,
        COMPANION_PORT,
        OBS_HOST,
        OBS_PORT,
        OBS_SCENE,
    )
    log.info("NOTE: Script runs even if OBS is down; Companion will still be updated.")
    log.info("NOTE: CSV reload is allowed when OBS is down; when OBS is up it reloads only when OBS is idle.")
    log.info("")

    obsctl: Optional[OBSController] = None
    last_obs_connect_try = 0.0  # epoch seconds

    plans: List[GamePlan] = []
    last_csv_reload = 0.0
    last_csv_mtime = 0.0

    last_stream_auto: Optional[int] = None
    last_record_auto: Optional[int] = None

    stream_start_attempts: Dict[str, int] = {}
    record_start_attempts: Dict[str, int] = {}
    MAX_START_ATTEMPTS = 1

    controlled_game_id: Optional[str] = None

    stop_enforced_for_game_id: Optional[str] = None
    early_stop_done_for_game_id: Optional[str] = None

    last_active_game_id: Optional[str] = None

    last_game_status_check = 0.0
    game_status_cache: Dict[str, Optional[str]] = {}

    last_is_streaming: Optional[bool] = None
    last_is_recording: Optional[bool] = None

    # Game list tracking (must still send empty on first run)
    last_sent_game_list_today: Optional[str] = None
    last_seen_local_date: date = datetime.now(tz=tz).date()

    global _last_companion_ok, _last_companion_err
    global _last_api_ok, _last_api_err

    while True:
        now = datetime.now(tz=tz)

        # Day rollover: force resend
        if now.date() != last_seen_local_date:
            last_seen_local_date = now.date()
            last_sent_game_list_today = None

        # -------------------------
        # OBS connect/reconnect (NON-BLOCKING)
        # -------------------------
        if obsctl is None:
            if (time.time() - last_obs_connect_try) >= OBS_RETRY_SECONDS or last_obs_connect_try == 0.0:
                last_obs_connect_try = time.time()
                obsctl = try_connect_obs()
                if obsctl is None:
                    # Treat OBS as down; keep running Companion/CSV logic
                    is_streaming, is_recording = False, False
                else:
                    # On fresh connect, force output log
                    last_is_streaming = None
                    last_is_recording = None
                    is_streaming, is_recording = obsctl.get_status()
            else:
                is_streaming, is_recording = False, False
        else:
            try:
                is_streaming, is_recording = obsctl.get_status()
            except Exception as e:
                log.error("OBS connection lost (%s). Will retry in %d minutes.", e, OBS_RETRY_SECONDS // 60)
                log.info("")
                obsctl = None
                last_is_streaming = None
                last_is_recording = None
                is_streaming, is_recording = False, False

        # OBS output state log (only meaningful when connected; harmless otherwise)
        if last_is_streaming is None or last_is_recording is None:
            last_is_streaming, last_is_recording = is_streaming, is_recording
            log.info("OBS outputs: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))
            log.info("")
        elif is_streaming != last_is_streaming or is_recording != last_is_recording:
            last_is_streaming, last_is_recording = is_streaming, is_recording
            log.info("OBS outputs changed: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))
            log.info("")

        obs_idle = (not is_streaming) and (not is_recording)

        # -------------------------
        # CSV reload
        # - Always allow reload if OBS is NOT connected (so Companion stays fresh)
        # - If OBS is connected, keep the "only when idle" behavior
        # -------------------------
        try:
            p = Path(STREAM_PLANNING_FILE)
            mtime = p.stat().st_mtime if p.exists() else 0.0
        except Exception:
            mtime = 0.0

        csv_reloaded_this_loop = False
        allow_csv_reload = (obsctl is None) or obs_idle

        if allow_csv_reload:
            due_time = (time.time() - last_csv_reload) >= CSV_RELOAD_IDLE_EVERY_SECONDS
            due_mtime = (mtime != 0.0 and mtime != last_csv_mtime)
            if due_time or due_mtime or not plans:
                try:
                    plans = load_game_plans(STREAM_PLANNING_FILE, CSV_SEPARATOR, tz)
                    last_csv_reload = time.time()
                    last_csv_mtime = mtime
                    csv_reloaded_this_loop = True
                    reason = "mtime change" if due_mtime else "timer/initial"
                    log.info("CSV loaded: %d rows (%s)", len(plans), reason)
                    log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                    log.info("")
                except Exception as e:
                    log.error("CSV load failed: %s", e)
                    log.info("")

        # -------------------------
        # Update Game_List_Today after CSV load (or day-change forced resend)
        # -------------------------
        if plans and (csv_reloaded_this_loop or last_sent_game_list_today is None):
            today_value = build_game_list_today_value(plans, now.date())

            log.info("Preparing $(custom:%s) for local date %s", COMPANION_VAR_GAME_LIST_TODAY, now.date().isoformat())
            log.info("Generated leagues list (%d chars):", len(today_value))
            log.info("---- BEGIN Game_List_Today ----")
            log.info("%s", today_value if today_value else "(empty)")
            log.info("---- END   Game_List_Today ----")

            # IMPORTANT: send even if empty on first run (None) or if changed
            should_send = (last_sent_game_list_today is None) or (today_value != last_sent_game_list_today)

            if should_send:
                ok = companion_set_custom_var(COMPANION_VAR_GAME_LIST_TODAY, today_value)
                if ok:
                    last_sent_game_list_today = today_value
                log.info("")
            else:
                log.info("$(custom:%s) unchanged -> no update sent", COMPANION_VAR_GAME_LIST_TODAY)
                log.info("")

        # -------------------------
        # Companion flags
        # -------------------------
        stream_auto, record_auto, companion_ok, companion_err = companion_get_flags_and_health()

        if _last_companion_ok is None or companion_ok != _last_companion_ok or (
            not companion_ok and companion_err != _last_companion_err
        ):
            if companion_ok:
                log.info("Companion connection: OK")
            else:
                log.warning("Companion connection: NOT OK (%s)", companion_err)
            log.info("")
            _last_companion_ok = companion_ok
            _last_companion_err = companion_err

        if last_stream_auto is None or last_record_auto is None:
            last_stream_auto, last_record_auto = stream_auto, record_auto
            log.info(
                "AUTO MODE: %s (Stream_Auto=%s Record_Auto=%s)",
                auto_mode(stream_auto, record_auto),
                onoff(stream_auto),
                onoff(record_auto),
            )
            log.info("")
        elif stream_auto != last_stream_auto or record_auto != last_record_auto:
            last_stream_auto, last_record_auto = stream_auto, record_auto
            log.info(
                "AUTO MODE CHANGED: %s (Stream_Auto=%s Record_Auto=%s)",
                auto_mode(stream_auto, record_auto),
                onoff(stream_auto),
                onoff(record_auto),
            )
            log.info("")

        # -------------------------
        # Active plan
        # -------------------------
        active = find_active_plan(plans, now) if plans else None

        current_active_game_id = active.game_id if active else None
        if current_active_game_id != last_active_game_id:
            last_active_game_id = current_active_game_id

            if active:
                log.info(
                    "ACTIVE WINDOW: %s %s (Game_ID=%s) %s -> %s",
                    active.liga,
                    active.short_title,
                    active.game_id,
                    fmt_dt(active.start_time),
                    fmt_dt(active.stop_time),
                )
                stop_enforced_for_game_id = None
                early_stop_done_for_game_id = None
                game_status_cache.pop(active.game_id, None)
                log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                log.info("")
            else:
                log.info("ACTIVE WINDOW: none")
                log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                log.info("")

        # -------------------------
        # Scheduled stop (only if OBS connected)
        # -------------------------
        if (
            active
            and controlled_game_id == active.game_id
            and now >= active.stop_time
            and stop_enforced_for_game_id != active.game_id
        ):
            if obsctl is not None and (is_streaming or is_recording):
                log.info("STOP TIME reached for Game_ID=%s -> stopping stream+record", active.game_id)
                try:
                    if is_streaming:
                        obsctl.stop_stream()
                    if is_recording:
                        obsctl.stop_record()
                    is_streaming, is_recording = wait_for_obs_output(
                        obsctl,
                        want_streaming=False if is_streaming else None,
                        want_recording=False if is_recording else None,
                    )
                    log.info(
                        "OBS outputs stopped (verified): streaming=%s recording=%s",
                        onoff(is_streaming),
                        onoff(is_recording),
                    )
                except Exception as e:
                    log.error("Failed to stop OBS outputs: %s", e)
                log.info("")
            stop_enforced_for_game_id = active.game_id

        # -------------------------
        # Early stop FINISHED (only if OBS connected)
        # -------------------------
        if (
            active
            and controlled_game_id == active.game_id
            and (is_streaming or is_recording)
            and now < active.stop_time
        ):
            if (time.time() - last_game_status_check) >= GAME_STATUS_CHECK_EVERY_SECONDS:
                last_game_status_check = time.time()

                status = fetch_game_status(active.game_id)

                api_ok = status is not None
                api_err = "" if api_ok else _last_api_err
                if _last_api_ok is None or api_ok != _last_api_ok or (not api_ok and api_err != _last_api_err):
                    if api_ok:
                        log.info("Unihockey API connection: OK")
                    else:
                        log.warning("Unihockey API connection: NOT OK (%s)", api_err)
                    log.info("")
                    _last_api_ok = api_ok
                    _last_api_err = api_err

                prev = game_status_cache.get(active.game_id)
                game_status_cache[active.game_id] = status

                if status:
                    if LOG_GAME_STATUS_EVERY_POLL:
                        if status == prev:
                            log.info("Game status polled (unchanged): Game_ID=%s status=%s", active.game_id, status)
                            log.info("")
                        else:
                            log.info(
                                "Game status polled (changed):   Game_ID=%s %s -> %s",
                                active.game_id,
                                prev,
                                status,
                            )
                            log.info("")
                    else:
                        if status != prev:
                            log.info("Game status update: Game_ID=%s status=%s", active.game_id, status)
                            log.info("")

                if status == "FINISHED" and early_stop_done_for_game_id != active.game_id:
                    log.info(
                        "EARLY STOP: Game_ID=%s is FINISHED before scheduled stop (%s). Stopping stream+record.",
                        active.game_id,
                        fmt_dt(active.stop_time),
                    )
                    if obsctl is not None:
                        try:
                            if is_streaming:
                                obsctl.stop_stream()
                            if is_recording:
                                obsctl.stop_record()
                            is_streaming, is_recording = wait_for_obs_output(
                                obsctl,
                                want_streaming=False if is_streaming else None,
                                want_recording=False if is_recording else None,
                            )
                            log.info(
                                "OBS outputs stopped (early stop, verified): streaming=%s recording=%s",
                                onoff(is_streaming),
                                onoff(is_recording),
                            )
                        except Exception as e:
                            log.error("Failed early stop of OBS outputs: %s", e)
                        log.info("")
                    early_stop_done_for_game_id = active.game_id

        # -------------------------
        # Start logic (only if OBS connected)
        # -------------------------
        if active and obsctl is not None:
            try:
                obsctl.set_scene(OBS_SCENE)
            except Exception as e:
                log.error("Failed to set scene %r: %s", OBS_SCENE, e)

            # STREAM
            if (
                stream_auto == 1
                and (stream_start_attempts.get(active.game_id, 0) < MAX_START_ATTEMPTS)
                and not is_streaming
            ):
                stream_start_attempts[active.game_id] = stream_start_attempts.get(active.game_id, 0) + 1

                if not (active.stream_server and active.stream_key):
                    log.warning(
                        "Stream_Auto=ON but missing Stream URL and/or Stream key for Game_ID=%s",
                        active.game_id,
                    )
                    log.info("")
                else:
                    try:
                        log.info(
                            "AUTO START: STREAM (attempt %d/%d) Game_ID=%s",
                            stream_start_attempts[active.game_id],
                            MAX_START_ATTEMPTS,
                            active.game_id,
                        )
                        log.info("  Pre-check: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))
                        log.info("  Setting scene=%r", OBS_SCENE)
                        log.info("  Setting RTMP: server=%s key=%s", active.stream_server, mask_key(active.stream_key))

                        obsctl.set_rtmp_destination(active.stream_server, active.stream_key)
                        log.info("  Waiting %.1fs after setting RTMP...", WAIT_AFTER_SET_RTMP_SECONDS)
                        time.sleep(WAIT_AFTER_SET_RTMP_SECONDS)

                        log.info("  Calling start_stream() ...")
                        obsctl.start_stream()

                        is_streaming2, is_recording2 = wait_for_obs_output(
                            obsctl, want_streaming=True, want_recording=None
                        )
                        if is_streaming2:
                            log.info(
                                "  Streaming started (verified). streaming=%s recording=%s",
                                onoff(is_streaming2),
                                onoff(is_recording2),
                            )
                            controlled_game_id = active.game_id
                        else:
                            log.error(
                                "  Streaming did NOT become active within %.1fs. Last seen: streaming=%s recording=%s",
                                START_VERIFY_TIMEOUT_SECONDS,
                                onoff(is_streaming2),
                                onoff(is_recording2),
                            )
                        log.info("")
                    except Exception as e:
                        log.error("Failed to start streaming: %s", e)
                        log.info("")

            # RECORD
            if (
                record_auto == 1
                and (record_start_attempts.get(active.game_id, 0) < MAX_START_ATTEMPTS)
                and not is_recording
            ):
                record_start_attempts[active.game_id] = record_start_attempts.get(active.game_id, 0) + 1

                try:
                    log.info(
                        "AUTO START: RECORD (attempt %d/%d) Game_ID=%s",
                        record_start_attempts[active.game_id],
                        MAX_START_ATTEMPTS,
                        active.game_id,
                    )
                    log.info("  Pre-check: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))
                    log.info("  Setting scene=%r", OBS_SCENE)
                    log.info("  Calling start_record() ...")
                    obsctl.start_record()

                    is_streaming2, is_recording2 = wait_for_obs_output(
                        obsctl, want_streaming=None, want_recording=True
                    )
                    if is_recording2:
                        log.info(
                            "  Recording started (verified). streaming=%s recording=%s",
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                        controlled_game_id = active.game_id
                    else:
                        log.error(
                            "  Recording did NOT become active within %.1fs. Last seen: streaming=%s recording=%s",
                            START_VERIFY_TIMEOUT_SECONDS,
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                    log.info("")
                except Exception as e:
                    log.error("Failed to start recording: %s", e)
                    log.info("")

        # Release control if no active window
        if not active and controlled_game_id is not None:
            log.info("CONTROLLED GAME cleared (no active window). Was Game_ID=%s", controlled_game_id)
            log.info("")
            controlled_game_id = None

        time.sleep(MAIN_LOOP_SECONDS)


if __name__ == "__main__":
    main()