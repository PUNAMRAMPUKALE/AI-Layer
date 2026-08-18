#!/usr/bin/env python
"""Adds known_outcomes (business_outcome / hard_failure branches) to a
discovered artifact, based on domain knowledge of the target app gathered
while building mock_app/ -- NOT hallucinated by the discovery LLM, which by
design only ever demonstrates the happy path once (see REPORT.md section 3
for why: a single discovery run can't observe branches it didn't take).

This stands in for what a human reviewer would do before approving an
artifact for unattended replay: annotate the known alternate outcomes for
each step, informed by knowledge of the target application. In a system
with a review/approval workflow (see the "Confidence & approval" stretch
goal in the brief), this is exactly the kind of edit that approval would
gate on.

The one thing this script deliberately does NOT annotate is the one-time
"system notice" interstitial: it's guaranteed to appear once per fresh
browser session regardless of input, so the discovery run already recorded
dismissing it as an ordinary step -- there's no input-dependent branch to
encode, and adding a redundant known_outcome would fight the literal
recorded step on replay (see REPORT.md section 3 for this specific
trade-off).

Usage:
    python -m scripts.enrich_artifact --name open_member_subaccount
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.artifact import (  # noqa: E402
    ActionType,
    ArtifactStore,
    Checkpoint,
    CheckpointKind,
    ElementTarget,
    KnownOutcome,
    LocatorCandidate,
    LocatorStrategy,
    OutcomeClassification,
    Step,
)


def _target_name(step: Step) -> str | None:
    if step.target is None or not step.target.candidates:
        return None
    for c in step.target.candidates:
        if c.strategy == LocatorStrategy.ROLE_NAME and c.name:
            return c.name
    return None


def enrich(artifact):
    click_steps = [s for s in artifact.steps if s.action == ActionType.CLICK]

    search_step = next((s for s in click_steps if _target_name(s) == "Search"), None)
    if search_step:
        search_step.known_outcomes.append(
            KnownOutcome(
                code="member_not_found",
                match=Checkpoint(
                    kind=CheckpointKind.TEXT_PRESENT,
                    value="No member found matching ID",
                    description="Search returned no match",
                ),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
                message_template="No member found matching the given member ID.",
            )
        )

    open_subaccount_step = next((s for s in click_steps if _target_name(s) == "Open Sub-Account"), None)
    if open_subaccount_step:
        open_subaccount_step.known_outcomes.append(
            KnownOutcome(
                code="session_expired",
                match=Checkpoint(
                    kind=CheckpointKind.TEXT_PRESENT,
                    value="session has timed out",
                    description="Session expired before the sub-account form loaded",
                ),
                classification=OutcomeClassification.HARD_FAILURE,
                message_template="The session expired before the sub-account form could be reached.",
            )
        )

    # Two steps in this flow are both labeled "Continue" (dismiss the one-time
    # notice, and submit the sub-account form). The form-submit one is the
    # later occurrence -- see module docstring for why the notice-dismiss
    # click isn't annotated at all.
    continue_steps = [s for s in click_steps if _target_name(s) == "Continue"]
    if continue_steps:
        submit_step = continue_steps[-1]
        submit_step.known_outcomes.append(
            KnownOutcome(
                code="member_restricted",
                match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="compliance hold", description="Member is under a compliance hold"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
                message_template="This member's account is under a compliance hold; sub-accounts cannot be opened.",
            )
        )
        submit_step.known_outcomes.append(
            KnownOutcome(
                code="invalid_deposit_amount",
                match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="is not a valid dollar amount", description="Deposit amount failed validation"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
                message_template="The initial deposit amount failed validation.",
            )
        )
        submit_step.known_outcomes.append(
            KnownOutcome(
                code="deposit_out_of_range",
                match=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="cannot exceed $10,000.00", description="Deposit amount exceeds the allowed range"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
                message_template="The initial deposit amount exceeds the allowed range without a supervisor override.",
            )
        )

    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", type=int, default=None)
    args = parser.parse_args()

    store = ArtifactStore(ROOT / "artifacts")
    artifact = store.load(args.name, args.version)
    original_version = artifact.version

    artifact = enrich(artifact)
    artifact.version = original_version + 1
    path = store.save(artifact)
    print(f"Enriched {args.name} v{original_version} -> v{artifact.version}: {path}")
    for step in artifact.steps:
        if step.known_outcomes:
            codes = ", ".join(o.code for o in step.known_outcomes)
            print(f"  step {step.step_id} ({step.action.value}): {codes}")


if __name__ == "__main__":
    main()
