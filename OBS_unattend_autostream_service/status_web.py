#!/usr/bin/env python3
"""Read-only HTML status page for auto_stream.py.

auto_stream.py builds a plain dict snapshot once per main loop tick and
hands it to StatusStore.update(). This module only renders that snapshot
as HTML and serves it over HTTP; it has no knowledge of OBS/Companion/CSV
internals.
"""
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional


class StatusStore:
    """Holds the latest status snapshot (whole-dict replacement, no lock needed)."""

    def __init__(self):
        self.latest: Dict[str, Any] = {}

    def update(self, snapshot: Dict[str, Any]) -> None:
        self.latest = snapshot

    def get(self) -> Dict[str, Any]:
        return self.latest


def _esc(v: Any) -> str:
    return html.escape(str(v)) if v is not None and v != "" else "-"


def _badge(ok: Optional[bool], true_text: str = "OK", false_text: str = "DOWN") -> str:
    if ok is None:
        return '<span class="badge badge-unknown">UNKNOWN</span>'
    cls = "badge-ok" if ok else "badge-bad"
    text = true_text if ok else false_text
    return f'<span class="badge {cls}">{_esc(text)}</span>'


def _onoff(v: Optional[bool]) -> str:
    if v is None:
        return '<span class="badge badge-unknown">UNKNOWN</span>'
    return '<span class="badge badge-ok">ON</span>' if v else '<span class="badge badge-off">OFF</span>'


