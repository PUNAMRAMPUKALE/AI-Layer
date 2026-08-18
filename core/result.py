"""Structured result contract returned by replay (and reused by the discovery
loop's final report). This is the thing a calling AI agent actually reads --
it has to distinguish three fundamentally different situations, per the
brief: a legitimate business outcome is not a crash, a recovered hiccup is
not a crash, and a hard failure needs enough detail to debug.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ResultStatus(str, Enum):
    SUCCESS = "success"                    # happy path completed, checkpoint verified, outputs returned
    BUSINESS_OUTCOME = "business_outcome"  # expected non-happy-path result (e.g. "member not found")
    FAILURE = "failure"                    # hard failure -- stopped, needs a human/debugging
    ESCALATED = "escalated"                # handed off to a human mid-run; see core/escalation.py


class RecoveredEvent(BaseModel):
    step_id: int
    outcome_code: str
    description: str


class BusinessOutcome(BaseModel):
    code: str
    message: str
    step_id: int


class FailureDetail(BaseModel):
    step_id: int
    expected: str
    observed: str
    message: str


class RunResult(BaseModel):
    status: ResultStatus
    artifact_name: str
    artifact_version: int
    run_id: str
    outputs: dict = Field(default_factory=dict)
    business_outcome: BusinessOutcome | None = None
    failure: FailureDetail | None = None
    recovered_events: list[RecoveredEvent] = Field(default_factory=list)
    evidence_dir: str | None = None
