Scoreboard decoder (Saturn/Vega) - hardened framing/CRC, vMix-main outputs, HTML scoreboard, and simulator for testing.

Quick start (no serial hardware):
  1) pip install pyserial
  2) python main_simulator.py
  3) Open: http://localhost:8080

Live (real serial):
  1) Edit main_live.py (SERIAL_PORT/BAUD if needed)
  2) python main_live.py

vMix:
- Output is non-blocking; if vMix is down it will log and retry every 5 minutes.
- Scoreboard updates go to:
    vmix_main_scoreboard_UID = b7bd3bf4-636f-4099-9a0c-36090abc8122
- Pause/summary (old logic) updates go to:
    vmix_main_pause_UID = db3c2bbe-4ecf-4630-8987-16a3756ce48a
  using fields: TxtTop.Text, TxtBottom.Text, TxtMain.Text

HTML:
- Served at http://localhost:8080
- JSON state at http://localhost:8080/state


vMix troubleshooting:
- If names/scores update but clock/period/penalties do not, your Title field names likely differ.
- Edit outputs/vmix_main.py FIELD_* constants, or set DEBUG_SEND=True to see commands being sent.


vMix field mapping note:
- This build uses your DIGIT fields for clock (M10,M01,S10,S01) and penalties (Pen1/Pen2 per team).
- If you want to see what is being sent, set DEBUG_SEND=True in outputs/vmix_main.py.

Simulator v4 changes:
- Teams: GCZ vs ZUG
- Clock counts UP 00:00 -> 20:00
- 15:00 intermission between periods (PNU=0)
- 5x speed
- Random goals (cap 6 per team per period) + penalties (2 slots per team)


Penalty background color:
- This build can change background colors via vMix SetColor.
- Set BG_HOME_P1/BG_HOME_P2/BG_AWAY_P1/BG_AWAY_P2 in outputs/vmix_main.py to the element names of your background rectangles.
- Active color: #E60A14, inactive/transparent: #FFF00000


v6 penalty styling:
- Adds ':' to Txt*Pen*T.Text when penalty active.
- Sets Rect*Pen*.Fill.Color to active red (#E60A14) or transparent (#FFF00000).


v7 changes:
- Inactive penalties now blank all digit fields + spacer.
- SetColor values are sent exactly (#E60A14 and #FFF00000).
