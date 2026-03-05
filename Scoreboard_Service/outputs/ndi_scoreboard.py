from __future__ import annotations

import ctypes
import time
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
import cv2


FOURCC_RGBA = 0x41424752
NDI_FRAME_FORMAT_PROGRESSIVE = 1


class NDIlib_video_frame_v2(ctypes.Structure):
    _fields_ = [
        ("xres", ctypes.c_int),
        ("yres", ctypes.c_int),
        ("FourCC", ctypes.c_uint32),
        ("frame_rate_N", ctypes.c_int),
        ("frame_rate_D", ctypes.c_int),
        ("picture_aspect_ratio", ctypes.c_float),
        ("frame_format_type", ctypes.c_int),
        ("timecode", ctypes.c_int64),
        ("p_data", ctypes.c_void_p),
        ("line_stride_in_bytes", ctypes.c_int),
        ("p_metadata", ctypes.c_char_p),
    ]


class NDIlib_send_create_desc(ctypes.Structure):
    _fields_ = [
        ("p_ndi_name", ctypes.c_char_p),
        ("p_groups", ctypes.c_char_p),
        ("clock_video", ctypes.c_bool),
        ("clock_audio", ctypes.c_bool),
    ]


def _load_libndi():
    return ctypes.CDLL("libndi.so")

# These values are overwritten from the calling it in main, change values in main!!!!
@dataclass
class NDIConfig:
    source_name: str = "Scoreboard NDI"
    width: int = 1920
    height: int = 250
    fps: int = 10

    # UI-only scaling (does NOT change output resolution)
    ui_scale: float = 1


