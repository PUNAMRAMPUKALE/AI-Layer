"""Replay engine tests using hand-built artifacts (no LLM involved) against
the real mock app. This is what exercises the error taxonomy -- success,
business outcome, recoverable, hard failure, and risky-step gating -- before
spending real Anthropic API calls on the one required genuine discovery run.
"""
from pathlib import Path

import pytest

from core.artifact import (
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    CheckpointKind,
    ElementTarget,
    KnownOutcome,
    LocatorCandidate,
    LocatorStrategy,
    OutcomeClassification,
    OutputField,
    ParamType,
    Parameter,
    RecoveryAction,
    RiskLevel,
    Step,
    TargetApp,
)
from core.guardrails import Guardrails
from replay.engine import ReplayEngine
from core.result import ResultStatus

ROOT = Path(__file__).resolve().parent.parent


def _role_target(description, role, name) -> ElementTarget:
    return ElementTarget(
        description=description,
        candidates=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, role=role, name=name)],
    )


def _engine(evidence_dir, **kwargs) -> ReplayEngine:
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    return ReplayEngine(guardrails, evidence_dir, headless=True, **kwargs)


def _target_app(mock_app_url) -> TargetApp:
    return TargetApp(app_id="coreserv-member-admin", base_url=mock_app_url, entry_route="/", vendor_product="coreserv-member-admin-v1")


def _happy_path_artifact(mock_app_url) -> CapabilityArtifact:
    steps = [
        Step(step_id=1, action=ActionType.NAVIGATE, url=mock_app_url + "/",
             checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Member ID", description="Search form or notice shown"),
             known_outcomes=[
                 KnownOutcome(
                     code="system_notice_interstitial",
                     match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Scheduled maintenance", description="Notice shown"),
                     classification=OutcomeClassification.RECOVERABLE,
                     message_template="Dismissed the one-time system notice interstitial.",
                     recovery=RecoveryAction(target=_role_target('button "Continue"', "button", "Continue"), then="retry_step"),
                 )
             ]),
        Step(step_id=2, action=ActionType.FILL, target=_role_target('textbox "Member ID"', "textbox", "Member ID"), value_param="member_id"),
        Step(step_id=3, action=ActionType.CLICK, target=_role_target('button "Search"', "button", "Search"),
             checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Member Detail", description="Member detail page shown"),
             known_outcomes=[
                 KnownOutcome(
                     code="member_not_found",
                     match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="No member found matching ID", description="Search had no match"),
                     classification=OutcomeClassification.BUSINESS_OUTCOME,
                     message_template="No member found matching the given member ID.",
                 )
             ]),
        Step(step_id=4, action=ActionType.EXTRACT_TEXT, target=ElementTarget(description='Value for "Savings Balance"', candidates=[LocatorCandidate(strategy=LocatorStrategy.TABLE_ROW_VALUE, name="Savings Balance")]), extract_as="savings_balance"),
        Step(step_id=5, action=ActionType.CLICK, target=_role_target('link "Open Sub-Account"', "link", "Open Sub-Account"),
             checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Open Sub-Account", description="Open sub-account form shown"),
             known_outcomes=[
                 KnownOutcome(
                     code="session_expired",
                     match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="session has timed out", description="Session expired"),
                     classification=OutcomeClassification.HARD_FAILURE,
                     message_template="The session expired before the sub-account form could be reached.",
                 )
             ]),
        Step(step_id=6, action=ActionType.SELECT_OPTION, target=_role_target('combobox "Account Type"', "combobox", "Account Type"), value_literal="Youth Savings"),
        Step(step_id=7, action=ActionType.FILL, target=_role_target('textbox "Initial Deposit (USD)"', "textbox", "Initial Deposit (USD)"), value_param="initial_deposit"),
        Step(step_id=8, action=ActionType.CLICK, target=_role_target('button "Continue"', "button", "Continue"),
             checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Confirm Sub-Account Opening", description="Confirmation page shown"),
             known_outcomes=[
                 KnownOutcome(
                     code="member_restricted",
                     match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="compliance hold", description="Member is restricted"),
                     classification=OutcomeClassification.BUSINESS_OUTCOME,
                     message_template="This member's account is under a compliance hold.",
                 ),
                 KnownOutcome(
                     code="invalid_deposit_amount",
                     match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="is not a valid dollar amount", description="Deposit invalid"),
                     classification=OutcomeClassification.BUSINESS_OUTCOME,
                     message_template="The initial deposit amount failed validation.",
                 ),
             ]),
        Step(step_id=9, action=ActionType.CLICK, target=_role_target('button "Confirm and Open Account"', "button", "Confirm and Open Account"),
             checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Sub-Account Opened", description="Success page shown"),
             risk_level="risky"),
        Step(step_id=10, action=ActionType.EXTRACT_TEXT, target=ElementTarget(description='Value for "Reference Number"', candidates=[LocatorCandidate(strategy=LocatorStrategy.TABLE_ROW_VALUE, name="Reference Number")]), extract_as="reference_number"),
    ]
    return CapabilityArtifact(
        name="open_member_subaccount",
        description="Open a new sub-account for a member and reach the confirmation screen.",
        created_from_run_id="test",
        target=_target_app(mock_app_url),
        parameters=[
            Parameter(name="member_id", type=ParamType.STRING, required=True, description="Member ID to look up", example="10234"),
            Parameter(name="initial_deposit", type=ParamType.STRING, required=True, description="Opening deposit amount", example="150"),
        ],
        outputs=[
            OutputField(name="savings_balance", type=ParamType.STRING, description="Member's savings balance at time of run"),
            OutputField(name="reference_number", type=ParamType.STRING, description="New sub-account reference number"),
        ],
        steps=steps,
        success_checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Sub-Account Opened", description="Success page reached"),
        risk_level=RiskLevel.RISKY,
        risk_notes="Step 9 opens a new account -- irreversible through this UI.",
    )


