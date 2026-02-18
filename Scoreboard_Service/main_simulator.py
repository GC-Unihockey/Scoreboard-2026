import threading
from state import GameState
from saturn import extract_frames
from parser import parse_base, parse_time, parse_expulsion, parse_names
from summary import SummaryCalculator
from outputs.vmix_main import VmixMainClient
from web.server import run_web
from simulator import generate_stream

# --- NDI scoreboard output (optional) ---
ENABLE_NDI = True  # set True to enable NDI output
NDI_SOURCE_NAME = "Scoreboard"
NDI_WIDTH = 1920
NDI_HEIGHT = 1080
NDI_FPS = 50

if ENABLE_NDI:
    from outputs.ndi_scoreboard import NDIScoreboardOutput, NDIConfig

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
            fps=NDI_FPS
        )
        ndi = NDIScoreboardOutput(ndi_cfg)
        threading.Thread(target=ndi.run, args=(lambda: state,), daemon=True).start()
        print("[NDI] Started:", NDI_SOURCE_NAME)
    except Exception as e:
        print(f"[NDI] Failed to start: {e}")
print("[Simulator] Running. Open http://localhost:8080")

for chunk in generate_stream():
    buffer.extend(chunk)

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