class NDIScoreboardOutput:
    def __init__(self, config: NDIConfig):
        self.config = config
        self._interval = 1.0 / float(config.fps)
        self._stop = threading.Event()

        self.ndi = _load_libndi()
        self.ndi.NDIlib_initialize.restype = ctypes.c_bool
        self.ndi.NDIlib_send_create.restype = ctypes.c_void_p
        self.ndi.NDIlib_send_send_video_v2.restype = None
        self.ndi.NDIlib_send_destroy.restype = None

        if not self.ndi.NDIlib_initialize():
            raise RuntimeError("NDI init failed")

        desc = NDIlib_send_create_desc()
        desc.p_ndi_name = config.source_name.encode("utf-8")
        desc.p_groups = None
        desc.clock_video = True
        desc.clock_audio = False

        self.sender = self.ndi.NDIlib_send_create(ctypes.byref(desc))
        if not self.sender:
            raise RuntimeError("NDI send_create failed")

        self.frame = NDIlib_video_frame_v2()
        self.frame.xres = config.width
        self.frame.yres = config.height
        self.frame.FourCC = FOURCC_RGBA
        self.frame.frame_rate_N = int(config.fps) * 1000
        self.frame.frame_rate_D = 1000
        self.frame.picture_aspect_ratio = float(config.width) / float(config.height)
        self.frame.frame_format_type = NDI_FRAME_FORMAT_PROGRESSIVE
        self.frame.line_stride_in_bytes = config.width * 4

    # ---------------- helpers ----------------

    def _norm_time(self, x) -> str:
        if x is None:
            return ""
        s = str(x).strip()
        if s in ("", "0", "00:00", "0:00", "0000", "000", "00"):
            return ""
        if s.isdigit():
            if len(s) == 4:
                return f"{s[:2]}:{s[2:]}"
            if len(s) == 3:
                return f"0{s[0]}:{s[1:]}"
            if len(s) == 2:
                return f"00:{s}"
        return s

    @staticmethod
    def _text_metrics(text: str, font, scale: float, thickness: int):
        (tw, th), bl = cv2.getTextSize(str(text), font, scale, thickness)
        return tw, th, bl

    def _baseline_centered(self, text: str, y0: int, y1: int, font, scale: float, thickness: int) -> int:
        _, th, bl = self._text_metrics(text, font, scale, thickness)
        cy = (y0 + y1) // 2
        return cy + (th // 2) - bl

    def _x_left(self, x0: int, pad: int) -> int:
        return x0 + pad

    def _x_right(self, text: str, x1: int, font, scale: float, thickness: int, pad: int) -> int:
        tw, _, _ = self._text_metrics(text, font, scale, thickness)
        return x1 - pad - tw

    def _x_center(self, text: str, x0: int, x1: int, font, scale: float, thickness: int) -> int:
        tw, _, _ = self._text_metrics(text, font, scale, thickness)
        return (x0 + x1) // 2 - tw // 2

    # ---------------- render ----------------

    def render_frame(self, state) -> np.ndarray:
        w, h = self.config.width, self.config.height
        img = np.zeros((h, w, 4), dtype=np.uint8)  # BGRA

        UI = float(self.config.ui_scale)

        # ---- colors (BGRA) ----
        DARK2 = (70, 40, 40, 255)
        RED = (18, 8, 227, 255)
        WHITE = (255, 255, 255, 255)

        # ---- base sizing (scaled) ----
        def px(x: float) -> int:
            return int(round(x * UI))

        border_th = max(1, px(3))
        pad_text = px(14)

        # Row positions
        y0 = px(26)
        y1 = px(96)

        gap = px(6)
        y2 = y1 + gap
        y3 = y2 + px(34)

        # If UI is huge, keep everything inside the 200px frame
        y0 = max(0, y0)
        y3 = min(h, y3)

        # Component widths
        home_w = px(300)
        score_w = px(70)
        clock_w = px(150)
        away_w = px(300)

        sep = px(8)  # gap between boxes

        total_w = home_w + score_w + clock_w + score_w + away_w + 4 * sep
        x_base = (w - total_w) // 2  # <-- centered on output width

        # Derived X positions
        xL0 = x_base
        xL1 = xL0 + home_w

        xS1a = xL1 + sep
        xS1b = xS1a + score_w

        xC0 = xS1b + sep
        xC1 = xC0 + clock_w

        xS2a = xC1 + sep
        xS2b = xS2a + score_w

        xR0 = xS2b + sep
        xR1 = xR0 + away_w

        # ---- drawing helpers ----
        def draw_box(x0: int, y0_: int, x1: int, y1_: int, fill, border=RED):
            cv2.rectangle(img, (x0, y0_), (x1, y1_), fill, -1)
            cv2.rectangle(img, (x0, y0_), (x1, y1_), border, border_th)

        # ---- state mapping ----
        home = getattr(state, "home", None)
        away = getattr(state, "away", None)

        home_name = (getattr(home, "name", "") or "HOME").strip()
        away_name = (getattr(away, "name", "") or "AWAY").strip()

        home_score = getattr(home, "score", 0)
        away_score = getattr(away, "score", 0)

        clock = (getattr(state, "clock", "") or "00:00").strip()
        period = (getattr(state, "period_display", "") or "").strip()
        if not period:
            period = str(getattr(state, "period_number", "") or "").strip()

        home_raw = getattr(home, "penalties", []) or []
        away_raw = getattr(away, "penalties", []) or []

        home_pens = [self._norm_time(p) for p in home_raw]
        home_pens = [p for p in home_pens if p][:2]

        away_pens = [self._norm_time(p) for p in away_raw]
        away_pens = [p for p in away_pens if p][:2]

        # ---- top row boxes ----
        draw_box(xL0, y0, xL1, y1, DARK2)
        draw_box(xS1a, y0, xS1b, y1, RED)
        draw_box(xC0, y0, xC1, y1, DARK2)
        draw_box(xS2a, y0, xS2b, y1, RED)
        draw_box(xR0, y0, xR1, y1, DARK2)

        # ---- text styles (scaled) ----
        font = cv2.FONT_HERSHEY_DUPLEX

        team_scale = 1.35 * UI
        score_scale = 1.75 * UI
        clock_scale = 1.35 * UI

        team_th = max(1, int(round(3 * UI)))
        score_th = max(1, int(round(5 * UI)))
        clock_th = max(1, int(round(4 * UI)))

        # Vertical nudges
        TEXT_Y_OFFSET = px(13)
        SCORE_Y_OFFSET = px(2)

        # ---- team names ----
        ty_home = self._baseline_centered(home_name, y0, y1, font, team_scale, team_th) + TEXT_Y_OFFSET
        ty_away = self._baseline_centered(away_name, y0, y1, font, team_scale, team_th) + TEXT_Y_OFFSET

        cv2.putText(img, home_name, (self._x_left(xL0, pad_text), ty_home),
                    font, team_scale, WHITE, team_th, cv2.LINE_AA)

        cv2.putText(img, away_name,
                    (self._x_right(away_name, xR1, font, team_scale, team_th, pad_text), ty_away),
                    font, team_scale, WHITE, team_th, cv2.LINE_AA)

        # ---- scores ----
        hs = str(home_score)
        as_ = str(away_score)

        sy_h = self._baseline_centered(hs, y0, y1, font, score_scale, score_th) + TEXT_Y_OFFSET + SCORE_Y_OFFSET
        sy_a = self._baseline_centered(as_, y0, y1, font, score_scale, score_th) + TEXT_Y_OFFSET + SCORE_Y_OFFSET

        cv2.putText(img, hs, (self._x_center(hs, xS1a, xS1b, font, score_scale, score_th), sy_h),
                    font, score_scale, WHITE, score_th, cv2.LINE_AA)

        cv2.putText(img, as_, (self._x_center(as_, xS2a, xS2b, font, score_scale, score_th), sy_a),
                    font, score_scale, WHITE, score_th, cv2.LINE_AA)

        # ---- clock ----
        cy = self._baseline_centered(clock, y0, y1, font, clock_scale, clock_th) + TEXT_Y_OFFSET
        cv2.putText(img, clock, (self._x_center(clock, xC0, xC1, font, clock_scale, clock_th), cy),
                    font, clock_scale, WHITE, clock_th, cv2.LINE_AA)

        # ---- period (optional) + connector tab ----
        if period:
            pW = px(50)
            pX0 = (xC0 + xC1) // 2 - pW // 2
            pX1 = pX0 + pW

            tab_w, tab_h = px(18), px(10)
            tab_x0 = (xC0 + xC1) // 2 - tab_w // 2
            cv2.rectangle(img, (tab_x0, y1), (tab_x0 + tab_w, min(h, y1 + tab_h)), RED, -1)

            draw_box(pX0, y2, pX1, y3, RED)

            p_scale = 1.0 * UI
            p_th = max(1, int(round(3 * UI)))
            py = self._baseline_centered(period, y2, y3, font, p_scale, p_th) + TEXT_Y_OFFSET

            cv2.putText(img, period, (self._x_center(period, pX0, pX1, font, p_scale, p_th), py),
                        font, p_scale, WHITE, p_th, cv2.LINE_AA)

        # ---- penalties ----
        penW = px(95)
        pen_gap = px(10)

        pen_scale = 0.95 * UI
        pen_th = max(1, int(round(2 * UI)))

        def draw_pen(x0_: int, label: str):
            draw_box(x0_, y2, x0_ + penW, y3, RED)
            py = self._baseline_centered(label, y2, y3, font, pen_scale, pen_th) + TEXT_Y_OFFSET
            cv2.putText(img, label, (self._x_center(label, x0_, x0_ + penW, font, pen_scale, pen_th), py),
                        font, pen_scale, WHITE, pen_th, cv2.LINE_AA)

        home_start = xL0 + px(20)
        if len(home_pens) >= 1:
            draw_pen(home_start, home_pens[0])
        if len(home_pens) >= 2:
            draw_pen(home_start + penW + pen_gap, home_pens[1])

        away_end = xR1 - px(20)
        if len(away_pens) == 1:
            draw_pen(away_end - penW, away_pens[0])
        elif len(away_pens) >= 2:
            draw_pen(away_end - (2 * penW + pen_gap), away_pens[0])
            draw_pen(away_end - penW, away_pens[1])

        # Convert BGRA -> RGBA for NDI FourCC_RGBA expectation in your codepath
        rgba = img[:, :, [2, 1, 0, 3]]
        return np.ascontiguousarray(rgba)

    # ---------------- run ----------------

    def run(self, state_provider: Callable[[], object]):
        while not self._stop.is_set():
            t0 = time.time()

            frame = self.render_frame(state_provider())
            self.frame.p_data = frame.ctypes.data_as(ctypes.c_void_p)
            self.ndi.NDIlib_send_send_video_v2(self.sender, ctypes.byref(self.frame))

            dt = time.time() - t0
            if dt < self._interval:
                time.sleep(self._interval - dt)