def test_replay_success(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=True)
    result = engine.run(artifact, {"member_id": "10234", "initial_deposit": "150"})
    assert result.status == ResultStatus.SUCCESS, result
    assert result.outputs["savings_balance"] == "$4210.55"
    assert result.outputs["reference_number"].startswith("SUB-")
    assert any(e.outcome_code == "system_notice_interstitial" for e in result.recovered_events)


def test_replay_business_outcome_not_found(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=True)
    result = engine.run(artifact, {"member_id": "99999", "initial_deposit": "150"})
    assert result.status == ResultStatus.BUSINESS_OUTCOME
    assert result.business_outcome.code == "member_not_found"


def test_replay_business_outcome_restricted(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=True)
    result = engine.run(artifact, {"member_id": "30500", "initial_deposit": "150"})
    assert result.status == ResultStatus.BUSINESS_OUTCOME
    assert result.business_outcome.code == "member_restricted"


def test_replay_hard_failure_session_expired(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=True)
    result = engine.run(artifact, {"member_id": "40999", "initial_deposit": "150"})
    assert result.status == ResultStatus.FAILURE
    assert result.failure.message.startswith("The session expired")


def test_replay_blocks_risky_step_by_default(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=False)
    result = engine.run(artifact, {"member_id": "10234", "initial_deposit": "150"})
    assert result.status == ResultStatus.FAILURE
    assert result.failure.step_id == 9
    assert "Blocked by policy" in result.failure.message


def test_replay_missing_required_parameter(tmp_path, mock_app_url, reset_mock_app):
    artifact = _happy_path_artifact(mock_app_url)
    engine = _engine(tmp_path, allow_risky=True)
    result = engine.run(artifact, {"member_id": "10234"})
    assert result.status == ResultStatus.FAILURE
    assert "initial_deposit" in result.failure.expected
