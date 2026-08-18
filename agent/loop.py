"""The discovery agent: an LLM-driven observe -> decide -> act loop against a
live browser session. On success, emits a CapabilityArtifact. Every turn is
logged to /evidence/<run_id>/log.jsonl (redacted) -- this is the evidence
the brief requires for the genuine LLM-driven run.

Stopping conditions (brief section 3.1): goal met (`finish`), max steps
exhausted, or the model calls `escalate` and no human resumes in time.
Reaching max steps without finishing is itself treated as a "stuck" signal
and routed through the same handoff mechanism as an explicit escalate call
(brief 3.6: "the agent is stuck during discovery" is a first-class escalation
trigger, not just a tool the model may or may not use).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.browser import BrowserSession
from agent.llm_client import LLMClient
from agent.tools import TOOLS
from core.artifact import (
    ArtifactStore,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    OutputField,
    ParamType,
    Parameter,
    RiskLevel,
    Step,
    TargetApp,
)
from core.guardrails import Guardrails, redact_dict, redact_text
from escalation.handoff import HandoffController, InterventionRequest

SYSTEM_PROMPT_TEMPLATE = """You are operating a real internal back-office web application on behalf \
of a bank/credit union staff member, entirely through the tools provided. You cannot see pixels; \
each turn you're given the accessible role/name of every interactive control plus the visible page \
text, after your previous action.

Goal: {goal}

