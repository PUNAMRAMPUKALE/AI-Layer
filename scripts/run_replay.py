#!/usr/bin/env python
"""CLI entry point: deterministic replay of a saved capability artifact. No
LLM call -- this is the production execution path an AI agent would trigger.

Example:
    python -m scripts.run_replay --capability open_member_subaccount \\
        --param member_id=10234 --param initial_deposit=150 --allow-risky
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.artifact import ArtifactStore  # noqa: E402
from core.guardrails import Guardrails  # noqa: E402
from replay.engine import ReplayEngine  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--version", type=int, default=None, help="Defaults to the latest saved version.")
    parser.add_argument("--param", action="append", default=[], help="name=value, may repeat")
    parser.add_argument("--allow-risky", action="store_true", help="Permit steps classified risky/irreversible.")
    parser.add_argument("--escalate-on-failure", action="store_true", help="Route unrecognized checkpoint failures to a human instead of failing immediately.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-reset", action="store_true", help="Don't reset mock app state before replay.")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()

    params: dict[str, str] = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        params[k] = v

    store = ArtifactStore(ROOT / "artifacts")
    artifact = store.load(args.capability, args.version)

    if not args.no_reset:
        base = args.base_url or artifact.target.base_url
        requests.post(f"{base}/__reset", timeout=5)

    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    engine = ReplayEngine(
        guardrails,
        ROOT / "evidence",
        allow_risky=args.allow_risky,
        escalate_on_failure=args.escalate_on_failure,
        headless=not args.headed,
    )

    result = engine.run(artifact, params)

    print(f"run_id: {result.run_id}")
    print(f"artifact: {artifact.name} v{artifact.version}")
    print(f"params: {params}")
    print(f"status: {result.status.value}")
    if result.outputs:
        print(f"outputs: {json.dumps(result.outputs, indent=2)}")
    if result.business_outcome:
        print(f"business_outcome: {result.business_outcome.code} -- {result.business_outcome.message}")
    if result.failure:
        print(f"failure at step {result.failure.step_id}: {result.failure.message}")
        print(f"  expected: {result.failure.expected}")
        print(f"  observed: {result.failure.observed}")
    if result.recovered_events:
        for ev in result.recovered_events:
            print(f"recovered: step {ev.step_id} [{ev.outcome_code}] {ev.description}")
    print(f"evidence: {result.evidence_dir}")

    if result.evidence_dir:
        Path(result.evidence_dir, "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")

    sys.exit(0 if result.status.value in ("success", "business_outcome") else 1)


if __name__ == "__main__":
    main()
