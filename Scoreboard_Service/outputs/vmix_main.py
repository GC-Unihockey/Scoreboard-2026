import socket
import time
import re

# Non-blocking vMix "main" client.
# Updates:
# - Inactive penalties => all digit fields + spacer set to "" (blank), not "00:00"
# - Active penalties => show digits + ":" spacer and set background fill color
# - Color values are sent EXACTLY as configured (no normalization), matching your original script
#
# IMPORTANT CHANGE:
# - Added per-field de-duplication so vMix only receives updates when a value actually changes.

RETRY_SECONDS = 300
DEBUG_SEND = False

# User requested colors (send as-is)
COLOR_BACKGROUND_ACTIVE = "#E60A14"
COLOR_BACKGROUND_TRANSPARENT = "#FFF00000"

# Score/name fields
FIELD_HOME_SCORE = "TxtHomeScore.Text"
FIELD_AWAY_SCORE = "TxtAwayScore.Text"
FIELD_HOME_NAME  = "TxtHomeName.Text"
FIELD_AWAY_NAME  = "TxtAwayName.Text"

# Clock digit fields (MM:SS shown via digits)
FIELD_CLK_M10 = "TxtClockTimeM10.Text"
FIELD_CLK_M01 = "TxtClockTimeM01.Text"
FIELD_CLK_S10 = "TxtClockTimeS10.Text"
FIELD_CLK_S01 = "TxtClockTimeS01.Text"

# Period field
FIELD_PERIOD = "TxtClockPeriod.Text"

# Penalties: 2 per team, each as MM:SS digits + ':' spacer + background fill
HOME_P1_M10 = "TxtHomePen1M10.Text"
HOME_P1_M01 = "TxtHomePen1M01.Text"
HOME_P1_S10 = "TxtHomePen1S10.Text"
HOME_P1_S01 = "TxtHomePen1S01.Text"
HOME_P1_T   = "TxtHomePen1T.Text"
HOME_P1_BG  = "RectHomePen1.Fill.Color"

HOME_P2_M10 = "TxtHomePen2M10.Text"
HOME_P2_M01 = "TxtHomePen2M01.Text"
HOME_P2_S10 = "TxtHomePen2S10.Text"
HOME_P2_S01 = "TxtHomePen2S01.Text"
HOME_P2_T   = "TxtHomePen2T.Text"
HOME_P2_BG  = "RectHomePen2.Fill.Color"

AWAY_P1_M10 = "TxtAwayPen1M10.Text"
AWAY_P1_M01 = "TxtAwayPen1M01.Text"
AWAY_P1_S10 = "TxtAwayPen1S10.Text"
AWAY_P1_S01 = "TxtAwayPen1S01.Text"
AWAY_P1_T   = "TxtAwayPen1T.Text"
AWAY_P1_BG  = "RectAwayPen1.Fill.Color"

AWAY_P2_M10 = "TxtAwayPen2M10.Text"
AWAY_P2_M01 = "TxtAwayPen2M01.Text"
AWAY_P2_S10 = "TxtAwayPen2S10.Text"
AWAY_P2_S01 = "TxtAwayPen2S01.Text"
AWAY_P2_T   = "TxtAwayPen2T.Text"
AWAY_P2_BG  = "RectAwayPen2.Fill.Color"

# Pause/summary fields
FIELD_SUM_TOP    = "TxtTop.Text"
FIELD_SUM_BOTTOM = "TxtBottom.Text"
FIELD_SUM_MAIN   = "TxtMain.Text"


def _digits_mmss(value: str):
    """Return (m10,m01,s10,s01) as strings, or blanks if invalid/blank."""
    if value is None:
        return ("", "", "", "")
    v = str(value).strip()
    if not v:
        return ("", "", "", "")

    if ":" in v:
        parts = v.split(":")
        if len(parts) != 2:
            return ("", "", "", "")
        mm = re.sub(r"\D", "", parts[0]).rjust(2, "0")[-2:]
        ss = re.sub(r"\D", "", parts[1]).rjust(2, "0")[-2:]
    else:
        digits = re.sub(r"\D", "", v)
        if not digits:
            return ("", "", "", "")
        if len(digits) == 3:
            digits = "0" + digits
        if len(digits) < 4:
            digits = digits.rjust(4, "0")
        mm, ss = digits[:2], digits[2:4]

    return (mm[0], mm[1], ss[0], ss[1])


class VmixMainClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = None
        self._last_attempt = 0.0

        # De-dup cache:
        # key = (kind, uid, selected_name) -> last sent value
        self._last_sent = {}

    def _maybe_connect(self):
        if self.sock is not None:
            return
        now = time.time()
        if now - self._last_attempt < RETRY_SECONDS:
            return
        self._last_attempt = now
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((self.host, self.port))
            s.settimeout(None)
            self.sock = s

            # New connection: safest is to clear cache so next calls re-send current state.
            self._last_sent.clear()

            print("[vMix-main] Connected")
        except Exception as e:
            print(f"[vMix-main] Not available: {e}")
            self.sock = None

    def _disconnect(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

        # On disconnect, clear cache so we don't "skip" updates after reconnect.
        self._last_sent.clear()

    def _send(self, cmd: str) -> bool:
        """Send raw command. Returns True if sent, False if not connected/failed."""
        self._maybe_connect()
        if self.sock is None:
            return False
        try:
            if DEBUG_SEND:
                print("[vMix-main SEND]", cmd.strip())
            self.sock.sendall(cmd.encode("ascii"))
            return True
        except Exception as e:
            print(f"[vMix-main] Send failed: {e}")
            self._disconnect()
            return False

    def _set_text(self, uid: str, value: str, selected_name: str):
        value = "" if value is None else str(value)
        key = ("text", uid, selected_name)
        if self._last_sent.get(key) == value:
            return
        if self._send(f"FUNCTION SetText Input={uid}&Value={value}&SelectedName={selected_name}\r\n"):
            self._last_sent[key] = value

    def _set_color(self, uid: str, color: str, selected_name: str):
        color = "" if color is None else str(color)
        key = ("color", uid, selected_name)
        if self._last_sent.get(key) == color:
            return
        if self._send(f"FUNCTION SetColor Input={uid}&Value={color}&SelectedName={selected_name}\r\n"):
            self._last_sent[key] = color

    def _penalty_active(self, pen_str: str) -> bool:
        if pen_str is None:
            return False
        s = str(pen_str).strip()
        if not s:
            return False
        if s in ("0000", "00:00", "0:00"):
            return False
        digits = re.sub(r"\D", "", s)
        return bool(digits) and int(digits) != 0

    def _set_mmss_digits(self, uid: str, value: str, f_m10: str, f_m01: str, f_s10: str, f_s01: str):
        m10, m01, s10, s01 = _digits_mmss(value)
        self._set_text(uid, m10, f_m10)
        self._set_text(uid, m01, f_m01)
        self._set_text(uid, s10, f_s10)
        self._set_text(uid, s01, f_s01)

    def _set_penalty_block(self, uid: str, value: str, f_m10: str, f_m01: str, f_s10: str, f_s01: str, f_t: str, f_bg: str):
        active = self._penalty_active(value)
        if active:
            self._set_mmss_digits(uid, value, f_m10, f_m01, f_s10, f_s01)
            self._set_text(uid, ":", f_t)
            self._set_color(uid, COLOR_BACKGROUND_ACTIVE, f_bg)
        else:
            # Inactive -> blank everything
            self._set_text(uid, "", f_m10)
            self._set_text(uid, "", f_m01)
            self._set_text(uid, "", f_s10)
            self._set_text(uid, "", f_s01)
            self._set_text(uid, "", f_t)
            self._set_color(uid, COLOR_BACKGROUND_TRANSPARENT, f_bg)

    def update_scoreboard(self, state, vmix_main_scoreboard_UID: str):
        self._set_text(vmix_main_scoreboard_UID, str(state.home.score), FIELD_HOME_SCORE)
        self._set_text(vmix_main_scoreboard_UID, str(state.away.score), FIELD_AWAY_SCORE)
        #self._set_text(vmix_main_scoreboard_UID, state.home.name or "", FIELD_HOME_NAME)
        #self._set_text(vmix_main_scoreboard_UID, state.away.name or "", FIELD_AWAY_NAME)

        self._set_mmss_digits(vmix_main_scoreboard_UID, state.clock or "", FIELD_CLK_M10, FIELD_CLK_M01, FIELD_CLK_S10, FIELD_CLK_S01)

        self._set_text(vmix_main_scoreboard_UID, state.period_display or "", FIELD_PERIOD)

        hp = (state.home.penalties or ["", "", ""]) + ["", ""]
        ap = (state.away.penalties or ["", "", ""]) + ["", ""]

        self._set_penalty_block(vmix_main_scoreboard_UID, hp[0], HOME_P1_M10, HOME_P1_M01, HOME_P1_S10, HOME_P1_S01, HOME_P1_T, HOME_P1_BG)
        self._set_penalty_block(vmix_main_scoreboard_UID, hp[1], HOME_P2_M10, HOME_P2_M01, HOME_P2_S10, HOME_P2_S01, HOME_P2_T, HOME_P2_BG)
        self._set_penalty_block(vmix_main_scoreboard_UID, ap[0], AWAY_P1_M10, AWAY_P1_M01, AWAY_P1_S10, AWAY_P1_S01, AWAY_P1_T, AWAY_P1_BG)
        self._set_penalty_block(vmix_main_scoreboard_UID, ap[1], AWAY_P2_M10, AWAY_P2_M01, AWAY_P2_S10, AWAY_P2_S01, AWAY_P2_T, AWAY_P2_BG)

    def update_pause_summary(self, state, vmix_main_pause_UID: str):
        self._set_text(vmix_main_pause_UID, state.summary.top or "", FIELD_SUM_TOP)
        self._set_text(vmix_main_pause_UID, state.summary.bottom or "", FIELD_SUM_BOTTOM)
        self._set_text(vmix_main_pause_UID, state.summary.main or "", FIELD_SUM_MAIN)
