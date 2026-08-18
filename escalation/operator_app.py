"""Minimal (deliberately mocked) operator UI for the handoff described in
escalation/handoff.py. Runs as a Flask app in a background thread of the
same process as the automation, so it shares the live HandoffController
instance.

Scope note (mirrors the brief): this is NOT a co-browsing console. It does
not render or proxy the live page -- it shows the context captured at the
moment of escalation (goal, step, reason, a screenshot taken by the
automation thread just before pausing) and a single control: resume. The
human actually operates the live session by looking at the real, visible
Playwright browser window (headed) and using their own mouse/keyboard --
that window is not touched or recreated by this app. See REPORT.md section
5 for what a real co-browsing operator console would add.
"""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, send_file

from escalation.handoff import HandoffController

_PAGE = """
<html><head><title>Operator Console</title></head>
<body style="font-family: sans-serif; max-width: 720px; margin: 40px auto;">
<h1>Operator Console</h1>
{% if req %}
  <p><b>Run:</b> {{ req.run_id }} &nbsp; <b>Capability:</b> {{ req.capability }}</p>
  <p><b>Goal:</b> {{ req.goal }}</p>
  <p><b>Stopped at step:</b> {{ req.step_id }}</p>
  <p><b>Reason:</b> {{ req.reason }}</p>
  <p><b>Current URL:</b> {{ req.current_url }}</p>
  {% if req.screenshot_path %}
  <p><img src="/screenshot" style="max-width:100%; border:1px solid #999;"></p>
  {% endif %}
  <p>The live browser window is still open on this machine -- switch to it, complete the
     step by hand, then describe what you did below and resume.</p>
  <form method="POST" action="/resume">
    <textarea name="notes" rows="3" style="width:100%;" placeholder="What did you do?"></textarea><br><br>
    <button type="submit">Resume automation</button>
  </form>
{% else %}
  <p>No active intervention. Automation is running normally.</p>
{% endif %}
</body></html>
"""

_RESUMED_PAGE = """
<html><body style="font-family: sans-serif; max-width: 720px; margin: 40px auto;">
<h1>Resumed</h1>
<p>Control handed back to automation. You can close this tab.</p>
</body></html>
"""


def create_operator_app(controller: HandoffController) -> Flask:
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        return render_template_string(_PAGE, req=controller.active_request)

    @app.route("/screenshot", methods=["GET"])
    def screenshot():
        req = controller.active_request
        if not req or not req.screenshot_path or not Path(req.screenshot_path).exists():
            return "", 404
        return send_file(req.screenshot_path)

    @app.route("/resume", methods=["POST"])
    def resume():
        notes = request.form.get("notes", "")
        controller.resume(notes)
        return render_template_string(_RESUMED_PAGE)

    return app


def run_operator_server(controller: HandoffController, port: int) -> threading.Thread:
    app = create_operator_app(controller)
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