def _plan_rows(plans: List[Dict[str, Any]], show_countdown: bool) -> str:
    if not plans:
        return '<tr><td colspan="8" class="muted">none</td></tr>'
    rows = []
    for p in plans:
        countdown = f"<td>{_esc(p.get('starts_in'))}</td>" if show_countdown else ""
        rows.append(
            "<tr>"
            f"<td>{_esc(p.get('liga'))}</td>"
            f"<td>{_esc(p.get('short_title'))}</td>"
            f"<td>{_esc(p.get('game_id'))}</td>"
            f"<td>{_esc(p.get('start_time'))}</td>"
            f"{countdown}"
            f"<td>{_esc(p.get('stop_time'))}</td>"
            f"<td>{_esc(p.get('stream_server'))}</td>"
            f"<td class=\"key\">{_esc(p.get('stream_key'))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _dict_rows(d: Dict[str, Any], key_label: str, value_label: str) -> str:
    if not d:
        return f'<tr><td colspan="2" class="muted">none</td></tr>'
    rows = [f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in d.items()]
    return "\n".join(rows)


_CSS = """
body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1a1a1a; background: #fafafa; }
h1 { margin-bottom: 0.2rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
.meta { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #e5e5e5; font-size: 0.92rem; }
th { background: #f0f0f0; }
.key { font-family: monospace; word-break: break-all; }
.muted { color: #999; font-style: italic; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px; font-size: 0.82rem; font-weight: 600; }
.badge-ok { background: #d9f2d9; color: #1a6b1a; }
.badge-bad { background: #f8d6d6; color: #a31414; }
.badge-off { background: #eee; color: #666; }
.badge-unknown { background: #eee; color: #999; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }
.card { background: #fff; border: 1px solid #e5e5e5; border-radius: 6px; padding: 0.8rem 1rem; }
.card h3 { margin: 0 0 0.5rem 0; font-size: 1rem; }
.card dl { margin: 0; }
.card dt { color: #666; font-size: 0.82rem; }
.card dd { margin: 0 0 0.5rem 0; }
"""


def render_page(s: Dict[str, Any]) -> bytes:
    if not s:
        body = "<p>No status yet - the service just started.</p>"
    else:
        config = s.get("config", {})
        body = f"""
<div class="meta">
  Now: {_esc(s.get('now'))} &middot;
  Process started: {_esc(s.get('process_started'))} &middot;
  Last loop tick: {_esc(s.get('last_loop_at'))}
</div>

<h2>Connections</h2>
<div class="grid">
  <div class="card">
    <h3>OBS</h3>
    <dl>
      <dt>Connection</dt><dd>{_badge(s.get('obs_connected'))}</dd>
      <dt>Host</dt><dd>{_esc(s.get('obs_host'))}:{_esc(s.get('obs_port'))}</dd>
      <dt>Scene</dt><dd>{_esc(s.get('obs_scene'))}</dd>
      <dt>Streaming</dt><dd>{_onoff(s.get('is_streaming'))}</dd>
      <dt>Recording</dt><dd>{_onoff(s.get('is_recording'))}</dd>
    </dl>
  </div>
  <div class="card">
    <h3>Companion</h3>
    <dl>
      <dt>Connection</dt><dd>{_badge(s.get('companion_connected'))}</dd>
      <dt>Address</dt><dd>{_esc(s.get('companion_ip'))}:{_esc(s.get('companion_port'))}</dd>
      <dt>Last error</dt><dd>{_esc(s.get('companion_err'))}</dd>
      <dt>Stream_Auto</dt><dd>{_onoff(s.get('stream_auto'))}</dd>
      <dt>Record_Auto</dt><dd>{_onoff(s.get('record_auto'))}</dd>
      <dt>Auto mode</dt><dd>{_esc(s.get('auto_mode'))}</dd>
    </dl>
  </div>
  <div class="card">
    <h3>Unihockey API</h3>
    <dl>
      <dt>Connection</dt><dd>{_badge(s.get('api_connected'))}</dd>
      <dt>Last error</dt><dd>{_esc(s.get('api_err'))}</dd>
    </dl>
  </div>
</div>

<h2>Active window</h2>
<div class="grid">
  <div class="card">
    <h3>Active (schedule window)</h3>
    {_active_plan_card(s.get('active_plan'))}
  </div>
  <div class="card">
    <h3>Controlled (OBS is following)</h3>
    {_active_plan_card(s.get('controlled_plan'))}
    <dt>Early-stopped game</dt><dd>{_esc(s.get('early_stop_done_for_game_id'))}</dd>
  </div>
</div>

<h2>Upcoming games</h2>
<table>
  <tr><th>Liga</th><th>Title</th><th>Game ID</th><th>Start</th><th>Starts in</th><th>Stop</th><th>Stream server</th><th>Stream key</th></tr>
  {_plan_rows(s.get('upcoming_plans', []), show_countdown=True)}
</table>

<h2>Schedule source</h2>
<div class="card">
  <dl>
    <dt>CSV path</dt><dd>{_esc(s.get('csv_path'))}</dd>
    <dt>Rows loaded</dt><dd>{_esc(s.get('csv_rows_loaded'))}</dd>
    <dt>Rows skipped (missing stream info)</dt><dd>{_esc(s.get('csv_rows_skipped'))}</dd>
    <dt>Last reload</dt><dd>{_esc(s.get('csv_last_reload'))}</dd>
    <dt>Last reload reason</dt><dd>{_esc(s.get('csv_last_reload_reason'))}</dd>
  </dl>
</div>

<h2>Game status polling</h2>
<table>
  <tr><th>Game ID</th><th>Last known status</th></tr>
  {_dict_rows(s.get('game_status_cache', {}), 'Game ID', 'Status')}
</table>
<div class="meta">Last checked: {_esc(s.get('last_game_status_check'))}</div>

<h2>Companion sync</h2>
<div class="card">
  <dt>Game_List_Today (last sent)</dt>
  <dd><pre>{_esc(s.get('game_list_today')) or '(empty)'}</pre></dd>
</div>

<h2>Start attempts</h2>
<div class="grid">
  <div class="card">
    <h3>Stream (max {_esc(config.get('max_start_attempts', s.get('max_start_attempts')))} per plan)</h3>
    <table><tr><th>Plan ID</th><th>Attempts</th></tr>{_dict_rows(s.get('stream_start_attempts', {}), 'Plan', 'Attempts')}</table>
  </div>
  <div class="card">
    <h3>Record (max {_esc(config.get('max_start_attempts', s.get('max_start_attempts')))} per plan)</h3>
    <table><tr><th>Plan ID</th><th>Attempts</th></tr>{_dict_rows(s.get('record_start_attempts', {}), 'Plan', 'Attempts')}</table>
  </div>
</div>

<h2>Config reference</h2>
<div class="card">
  <dl>
    <dt>Lead / duration (minutes)</dt><dd>{_esc(config.get('lead_minutes'))} / {_esc(config.get('duration_minutes'))}</dd>
    <dt>Main loop interval (s)</dt><dd>{_esc(config.get('main_loop_seconds'))}</dd>
    <dt>CSV reload interval when idle (s)</dt><dd>{_esc(config.get('csv_reload_idle_every_seconds'))}</dd>
    <dt>Game status check interval (s)</dt><dd>{_esc(config.get('game_status_check_every_seconds'))}</dd>
    <dt>OBS retry interval (s)</dt><dd>{_esc(config.get('obs_retry_seconds'))}</dd>
    <dt>Start verify timeout (s)</dt><dd>{_esc(config.get('start_verify_timeout_seconds'))}</dd>
  </dl>
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Auto Stream Status</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Auto Stream Status</h1>
{body}
</body>
</html>
""".encode("utf-8")


def _active_plan_card(p: Optional[Dict[str, Any]]) -> str:
    if not p:
        return '<p class="muted">none</p>'
    return f"""<dl>
      <dt>Liga / Title</dt><dd>{_esc(p.get('liga'))} - {_esc(p.get('short_title'))}</dd>
      <dt>Game ID</dt><dd>{_esc(p.get('game_id'))}</dd>
      <dt>Plan ID</dt><dd>{_esc(p.get('plan_id'))}</dd>
      <dt>Window</dt><dd>{_esc(p.get('start_time'))} &rarr; {_esc(p.get('stop_time'))}</dd>
      <dt>Stream server</dt><dd>{_esc(p.get('stream_server'))}</dd>
      <dt>Stream key</dt><dd class="key">{_esc(p.get('stream_key'))}</dd>
    </dl>"""


class _Handler(BaseHTTPRequestHandler):
    store: Optional[StatusStore] = None

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        body = render_page(self.store.get() if self.store else {})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_web(store: StatusStore, port: int) -> None:
    _Handler.store = store
    server = HTTPServer(("0.0.0.0", port), _Handler)
    server.serve_forever()
