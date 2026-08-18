"""Deterministic replay: the production execution path (brief section 3.3).
No LLM call anywhere in this module -- every action comes straight from the
artifact's recorded steps, resolved through the same core.locators fallback
chain discovery used to build them.

Error taxonomy, concretely: after every step, `known_outcomes` are checked
*before* the step's own checkpoint. That ordering matters -- a "no such
member" page and a "the flow got lost" page can both fail the same naive
checkpoint, but only a known_outcomes match tells you which one you're
looking at. Three exits from that check:
  - BUSINESS_OUTCOME -> stop, return status=business_outcome (not a crash).
  - RECOVERABLE      -> run the recorded recovery action, then retry.
  - HARD_FAILURE     -> stop, return status=failure with debug detail.
Anything that matches neither known_outcomes nor the step's own checkpoint is
an *unrecognized* state -- treated as failure by default, or routed to a
human via escalation/handoff.py if escalate_on_failure is set (brief 3.6:
"a replay hits a condition it can't recover from").
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from core.artifact import ActionType, CapabilityArtifact, Checkpoint, CheckpointKind, OutcomeClassification, Step
from core.guardrails import Guardrails, GuardrailViolation, redact_dict, redact_text
from core.locators import resolve
from core.result import BusinessOutcome, FailureDetail, RecoveredEvent, ResultStatus, RunResult
from escalation.handoff import HandoffController, InterventionRequest


class ReplayError(Exception):
    pass


def _check_checkpoint(page: Page, checkpoint: Checkpoint) -> bool:
    if checkpoint.kind == CheckpointKind.URL_CONTAINS:
        return checkpoint.value in page.url
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body = ""
    if checkpoint.kind == CheckpointKind.TEXT_PRESENT:
        return checkpoint.value in body
    if checkpoint.kind == CheckpointKind.TEXT_ABSENT:
        return checkpoint.value not in body
    if checkpoint.kind == CheckpointKind.ELEMENT_VISIBLE:
        try:
            return page.locator(checkpoint.value).first.is_visible()
        except Exception:  # noqa: BLE001
            return False
    return False


def _snippet(page: Page, n: int = 400) -> str:
    try:
        return redact_text(page.inner_text("body")[:n])
    except Exception:  # noqa: BLE001
        return "(could not read page)"


def _resolve_value(step: Step, params: dict) -> str:
    if step.value_param is not None:
        if step.value_param not in params:
            raise ReplayError(f"Missing required parameter '{step.value_param}' for step {step.step_id}")
        return str(params[step.value_param])
    return step.value_literal or ""


class ReplayEngine:
    def __init__(
        self,
        guardrails: Guardrails,
        evidence_root: Path,
        allow_risky: bool = False,
        escalate_on_failure: bool = False,
        headless: bool = True,
    ):
        self.guardrails = guardrails
        self.evidence_root = Path(evidence_root)
        self.allow_risky = allow_risky
        self.escalate_on_failure = escalate_on_failure
        self.headless = headless

    def run(self, artifact: CapabilityArtifact, params: dict, on_escalation_wait=None) -> RunResult:
        run_id = f"replay-{uuid.uuid4().hex[:8]}"
        run_dir = self.evidence_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        log_f = open(run_dir / "log.jsonl", "a", encoding="utf-8")

        def log(event: dict) -> None:
            record = {"ts": time.time(), **event}
            log_f.write(json.dumps(redact_dict(record), default=str) + "\n")
            log_f.flush()

        def fail(step: Step | None, expected: str, observed: str, outputs: dict, recovered: list, message: str | None = None) -> RunResult:
            return RunResult(
                status=ResultStatus.FAILURE,
                artifact_name=artifact.name,
                artifact_version=artifact.version,
                run_id=run_id,
                outputs=outputs,
                recovered_events=recovered,
                failure=FailureDetail(
                    step_id=step.step_id if step else 0,
                    expected=expected,
                    observed=observed,
                    message=message or (f"Step {step.step_id} ({step.action.value}) did not reach the expected state." if step else expected),
                ),
                evidence_dir=str(run_dir),
            )

        def finish(result: RunResult) -> RunResult:
            log({"event": "run_end", "status": result.status.value})
            log_f.close()
            return result

        log({"event": "run_start", "artifact": artifact.name, "version": artifact.version, "params": redact_dict(dict(params))})

        outputs: dict = {}
        recovered: list[RecoveredEvent] = []

        for p in artifact.parameters:
            if p.required and p.name not in params:
                return finish(fail(None, f"parameter '{p.name}' provided", "missing", outputs, recovered, message=f"Required parameter '{p.name}' was not provided."))

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            page = browser.new_page()
            handoff = HandoffController(run_dir, run_id)
            try:
                for step in artifact.steps:
                    try:
                        self.guardrails.check_action_type(step.action.value)
                    except GuardrailViolation as e:
                        log({"event": "guardrail_violation", "step_id": step.step_id, "error": str(e)})
                        return finish(fail(step, "action permitted by policy", str(e), outputs, recovered))

                    if step.risk_level == "risky" and not self.allow_risky:
                        log({"event": "risky_step_blocked", "step_id": step.step_id})
                        return finish(
                            fail(
                                step,
                                "risky step approved for unattended replay (allow_risky=True)",
                                "allow_risky=False",
                                outputs,
                                recovered,
                                message="Blocked by policy: this step is classified risky/irreversible "
                                "and unattended replay does not auto-approve it. Re-run with "
                                "allow_risky=True after review, or use escalate_on_failure to route to a human.",
                            )
                        )

                    try:
                        self._execute(page, step, params, outputs)
                        if page.url.startswith("http"):
                            self.guardrails.check_navigation(page.url)
                    except GuardrailViolation as e:
                        log({"event": "guardrail_violation", "step_id": step.step_id, "error": str(e)})
                        return finish(fail(step, "resulting page within allowlist", str(e), outputs, recovered))
                    except Exception as e:  # noqa: BLE001
                        log({"event": "action_error", "step_id": step.step_id, "error": str(e)})
                        return finish(fail(step, "action to execute without error", f"{e.__class__.__name__}: {e}", outputs, recovered))

                    if step.action == ActionType.EXTRACT_TEXT:
                        log({"event": "extract", "step_id": step.step_id, "output_name": step.extract_as})

                    terminal = self._handle_outcomes_and_checkpoint(
                        page, step, params, artifact, run_id, run_dir, outputs, recovered, log, handoff, on_escalation_wait, fail
                    )
                    if terminal is not None:
                        return finish(terminal)

                if _check_checkpoint(page, artifact.success_checkpoint):
                    return finish(
                        RunResult(
                            status=ResultStatus.SUCCESS,
                            artifact_name=artifact.name,
                            artifact_version=artifact.version,
                            run_id=run_id,
                            outputs=outputs,
                            recovered_events=recovered,
                            evidence_dir=str(run_dir),
                        )
                    )
                shot = run_dir / "final_checkpoint_failure.png"
                page.screenshot(path=str(shot))
                last_step = artifact.steps[-1] if artifact.steps else None
                return finish(fail(last_step, artifact.success_checkpoint.description, _snippet(page), outputs, recovered, message="Success checkpoint not satisfied after all steps completed."))
            finally:
                browser.close()

    def _execute(self, page: Page, step: Step, params: dict, outputs: dict) -> None:
        if step.action == ActionType.NAVIGATE:
            self.guardrails.check_navigation(step.url)
            page.goto(step.url, wait_until="domcontentloaded", timeout=10000)
        elif step.action == ActionType.CLICK:
            resolve(page, step.target).click(timeout=8000)
            page.wait_for_timeout(150)
        elif step.action == ActionType.FILL:
            resolve(page, step.target).fill(_resolve_value(step, params), timeout=8000)
        elif step.action == ActionType.SELECT_OPTION:
            resolve(page, step.target).select_option(label=_resolve_value(step, params), timeout=8000)
        elif step.action == ActionType.EXTRACT_TEXT:
            value = resolve(page, step.target).inner_text(timeout=4000).strip()
            outputs[step.extract_as] = redact_text(value)
        elif step.action == ActionType.WAIT_FOR:
            page.wait_for_timeout(step.timeout_ms)
        else:
            raise ReplayError(f"Unsupported action type: {step.action}")

    def _handle_outcomes_and_checkpoint(
        self, page, step, params, artifact, run_id, run_dir, outputs, recovered, log, handoff, on_escalation_wait, fail
    ) -> RunResult | None:
        for outcome in step.known_outcomes:
            if not _check_checkpoint(page, outcome.match):
                continue
            log({"event": "known_outcome_matched", "step_id": step.step_id, "code": outcome.code, "classification": outcome.classification.value})

            if outcome.classification == OutcomeClassification.BUSINESS_OUTCOME:
                return RunResult(
                    status=ResultStatus.BUSINESS_OUTCOME,
                    artifact_name=artifact.name,
                    artifact_version=artifact.version,
                    run_id=run_id,
                    outputs=outputs,
                    business_outcome=BusinessOutcome(code=outcome.code, message=outcome.message_template, step_id=step.step_id),
                    recovered_events=recovered,
                    evidence_dir=str(run_dir),
                )

            if outcome.classification == OutcomeClassification.HARD_FAILURE:
                return fail(step, "recognized failure state", outcome.message_template, outputs, recovered, message=outcome.message_template)

            # RECOVERABLE
            recovered.append(RecoveredEvent(step_id=step.step_id, outcome_code=outcome.code, description=outcome.message_template))
            if outcome.recovery:
                try:
                    resolve(page, outcome.recovery.target).click(timeout=8000)
                    page.wait_for_timeout(150)
                except Exception as e:  # noqa: BLE001
                    log({"event": "recovery_failed", "step_id": step.step_id, "error": str(e)})
                    return fail(step, "recovery action to succeed", f"{e.__class__.__name__}: {e}", outputs, recovered)
                if outcome.recovery.then == "retry_step":
                    try:
                        self._execute(page, step, params, outputs)
                    except Exception as e:  # noqa: BLE001
                        log({"event": "action_error", "step_id": step.step_id, "phase": "retry_after_recovery", "error": str(e)})
                        return fail(step, "action to succeed after recovery", f"{e.__class__.__name__}: {e}", outputs, recovered)
            break  # re-check this step's own checkpoint below with the recovered state

        if step.checkpoint is None:
            return None
        if _check_checkpoint(page, step.checkpoint):
            return None

        if self.escalate_on_failure:
            shot = run_dir / f"escalation_step{step.step_id}.png"
            page.screenshot(path=str(shot))
            req = InterventionRequest(
                run_id=run_id,
                capability=artifact.name,
                goal=artifact.description,
                step_id=step.step_id,
                reason=f"Checkpoint not satisfied: {step.checkpoint.description}",
                current_url=page.url,
                screenshot_path=str(shot),
            )
            path = handoff.raise_intervention(req)
            log({"event": "escalate", "step_id": step.step_id, "intervention_path": str(path)})
            if on_escalation_wait:
                on_escalation_wait(handoff, req)
            try:
                record = handoff.wait_for_resume(timeout_s=300.0)
                recovered.append(RecoveredEvent(step_id=step.step_id, outcome_code="human_intervention", description=record.notes))
                if _check_checkpoint(page, step.checkpoint):
                    return None
                return fail(step, step.checkpoint.description, _snippet(page), outputs, recovered, message="Human intervened but the checkpoint still was not satisfied.")
            except TimeoutError:
                return fail(step, step.checkpoint.description, _snippet(page), outputs, recovered, message="No human operator resumed in time.")

        shot = run_dir / f"failure_step{step.step_id}.png"
        page.screenshot(path=str(shot))
        return fail(step, step.checkpoint.description, _snippet(page), outputs, recovered)
