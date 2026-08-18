"""Human-in-the-loop escalation & handoff (core requirement 3.6).

The seam this implements: automation must be able to pause, cede control of
the *same* live session to a human, and resume once they're done -- not spin
up a fresh session or hand over a description of the problem.

How control transfer actually works here: the Playwright browser is launched
headed (a real, visible OS window), so when automation "pauses" it simply
stops issuing Playwright commands and blocks on an Event. Nothing about the
browser process changes. A human can click and type directly into that same
window with their own hands, or (for a remote operator, out of scope to
build fully -- see REPORT.md) a real product would attach a co-browsing
viewer to the same CDP endpoint the automation used. The operator app
(escalation/operator_app.py) is the mocked part: a minimal local control
surface for signaling resume and recording what the human did, not a full
co-browsing console (explicitly out of scope per the brief).

"Who is in control" is tracked explicitly via HandoffController.state so a
caller can always ask, rather than inferring it from whether Playwright
calls happen to be in flight.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class ControlState(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class InterventionRequest:
    run_id: str
    capability: str
    goal: str
    step_id: int | None
    reason: str
    current_url: str | None
    screenshot_path: str | None
    raised_at: float = field(default_factory=time.time)


@dataclass
class HumanActionRecord:
    notes: str
    resumed_at: float = field(default_factory=time.time)


class HandoffController:
    """One instance per run. Owns the pause/resume signal and the record of
    who's in control. Shared between the automation thread (agent loop or
    replay engine) and the operator Flask app, which runs in a background
    thread in the same process so both sides touch the same live objects."""

    def __init__(self, evidence_dir: Path, run_id: str):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.state = ControlState.AUTOMATION
        self.active_request: InterventionRequest | None = None
        self.history: list[dict] = []
        self._resume_event = threading.Event()
        self._last_human_action: HumanActionRecord | None = None

    def raise_intervention(self, request: InterventionRequest) -> Path:
        self.active_request = request
        self.state = ControlState.HUMAN
        self._resume_event.clear()
        self.history.append({"event": "escalated", **asdict(request)})
        path = self.evidence_dir / f"intervention_{request.run_id}_{request.step_id}.json"
        path.write_text(json.dumps(asdict(request), indent=2), encoding="utf-8")
        return path

    def wait_for_resume(self, timeout_s: float | None = None) -> HumanActionRecord:
        """Blocks the automation thread. The same live browser window remains
        open and human-operable the entire time this call is blocked."""
        got = self._resume_event.wait(timeout=timeout_s)
        if not got:
            raise TimeoutError(
                f"No human operator resumed run {self.run_id} within {timeout_s}s."
            )
        record = self._last_human_action
        self.state = ControlState.AUTOMATION
        self.active_request = None
        self._resume_event.clear()
        self.history.append({"event": "resumed", **asdict(record)})
        self._write_history()
        return record

    def resume(self, notes: str) -> None:
        """Called by the operator app when the human signals they're done."""
        self._last_human_action = HumanActionRecord(notes=notes)
        self._resume_event.set()

    def _write_history(self) -> None:
        path = self.evidence_dir / f"handoff_history_{self.run_id}.json"
        path.write_text(json.dumps(self.history, indent=2), encoding="utf-8")
