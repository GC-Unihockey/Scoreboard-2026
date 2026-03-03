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
LEAD_MINUTES = 25 #25
DURATION_MINUTES = 180 #150

# Reload CSV only when OBS is idle (unless OBS is down/unreachable)
CSV_RELOAD_IDLE_EVERY_SECONDS = 5 * 60

# Loop / logging
MAIN_LOOP_SECONDS = 30
UPCOMING_LIST_COUNT = 5

# Check game status every 5 minutes to early-stop if FINISHED
GAME_STATUS_CHECK_EVERY_SECONDS = 5 * 60
UNIHOCKEY_GAME_API_BASE = "https://www.unihockey.live/api/game"
UNIHOCKEY_HTTP_TIMEOUT = 10

# OBS (websocket v5)
OBS_HOST = "192.168.10.208"
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

# Start verification
START_VERIFY_TIMEOUT_SECONDS = 20.0
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
    plan_id: str               # UNIQUE per CSV entry (fixes repeated Game_ID tests)
    liga: str
    short_title: str
    game_id: str               # Used for Unihockey API
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
    url = f"http://{COMPANION_IP}:{COMPANION_PORT}/api/custom-variable/{name}/value"

    log.info(
        "COMPANION SET: $(custom:%s) <= %d chars (%d lines) via POST query-param",
        name,
        len(value),
        0 if not value else (value.count("\n") + 1),
    )
    log.info("COMPANION SET URL: %s", url)
    log.info("COMPANION SET VALUE (repr): %r", value)

    try:
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
    # Plans already filtered to only those with stream info
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
    skipped_missing_stream = 0

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

            # Ignore games without complete stream info
            if not (server and key):
                skipped_missing_stream += 1
                continue

            # IMPORTANT: plan_id must be unique even if Game_ID repeats (e.g. your tests!)
            plan_id = f"{game_id}|{start_time.isoformat()}"

            plans.append(
                GamePlan(
                    plan_id=plan_id,
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

    if skipped_missing_stream:
        log.info("CSV filter: skipped %d row(s) missing Stream URL and/or Stream key", skipped_missing_stream)

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
    """
    If multiple plans overlap, pick the one that started most recently.
    """
    active = [p for p in plans if p.start_time <= now < p.stop_time]
    if not active:
        return None
    return max(active, key=lambda p: p.start_time)


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
            "UPCOMING #%d: %s %s (Plan_ID=%s Game_ID=%s) start=%s (in %s) stop=%s url=%s key=%s",
            i,
            p.liga,
            p.short_title,
            p.plan_id,
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
    last_obs_connect_try = 0.0

    plans: List[GamePlan] = []
    last_csv_reload = 0.0
    last_csv_mtime = 0.0
    force_csv_reload = False

    last_stream_auto: Optional[int] = None
    last_record_auto: Optional[int] = None

    # IMPORTANT: attempts keyed by plan_id, not game_id
    stream_start_attempts: Dict[str, int] = {}
    record_start_attempts: Dict[str, int] = {}
    MAX_START_ATTEMPTS = 1

    # Track which plan we are controlling
    controlled_plan: Optional[GamePlan] = None

    # Early-stop tracking (per game_id is fine)
    early_stop_done_for_game_id: Optional[str] = None

    last_active_plan_id: Optional[str] = None

    last_game_status_check = 0.0
    game_status_cache: Dict[str, Optional[str]] = {}

    last_is_streaming: Optional[bool] = None
    last_is_recording: Optional[bool] = None

    last_sent_game_list_today: Optional[str] = None
    last_seen_local_date: date = datetime.now(tz=tz).date()

    global _last_companion_ok, _last_companion_err
    global _last_api_ok, _last_api_err

    while True:
        now = datetime.now(tz=tz)

        # Day rollover: behave like restart (no games cross midnight)
        if now.date() != last_seen_local_date:
            log.info("DAY ROLLOVER: %s -> %s (resetting state like restart)", last_seen_local_date, now.date())
            last_seen_local_date = now.date()

            last_sent_game_list_today = None

            plans = []
            last_csv_reload = 0.0
            last_csv_mtime = 0.0

            stream_start_attempts.clear()
            record_start_attempts.clear()

            controlled_plan = None
            early_stop_done_for_game_id = None
            last_active_plan_id = None

            last_game_status_check = 0.0
            game_status_cache.clear()

            force_csv_reload = True
            log.info("")

        # -------------------------
        # OBS connect/reconnect (NON-BLOCKING)
        # -------------------------
        if obsctl is None:
            if (time.time() - last_obs_connect_try) >= OBS_RETRY_SECONDS or last_obs_connect_try == 0.0:
                last_obs_connect_try = time.time()
                obsctl = try_connect_obs()
                if obsctl is None:
                    is_streaming, is_recording = False, False
                else:
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

        # OBS output state log
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
        # -------------------------
        try:
            p = Path(STREAM_PLANNING_FILE)
            mtime = p.stat().st_mtime if p.exists() else 0.0
        except Exception:
            mtime = 0.0

        csv_reloaded_this_loop = False
        allow_csv_reload = force_csv_reload or (obsctl is None) or obs_idle

        if allow_csv_reload:
            due_time = (time.time() - last_csv_reload) >= CSV_RELOAD_IDLE_EVERY_SECONDS
            due_mtime = (mtime != 0.0 and mtime != last_csv_mtime)
            do_reload = force_csv_reload or due_time or due_mtime or not plans

            if do_reload:
                try:
                    reason = "forced reload" if force_csv_reload else ("mtime change" if due_mtime else "timer/initial")
                    plans = load_game_plans(STREAM_PLANNING_FILE, CSV_SEPARATOR, tz)
                    last_csv_reload = time.time()
                    last_csv_mtime = mtime
                    csv_reloaded_this_loop = True
                    force_csv_reload = False

                    log.info("CSV loaded: %d rows (%s)", len(plans), reason)
                    log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                    log.info("")
                except Exception as e:
                    log.error("CSV load failed: %s", e)
                    log.info("")

        # -------------------------
        # Update Game_List_Today
        # -------------------------
        if csv_reloaded_this_loop or last_sent_game_list_today is None:
            today_value = build_game_list_today_value(plans, now.date())

            log.info("Preparing $(custom:%s) for local date %s", COMPANION_VAR_GAME_LIST_TODAY, now.date().isoformat())
            log.info("Generated leagues list (%d chars):", len(today_value))
            log.info("---- BEGIN Game_List_Today ----")
            log.info("%s", today_value if today_value else "(empty)")
            log.info("---- END   Game_List_Today ----")

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
        # Active plan (overlap-safe)
        # -------------------------
        active = find_active_plan(plans, now) if plans else None

        current_active_plan_id = active.plan_id if active else None
        if current_active_plan_id != last_active_plan_id:
            last_active_plan_id = current_active_plan_id

            if active:
                log.info(
                    "ACTIVE WINDOW: %s %s (Plan_ID=%s Game_ID=%s) %s -> %s",
                    active.liga,
                    active.short_title,
                    active.plan_id,
                    active.game_id,
                    fmt_dt(active.start_time),
                    fmt_dt(active.stop_time),
                )
                early_stop_done_for_game_id = None
                game_status_cache.pop(active.game_id, None)
                log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                log.info("")
            else:
                log.info("ACTIVE WINDOW: none")
                log_upcoming(plans, now, UPCOMING_LIST_COUNT)
                log.info("")

        # -------------------------
        # HANDOVER (robust)
        # Compare by plan_id, not game_id
        # -------------------------
        if (
            active
            and obsctl is not None
            and (is_streaming or is_recording)
            and (stream_auto == 1 or record_auto == 1)
            and (controlled_plan is None or controlled_plan.plan_id != active.plan_id)
        ):
            prev_id = controlled_plan.plan_id if controlled_plan else "(unknown)"
            log.info(
                "HANDOVER: switching control %s -> %s. Stopping outputs for clean restart.",
                prev_id,
                active.plan_id,
            )
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
                    "HANDOVER stop verified: streaming=%s recording=%s",
                    onoff(is_streaming),
                    onoff(is_recording),
                )
            except Exception as e:
                log.error("HANDOVER stop failed: %s", e)

            log.info("")
            controlled_plan = None

        # -------------------------
        # Scheduled stop for the CONTROLLED plan
        # -------------------------
        if controlled_plan and obsctl is not None and (is_streaming or is_recording) and now >= controlled_plan.stop_time:
            log.info(
                "STOP TIME reached for CONTROLLED Plan_ID=%s -> stopping stream+record (stop=%s)",
                controlled_plan.plan_id,
                fmt_dt(controlled_plan.stop_time),
            )
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
            controlled_plan = None

        # -------------------------
        # Early stop FINISHED (for the CONTROLLED plan)
        # (game_id based)
        # -------------------------
        if controlled_plan and obsctl is not None and (is_streaming or is_recording) and now < controlled_plan.stop_time:
            if (time.time() - last_game_status_check) >= GAME_STATUS_CHECK_EVERY_SECONDS:
                last_game_status_check = time.time()

                status = fetch_game_status(controlled_plan.game_id)

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

                prev = game_status_cache.get(controlled_plan.game_id)
                game_status_cache[controlled_plan.game_id] = status

                if status:
                    if LOG_GAME_STATUS_EVERY_POLL:
                        if status == prev:
                            log.info(
                                "Game status polled (unchanged): Game_ID=%s status=%s",
                                controlled_plan.game_id,
                                status,
                            )
                        else:
                            log.info(
                                "Game status polled (changed):   Game_ID=%s %s -> %s",
                                controlled_plan.game_id,
                                prev,
                                status,
                            )
                        log.info("")

                if status == "FINISHED" and early_stop_done_for_game_id != controlled_plan.game_id:
                    log.info(
                        "EARLY STOP: Game_ID=%s is FINISHED before scheduled stop (%s). Stopping stream+record.",
                        controlled_plan.game_id,
                        fmt_dt(controlled_plan.stop_time),
                    )
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
                    early_stop_done_for_game_id = controlled_plan.game_id
                    controlled_plan = None

        # -------------------------
        # Start logic
        # Attempts keyed by plan_id (fix)
        # -------------------------
        if active and obsctl is not None:
            # STREAM
            if (
                stream_auto == 1
                and (stream_start_attempts.get(active.plan_id, 0) < MAX_START_ATTEMPTS)
                and not is_streaming
            ):
                stream_start_attempts[active.plan_id] = stream_start_attempts.get(active.plan_id, 0) + 1

                try:
                    log.info(
                        "AUTO START: STREAM (attempt %d/%d) Plan_ID=%s Game_ID=%s",
                        stream_start_attempts[active.plan_id],
                        MAX_START_ATTEMPTS,
                        active.plan_id,
                        active.game_id,
                    )
                    log.info("  Pre-check: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))

                    log.info("  Setting scene=%r", OBS_SCENE)
                    obsctl.set_scene(OBS_SCENE)

                    log.info("  Setting RTMP: server=%s key=%s", active.stream_server, mask_key(active.stream_key))
                    obsctl.set_rtmp_destination(active.stream_server, active.stream_key)

                    log.info("  Waiting %.1fs after setting RTMP...", WAIT_AFTER_SET_RTMP_SECONDS)
                    time.sleep(WAIT_AFTER_SET_RTMP_SECONDS)

                    log.info("  Calling start_stream() ...")
                    obsctl.start_stream()

                    is_streaming2, is_recording2 = wait_for_obs_output(obsctl, want_streaming=True, want_recording=None)
                    if is_streaming2:
                        log.info(
                            "  Streaming started (verified). streaming=%s recording=%s",
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                        controlled_plan = active
                        early_stop_done_for_game_id = None
                    else:
                        log.error(
                            "  Streaming did NOT become active within %.1fs. Last seen: streaming=%s recording=%s",
                            START_VERIFY_TIMEOUT_SECONDS,
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                    log.info("")
                    is_streaming, is_recording = is_streaming2, is_recording2
                except Exception as e:
                    log.error("Failed to start streaming: %s", e)
                    log.info("")

            # RECORD
            if (
                record_auto == 1
                and (record_start_attempts.get(active.plan_id, 0) < MAX_START_ATTEMPTS)
                and not is_recording
            ):
                record_start_attempts[active.plan_id] = record_start_attempts.get(active.plan_id, 0) + 1

                try:
                    log.info(
                        "AUTO START: RECORD (attempt %d/%d) Plan_ID=%s Game_ID=%s",
                        record_start_attempts[active.plan_id],
                        MAX_START_ATTEMPTS,
                        active.plan_id,
                        active.game_id,
                    )
                    log.info("  Pre-check: streaming=%s recording=%s", onoff(is_streaming), onoff(is_recording))

                    log.info("  Setting scene=%r", OBS_SCENE)
                    obsctl.set_scene(OBS_SCENE)

                    log.info("  Calling start_record() ...")
                    obsctl.start_record()

                    is_streaming2, is_recording2 = wait_for_obs_output(obsctl, want_streaming=None, want_recording=True)
                    if is_recording2:
                        log.info(
                            "  Recording started (verified). streaming=%s recording=%s",
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                        if controlled_plan is None:
                            controlled_plan = active
                        early_stop_done_for_game_id = None
                    else:
                        log.error(
                            "  Recording did NOT become active within %.1fs. Last seen: streaming=%s recording=%s",
                            START_VERIFY_TIMEOUT_SECONDS,
                            onoff(is_streaming2),
                            onoff(is_recording2),
                        )
                    log.info("")
                    is_streaming, is_recording = is_streaming2, is_recording2
                except Exception as e:
                    log.error("Failed to start recording: %s", e)
                    log.info("")

        # If no active window, release control
        if not active and controlled_plan is not None:
            log.info("CONTROLLED PLAN cleared (no active window). Was Plan_ID=%s", controlled_plan.plan_id)
            log.info("")
            controlled_plan = None

        time.sleep(MAIN_LOOP_SECONDS)


if __name__ == "__main__":
    main()