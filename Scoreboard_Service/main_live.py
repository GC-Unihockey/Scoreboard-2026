import threading
import time
import serial

# --- NDI scoreboard output (optional) ---
ENABLE_NDI = True  # set True to enable NDI output
NDI_SOURCE_NAME = "Scoreboard"
NDI_WIDTH = 1920
NDI_HEIGHT = 250
NDI_FPS = 5

# UI-only scale: makes the graphic bigger/smaller INSIDE the same 1920x200 frame
NDI_UI_SCALE = 1.6  # try 1.4, 1.6, 1.8, 2.0

if ENABLE_NDI:
    from outputs.ndi_scoreboard import NDIScoreboardOutput, NDIConfig

from state import GameState
from saturn import extract_frames
from parser import parse_base, parse_time, parse_expulsion, parse_names
from summary import SummaryCalculator
from outputs.vmix_main import VmixMainClient
from web.server import run_web

# ===== LINUX SERIAL CONFIG =====
# SERIAL_PORT = "/dev/ttyUSB0"
# SERIAL_PORT = "COM3"
SERIAL_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"

BAUD_RATE = 38400
SERIAL_RETRY_SECONDS = 300

vmix_main_HOST = "192.168.10.2"
vmix_main_PORT = 8099
vmix_main_scoreboard_UID = "b7bd3bf4-636f-4099-9a0c-36090abc8122"
vmix_main_pause_UID = "db3c2bbe-4ecf-4630-8987-16a3756ce48a"

state = GameState()
buffer = bytearray()
summary_calc = SummaryCalculator()
vmix_main = VmixMainClient(vmix_main_HOST, vmix_main_PORT)

threading.Thread(target=run_web, args=(state, 8080), daemon=True).start()

# --- Start NDI output (optional) ---
if ENABLE_NDI:
    try:
        ndi_cfg = NDIConfig(
            source_name=NDI_SOURCE_NAME,
            width=NDI_WIDTH,
            height=NDI_HEIGHT,
            fps=NDI_FPS,
            ui_scale=NDI_UI_SCALE,   # <-- THIS is the new part
        )
        ndi = NDIScoreboardOutput(ndi_cfg)
        threading.Thread(target=ndi.run, args=(lambda: state,), daemon=True).start()
        print("[NDI] Started:", NDI_SOURCE_NAME)
    except Exception as e:
        print(f"[NDI] Failed to start: {e}")

print("[Live] Running. Open http://localhost:8080/state")

while True:
    try:
        print(f"[Serial] Opening {SERIAL_PORT} @ {BAUD_RATE}")
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        ser.reset_input_buffer()
        print("[Serial] Connected")

        while True:
            n = ser.in_waiting
            if n:
                buffer.extend(ser.read(n))

            if len(buffer) > 16384:
                buffer = buffer[-16384:]

            for payload in extract_frames(buffer):
                t = payload[:1]
                if t == b"N":
                    parse_names(payload, state)
                elif t == b"T":
                    parse_time(payload, state)
                elif t == b"D":
                    parse_base(payload, state)
                elif t == b"C":
                    parse_expulsion(payload, state)

            if summary_calc.update(state):
                vmix_main.update_pause_summary(state, vmix_main_pause_UID)

            vmix_main.update_scoreboard(state, vmix_main_scoreboard_UID)
            time.sleep(0.02)

    except Exception as e:
        print(f"[Serial] Error: {e} — retrying in 5 minutes")
        time.sleep(SERIAL_RETRY_SECONDS)