Ground rules:
- Only interact with elements listed under "Interactive elements" -- refer to them by their exact \
role and name as shown.
- Prefer clicking real links/buttons over calling `navigate` directly; `navigate` is only for the \
entry point.
- Read the visible page text before acting -- it may already tell you the goal is unreachable (e.g. \
"no member found") or blocked (e.g. a compliance hold). That is useful information, not a reason to \
keep clicking around.
- When a value should vary each time this capability is invoked in the future (like a member ID), \
pass it as a named `parameter_name` instead of hardcoding it as one-off text.
- Use `extract` to capture any value the goal asks you to read back.
- Call `finish` only once the goal is fully and verifiably achieved, citing the exact text on the \
page that proves it.
- If you are stuck (unrecognized state, blocked action, repeated failures), call `escalate` rather \
than guessing or retrying forever.
- Take exactly one tool call per turn.
"""


@dataclass
class RunConfig:
    goal: str
    entry_url: str
    capability_name: str
    app_id: str
    vendor_product: str
    base_url: str
    max_steps: int = 20
    evidence_root: Path = Path("evidence")
    artifact_root: Path = Path("artifacts")
    headless: bool = True
    escalation_timeout_s: float = 300.0


@dataclass
class DiscoveryResult:
    success: bool
    run_id: str
    log_path: Path
    summary: str
    artifact: CapabilityArtifact | None = None
    artifact_path: Path | None = None
    steps_taken: int = 0


class EvidenceLog:
    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "a", encoding="utf-8")

    def write(self, event: dict) -> None:
        record = {"ts": time.time(), **event}
        self._f.write(json.dumps(redact_dict(record), default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def _perception_message(text: str) -> dict:
    return {"role": "user", "content": text}


def run_discovery(
    config: RunConfig,
    guardrails: Guardrails,
    llm: LLMClient,
    on_escalation_wait=None,
) -> DiscoveryResult:
    """`on_escalation_wait(handoff, request)` is called (if provided) right after
    an intervention is raised and before we block on it -- scripts/run_discovery.py
    uses this hook to start the operator server and, for reproducible evidence,
    to trigger a scripted resume. In a real attended deployment, a human would
    just open the operator URL themselves; this hook is not required."""
    run_id = f"discovery-{uuid.uuid4().hex[:8]}"
    run_dir = config.evidence_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = EvidenceLog(run_dir / "log.jsonl")
    log.write({"event": "run_start", "goal": config.goal, "entry_url": config.entry_url, "run_id": run_id})

    steps: list[Step] = []
    parameters: dict[str, Parameter] = {}
    outputs: dict[str, OutputField] = {}
    step_counter = 0

    def next_step_id() -> int:
        nonlocal step_counter
        step_counter += 1
        return step_counter

    finished = False
    success = False
    summary = ""
    success_checkpoint: Checkpoint | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless)
        page = browser.new_page()
        session = BrowserSession(page, guardrails)
        handoff = HandoffController(run_dir, run_id)

        try:
            first_step = session.navigate(config.entry_url, expect_text=None, reasoning="Entry point")
            first_step.step_id = next_step_id()
            steps.append(first_step)
            log.write({"event": "action", "tool": "navigate", "url": config.entry_url, "step_id": first_step.step_id})

            system = SYSTEM_PROMPT_TEMPLATE.format(goal=config.goal)
            messages = [_perception_message(f"Starting state:\n\n{session.perceive()}")]

            for turn in range(config.max_steps):
                response = llm.step(system, messages, TOOLS)
                messages.append({"role": "assistant", "content": response.content})
                log.write(
                    {
                        "event": "llm_response",
                        "turn": turn,
                        "text": "".join(b.text for b in response.content if b.type == "text"),
                        "stop_reason": response.stop_reason,
                    }
                )

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    messages.append(_perception_message("Please call exactly one tool to act."))
                    continue

                result_blocks = []
                executed = False
                should_break = False

                for block in tool_use_blocks:
                    if executed:
                        result_blocks.append(
                            {"type": "tool_result", "tool_use_id": block.id, "content": "Skipped: one action per turn."}
                        )
                        continue
                    executed = True
                    log.write({"event": "llm_action", "turn": turn, "tool": block.name, "input": block.input})
                    outcome_text, step, terminal = _execute_tool(
                        block, session, guardrails, handoff, config, run_id, run_dir, step_counter, log
                    )
                    if step is not None:
                        step.step_id = next_step_id()
                        steps.append(step)
                        if block.name == "fill" and block.input.get("parameter_name"):
                            pname = block.input["parameter_name"]
                            parameters[pname] = Parameter(
                                name=pname,
                                type=ParamType.STRING,
                                required=True,
                                description=f"Value for {block.input['name']}",
                                example=block.input["text"],
                            )
                        if block.name == "select_option" and block.input.get("parameter_name"):
                            pname = block.input["parameter_name"]
                            parameters[pname] = Parameter(
                                name=pname,
                                type=ParamType.STRING,
                                required=True,
                                description=f"Value for {block.input['name']}",
                                example=block.input["option"],
                            )
                        if block.name == "extract":
                            outputs[block.input["output_name"]] = OutputField(
                                name=block.input["output_name"],
                                type=ParamType(block.input.get("output_type", "string")),
                                description=f"Extracted from \"{block.input['label']}\"",
                            )
                    if block.name == "finish":
                        finished = True
                        success = bool(block.input.get("success"))
                        summary = block.input.get("summary", "")
                        checkpoint_text = block.input.get("checkpoint_text", "")
                        success_checkpoint = Checkpoint(
                            kind=CheckpointKind.TEXT_PRESENT,
                            value=checkpoint_text,
                            description=f'Page contains "{checkpoint_text}"',
                        )
                        terminal = True
                    result_blocks.append({"type": "tool_result", "tool_use_id": block.id, "content": outcome_text})
                    if terminal:
                        should_break = True

                messages.append({"role": "user", "content": result_blocks})
                if should_break:
                    break
            else:
                # Loop exhausted max_steps without finishing -- treat as "stuck", per 3.6.
                log.write({"event": "max_steps_exhausted", "steps_taken": step_counter})
                shot = session.screenshot(run_dir / f"stuck_step{step_counter}.png")
                req = InterventionRequest(
                    run_id=run_id,
                    capability=config.capability_name,
                    goal=config.goal,
                    step_id=step_counter,
                    reason=f"Reached max_steps ({config.max_steps}) without completing the goal.",
                    current_url=page.url,
                    screenshot_path=str(shot),
                )
                path = handoff.raise_intervention(req)
                log.write({"event": "escalate", "reason": req.reason, "intervention_path": str(path)})
                if on_escalation_wait:
                    on_escalation_wait(handoff, req)
                try:
                    handoff.wait_for_resume(timeout_s=config.escalation_timeout_s)
                    summary = "Resumed by human after max_steps, but run was not re-driven to completion."
                except TimeoutError:
                    summary = "Stuck: reached max_steps and no human operator resumed in time."
                success = False
                finished = True
        finally:
            browser.close()

    log.write({"event": "run_end", "success": success, "summary": summary, "steps_taken": step_counter})
    log.close()

    result = DiscoveryResult(success=success, run_id=run_id, log_path=run_dir / "log.jsonl", summary=summary, steps_taken=step_counter)

    if success and success_checkpoint is not None:
        overall_risk = RiskLevel.RISKY if any(s.risk_level == "risky" for s in steps) else RiskLevel.SAFE
        risky_steps = [s.step_id for s in steps if s.risk_level == "risky"]
        artifact = CapabilityArtifact(
            name=config.capability_name,
            description=config.goal,
            created_from_run_id=run_id,
            target=TargetApp(
                app_id=config.app_id, base_url=config.base_url, entry_route="/", vendor_product=config.vendor_product
            ),
            parameters=list(parameters.values()),
            outputs=list(outputs.values()),
            steps=steps,
            success_checkpoint=success_checkpoint,
            risk_level=overall_risk,
            risk_notes=(f"Steps {risky_steps} hit a route classified risky by policy." if risky_steps else "No irreversible actions."),
        )
        store = ArtifactStore(config.artifact_root)
        path = store.save(artifact)
        result.artifact = artifact
        result.artifact_path = path

    return result


def _execute_tool(block, session, guardrails, handoff, config, run_id, run_dir, step_counter, log):
    """Returns (outcome_text_for_llm, Step|None, terminal: bool)."""
    try:
        if block.name == "click":
            step = session.click(block.input["role"], block.input["name"], block.input.get("expect_text"), block.input.get("reasoning", ""))
            return f"OK.\n\n{session.perceive()}", step, False
        if block.name == "fill":
            step = session.fill(block.input["role"], block.input["name"], block.input["text"], block.input.get("parameter_name"), block.input.get("reasoning", ""))
            return f"OK.\n\n{session.perceive()}", step, False
        if block.name == "select_option":
            step = session.select_option(
                block.input["role"], block.input["name"], block.input["option"], block.input.get("parameter_name"), block.input.get("expect_text"), block.input.get("reasoning", "")
            )
            return f"OK.\n\n{session.perceive()}", step, False
        if block.name == "navigate":
            step = session.navigate(block.input["url"], block.input.get("expect_text"), block.input.get("reasoning", ""))
            return f"OK.\n\n{session.perceive()}", step, False
        if block.name == "extract":
            step, value = session.extract_text(block.input["label"], block.input["output_name"], block.input.get("reasoning", ""))
            return f"Extracted {block.input['output_name']} = {redact_text(value)!r}\n\n{session.perceive()}", step, False
        if block.name == "finish":
            return "Run finished.", None, True
        if block.name == "escalate":
            reason = block.input.get("reason", "")
            shot = session.screenshot(run_dir / f"escalation_step{step_counter}.png")
            req = InterventionRequest(
                run_id=run_id,
                capability=config.capability_name,
                goal=config.goal,
                step_id=step_counter,
                reason=reason,
                current_url=session.page.url,
                screenshot_path=str(shot),
            )
            path = handoff.raise_intervention(req)
            log.write({"event": "escalate", "reason": reason, "intervention_path": str(path)})
            record = handoff.wait_for_resume(timeout_s=config.escalation_timeout_s)
            return f"A human operator resumed after: {record.notes!r}. Re-observe and continue.\n\n{session.perceive()}", None, False
        return f"Unknown tool {block.name}", None, False
    except TimeoutError as e:
        return f"No human operator resumed in time: {e}", None, True
    except Exception as e:  # noqa: BLE001 -- surfaced to the model so it can adapt or escalate
        log.write({"event": "action_error", "tool": block.name, "error": str(e)})
        try:
            perception = session.perceive()
        except Exception:  # noqa: BLE001
            perception = "(could not read page state)"
        return f"Action failed: {e.__class__.__name__}: {e}\n\n{perception}", None, False
