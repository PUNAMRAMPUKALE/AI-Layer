"""Safety & policy guardrails: allowlist enforcement, risk classification, and
redaction. Both the discovery agent and the replay engine call into this
module for every navigation and every action -- it is the single place that
decides what's permitted, not a convention each caller has to remember.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


class GuardrailViolation(Exception):
    pass


@dataclass
class RiskDecision:
    risky: bool
    reason: str | None = None


class Guardrails:
    def __init__(self, config_path: Path):
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.allowed_domains: set[str] = set(cfg.get("allowed_domains", []))
        self.allowed_routes: list[re.Pattern] = [re.compile(p) for p in cfg.get("allowed_routes", [])]
        self.blocked_routes: list[re.Pattern] = [re.compile(p) for p in cfg.get("blocked_route_patterns", [])]
        self.allowed_action_types: set[str] = set(cfg.get("allowed_action_types", []))
        self.risky_routes: list[tuple[re.Pattern, str]] = [
            (re.compile(r["pattern"]), r["reason"]) for r in cfg.get("risky_route_patterns", [])
        ]

    def check_navigation(self, url: str) -> None:
        """Raise GuardrailViolation if `url` is outside the allowlist. Called
        before every navigate action, by both discovery and replay."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host not in self.allowed_domains:
            raise GuardrailViolation(f"Domain '{host}' is not in the allowlist ({sorted(self.allowed_domains)}).")

        path = parsed.path or "/"
        if any(p.match(path) for p in self.blocked_routes):
            raise GuardrailViolation(f"Route '{path}' is explicitly blocked by policy.")
        if not any(p.match(path) for p in self.allowed_routes):
            raise GuardrailViolation(f"Route '{path}' is not in the allowlist.")

    def check_action_type(self, action_type: str) -> None:
        if action_type not in self.allowed_action_types:
            raise GuardrailViolation(f"Action type '{action_type}' is not permitted by policy.")

    def classify_risk(self, url: str) -> RiskDecision:
        path = urlparse(url).path or "/"
        for pattern, reason in self.risky_routes:
            if pattern.match(path):
                return RiskDecision(risky=True, reason=reason)
        return RiskDecision(risky=False)


# --- Redaction -----------------------------------------------------------
#
# Applied to everything written to /evidence/ and to artifacts before they're
# saved: structured logs, extracted outputs, step values. This is regulated
# financial data (per the brief) -- redaction is not optional and is applied
# at the point of writing, not left to callers to remember.

_SECRET_KEY_NAMES = {"password", "passwd", "api_key", "apikey", "token", "secret", "credential", "ssn"}

_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "long_numeric_id": re.compile(r"\b\d{9,12}\b"),  # plausible account/routing numbers
}


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    for label, pattern in _PATTERNS.items():
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def redact_value(key: str, value):
    if isinstance(value, str) and key.lower() in _SECRET_KEY_NAMES:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    return value


def redact_dict(d: dict) -> dict:
    return {k: redact_value(k, v) for k, v in d.items()}
