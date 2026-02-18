#!/usr/bin/env python3
"""
Run once:
Update Stream_Planning.csv by adding missing games
from unihockey.live API.

Features:
- Skip leagues by 4-char league code (Lidl / EReg / DReg etc.)
- No blank lines before appended rows
- Only adds missing Game_IDs
- Runs once and exits (for systemd timer / boot execution)
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set, Optional
from zoneinfo import ZoneInfo

import requests


# -------------------------
# CONFIG
# -------------------------
CSV_PATH = "/mnt/One_Drive/01_GC_Streaming_Sharing/Stream_Planning.csv"
CSV_SEPARATOR = ";"

TZ_LOCAL = "Europe/Zurich"

API_URL = "https://www.unihockey.live/api/club/569/game"
ARENA_ID = 1245
SIZE = 25

DEFAULT_STREAM_URL = ""
DEFAULT_STREAM_KEY = ""

ONLY_STATUSES: Optional[Set[str]] = {"SCHEDULED"}

# Skip by Liga code (first 4 chars of league name without spaces)
SKIP_LEAGUE_CODES: Set[str] = {"lidl", "ereg", "dreg"}


# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("StreamPlanningUpdater")


# -------------------------
# HELPERS
# -------------------------
def liga_from_league_name(league_name: str) -> str:
    s = re.sub(r"\s+", "", (league_name or "").strip())
    return (s[:4] if len(s) >= 4 else s).ljust(4, "_")


def parse_game_time_to_local(game_time_iso: str, tz_local: ZoneInfo) -> datetime:
    iso = game_time_iso.strip().replace("Z", "+00:00")
    dt_utc = datetime.fromisoformat(iso)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
    return dt_utc.astimezone(tz_local)


def fmt_date_ch(dt_local: datetime) -> str:
    return dt_local.strftime("%d.%m.%Y")


def fmt_time(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M:%S")


def default_stream_details() -> str:
    if DEFAULT_STREAM_URL and DEFAULT_STREAM_KEY:
        return f"Stream URL: {DEFAULT_STREAM_URL} Stream key: {DEFAULT_STREAM_KEY}"
    return ""


def should_skip_league(league_name: str) -> bool:
    code = liga_from_league_name(league_name).lower().strip("_")
    return code in SKIP_LEAGUE_CODES


# -------------------------
# CSV FUNCTIONS
# -------------------------
def read_existing_game_ids(csv_path: str, sep: str) -> Set[str]:
    p = Path(csv_path)
    if not p.exists():
        return set()

    existing: Set[str] = set()
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=sep)
        for row in reader:
            if not row:
                continue
            if row[0].strip().lower() in ("liga", "league"):
                continue
            if len(row) >= 3:
                existing.add(row[2].strip())
    return existing


def ensure_csv_header(csv_path: str, sep: str) -> None:
    p = Path(csv_path)
    if p.exists() and p.stat().st_size > 0:
        return

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=sep)
        w.writerow(["Liga", "Short_Title", "Game_ID", "Date", "Game_Start", "Stream_Details"])
    log.info("Created CSV with header")


def append_rows(csv_path: str, sep: str, rows: List[List[str]]) -> None:
    if not rows:
        return

    with Path(csv_path).open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=sep)
        writer.writerows(rows)


# -------------------------
# API
# -------------------------
def fetch_games() -> List[Dict]:
    params = {"size": SIZE, "arenaId": ARENA_ID}
    r = requests.get(API_URL, params=params, headers={"accept": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()


# -------------------------
# MAIN RUN
# -------------------------
def run_once():
    tz_local = ZoneInfo(TZ_LOCAL)

    ensure_csv_header(CSV_PATH, CSV_SEPARATOR)
    existing_ids = read_existing_game_ids(CSV_PATH, CSV_SEPARATOR)

    games = fetch_games()

    new_rows: List[List[str]] = []

    added = 0
    skipped_existing = 0
    skipped_status = 0
    skipped_league = 0

    for g in games:
        game_id = str(g.get("id", "")).strip()
        if not game_id:
            continue

        league_name = (g.get("leagueName") or "").strip()

        if should_skip_league(league_name):
            skipped_league += 1
            continue

        status = (g.get("status") or "").strip()
        if ONLY_STATUSES and status not in ONLY_STATUSES:
            skipped_status += 1
            continue

        if game_id in existing_ids:
            skipped_existing += 1
            continue

        short_title = (g.get("shortTitle") or "").strip()
        game_time = (g.get("gameTime") or "").strip()

        if not league_name or not short_title or not game_time:
            continue

        dt_local = parse_game_time_to_local(game_time, tz_local)

        row = [
            liga_from_league_name(league_name),
            short_title,
            game_id,
            fmt_date_ch(dt_local),
            fmt_time(dt_local),
            default_stream_details(),
        ]

        new_rows.append(row)
        existing_ids.add(game_id)
        added += 1

    append_rows(CSV_PATH, CSV_SEPARATOR, new_rows)

    log.info(
        "Run complete → Added=%d | Existing=%d | StatusSkip=%d | LeagueSkip=%d",
        added, skipped_existing, skipped_status, skipped_league
    )


def main():
    log.info("Stream Planning Updater started (run once mode)")
    run_once()
    log.info("Stream Planning Updater finished")


if __name__ == "__main__":
    main()
