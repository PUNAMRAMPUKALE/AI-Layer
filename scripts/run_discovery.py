#!/usr/bin/env python
"""CLI entry point: run one genuine LLM-driven discovery run against the
mock bank app and save the resulting capability artifact.

Example:
    python -m scripts.run_discovery \\
        --goal "Open a new sub-account for member 10234 and reach the confirmation screen" \\
        --capability-name open_member_subaccount --headed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.llm_client import LLMClient  # noqa: E402
from agent.loop import RunConfig, run_discovery  # noqa: E402
from core.guardrails import Guardrails  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--capability-name", required=True)
    parser.add_argument("--base-url", default=f"http://localhost:{os.environ.get('MOCK_APP_PORT', '5055')}")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless.")
    parser.add_argument("--no-reset", action="store_true", help="Don't reset mock app state before the run.")
    args = parser.parse_args()

    if not args.no_reset:
        requests.post(f"{args.base_url}/__reset", timeout=5)

    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    llm = LLMClient()

    config = RunConfig(
        goal=args.goal,
        entry_url=args.base_url + "/",
        capability_name=args.capability_name,
        app_id="coreserv-member-admin",
        vendor_product="coreserv-member-admin-v1",
        base_url=args.base_url,
        max_steps=args.max_steps,
        evidence_root=ROOT / "evidence",
        artifact_root=ROOT / "artifacts",
        headless=not args.headed,
    )

    result = run_discovery(config, guardrails, llm)

    print(f"run_id: {result.run_id}")
    print(f"success: {result.success}")
    print(f"summary: {result.summary}")
    print(f"steps_taken: {result.steps_taken}")
    print(f"log: {result.log_path}")
    if result.artifact_path:
        print(f"artifact saved: {result.artifact_path}")

    summary_path = result.log_path.parent / "result.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_id": result.run_id,
                "success": result.success,
                "summary": result.summary,
                "steps_taken": result.steps_taken,
                "artifact_path": str(result.artifact_path) if result.artifact_path else None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
