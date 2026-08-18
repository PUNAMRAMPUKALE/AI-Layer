"""Executes a single agent-chosen action against the live page, going through
guardrails on every navigation/action, and returns a (mostly-filled-in)
core.artifact.Step -- the same Step type the replay engine consumes. This is
the seam between "how we perceive/act on a surface" and "the recorded flow"
described in the brief (section 3.7): swap this module for a
desktop-automation backend and agent/loop.py + the artifact schema don't
need to change.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

from agent.perception import perceive
from core.artifact import ActionType, Checkpoint, CheckpointKind, Step
from core.guardrails import Guardrails
from core.locators import build_extract_target, build_target, resolve


class BrowserSession:
    def __init__(self, page: Page, guardrails: Guardrails):
        self.page = page
        self.guardrails = guardrails

    def perceive(self) -> str:
        return perceive(self.page)

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))
        return path

    def _risk_level(self, url: str) -> str:
        return "risky" if self.guardrails.classify_risk(url).risky else "safe"

    def _checkpoint_after(self, expect_text: str | None) -> Checkpoint:
        if expect_text:
            try:
                found = expect_text in self.page.inner_text("body")
            except Exception:  # noqa: BLE001
                found = False
            if found:
                return Checkpoint(
                    kind=CheckpointKind.TEXT_PRESENT,
                    value=expect_text,
                    description=f'Page contains "{expect_text}"',
                )
        return Checkpoint(
            kind=CheckpointKind.URL_CONTAINS,
            value=self.page.url,
            description=f"URL is {self.page.url}",
        )

    def navigate(self, url: str, expect_text: str | None, reasoning: str) -> Step:
        self.guardrails.check_action_type("navigate")
        self.guardrails.check_navigation(url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=10000)
        self.guardrails.check_navigation(self.page.url)
        return Step(
            step_id=0,
            action=ActionType.NAVIGATE,
            url=url,
            checkpoint=self._checkpoint_after(expect_text),
            risk_level=self._risk_level(self.page.url),
            reasoning=reasoning,
        )

    def click(self, role: str, name: str, expect_text: str | None, reasoning: str) -> Step:
        self.guardrails.check_action_type("click")
        target = build_target(self.page, description=f'{role} "{name}"', role=role, name=name)
        loc = resolve(self.page, target)
        loc.click(timeout=8000)
        self.page.wait_for_timeout(150)
        self.guardrails.check_navigation(self.page.url)
        return Step(
            step_id=0,
            action=ActionType.CLICK,
            target=target,
            checkpoint=self._checkpoint_after(expect_text),
            risk_level=self._risk_level(self.page.url),
            reasoning=reasoning,
        )

    def fill(self, role: str, name: str, text: str, parameter_name: str | None, reasoning: str) -> Step:
        self.guardrails.check_action_type("fill")
        target = build_target(self.page, description=f'{role} "{name}"', role=role, name=name, label_text=name)
        loc = resolve(self.page, target)
        loc.fill(text, timeout=8000)
        return Step(
            step_id=0,
            action=ActionType.FILL,
            target=target,
            value_param=parameter_name or None,
            value_literal=None if parameter_name else text,
            reasoning=reasoning,
        )

    def select_option(
        self, role: str, name: str, option: str, parameter_name: str | None, expect_text: str | None, reasoning: str
    ) -> Step:
        self.guardrails.check_action_type("select_option")
        target = build_target(self.page, description=f'{role} "{name}"', role=role, name=name, label_text=name)
        loc = resolve(self.page, target)
        loc.select_option(label=option, timeout=8000)
        return Step(
            step_id=0,
            action=ActionType.SELECT_OPTION,
            target=target,
            value_param=parameter_name or None,
            value_literal=None if parameter_name else option,
            checkpoint=self._checkpoint_after(expect_text) if expect_text else None,
            reasoning=reasoning,
        )

    def extract_text(self, label: str, output_name: str, reasoning: str) -> tuple[Step, str]:
        self.guardrails.check_action_type("extract_text")
        target = build_extract_target(self.page, description=f'Value for "{label}"', label_text=label)
        loc = resolve(self.page, target)
        value = loc.inner_text(timeout=4000).strip()
        step = Step(
            step_id=0,
            action=ActionType.EXTRACT_TEXT,
            target=target,
            extract_as=output_name,
            reasoning=reasoning,
        )
        return step, value
