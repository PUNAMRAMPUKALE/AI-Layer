"""Proves the handoff mechanism (escalation/handoff.py + operator_app.py) is
real: automation genuinely blocks, the operator app genuinely unblocks it
over HTTP (as a real human clicking Resume would), and a timeout with no
operator present genuinely surfaces as a failure rather than hanging."""
import socket
import threading
import time

import pytest
import requests

from escalation.handoff import HandoffController, InterventionRequest
from escalation.operator_app import run_operator_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_human_resume_unblocks_automation_thread(tmp_path):
    controller = HandoffController(tmp_path, run_id="test-run")
    port = _free_port()
    run_operator_server(controller, port)
    base_url = f"http://127.0.0.1:{port}"

    for _ in range(50):
        try:
            requests.get(base_url + "/", timeout=0.5)
            break
        except requests.ConnectionError:
            time.sleep(0.1)

    req = InterventionRequest(
        run_id="test-run", capability="open_member_subaccount", goal="test goal",
        step_id=5, reason="unrecognized page state", current_url="http://mock/members/1",
        screenshot_path=None,
    )
    controller.raise_intervention(req)
    assert controller.state.value == "human"

    resumed = {}

    def wait_in_background():
        resumed["record"] = controller.wait_for_resume(timeout_s=10)

    t = threading.Thread(target=wait_in_background)
    t.start()

    # Give the automation thread a moment to actually be blocked, then act as
    # the human operator would: fetch the console, see the context, resume.
    time.sleep(0.3)
    page = requests.get(base_url + "/")
    assert "unrecognized page state" in page.text
    resp = requests.post(base_url + "/resume", data={"notes": "Manually confirmed the member and clicked through."})
    assert resp.status_code == 200

    t.join(timeout=5)
    assert not t.is_alive(), "wait_for_resume did not unblock after operator resumed"
    assert resumed["record"].notes == "Manually confirmed the member and clicked through."
    assert controller.state.value == "automation"


def test_wait_for_resume_times_out_without_a_human(tmp_path):
    controller = HandoffController(tmp_path, run_id="test-run-2")
    req = InterventionRequest(
        run_id="test-run-2", capability="x", goal="y", step_id=1, reason="stuck",
        current_url=None, screenshot_path=None,
    )
    controller.raise_intervention(req)
    with pytest.raises(TimeoutError):
        controller.wait_for_resume(timeout_s=0.5)
