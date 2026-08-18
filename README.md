# Computer-Use Automation System

An LLM-driven agent discovers how to complete a goal in a real (mocked)
legacy back-office banking UI, records what it did as a typed, versioned
**capability artifact**, and that artifact then **replays deterministically**
-- no model in the loop -- with structured error handling and a real
human-escalation/handoff path for when it can't proceed safely.

See [`REPORT.md`](REPORT.md) for the design write-up (architecture, artifact
schema, determinism & error handling, heterogeneity/multi-tenant story,
escalation & handoff, safety, and cuts).

## What's here

```
mock_app/       "legacy" target app the agent operates: nested tables, no test IDs
core/           artifact schema, locator resolution, guardrails, result contract
agent/          the LLM-driven discovery loop (observe -> decide -> act)
replay/         deterministic replay engine (no LLM)
escalation/     human-in-the-loop handoff (pause / take control / resume)
scripts/        CLIs: run_discovery, run_replay, enrich_artifact
config/         allowlist.yaml -- the guardrail policy
artifacts/      saved capability artifacts (JSON, versioned)
evidence/       logs + screenshots from real discovery and replay runs
tests/          pytest suite exercising the mechanics without spending LLM calls
```

## Setup

Requires Python 3.11+ and a real Anthropic API key (the discovery agent
makes genuine LLM calls -- there's no offline/mocked mode for that part,
by design; see the brief this project answers).

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY
```

Start the mock target app in one terminal:

```bash
python run_mock_app.py
# serves http://localhost:5055
```

## Demo path

With the mock app running, in another terminal:

**1. Discovery** -- one real LLM-driven run that opens a sub-account and
records it as a capability:

```bash
python -m scripts.run_discovery \
  --goal "Open a new sub-account for member 10234 and reach the confirmation screen" \
  --capability-name open_member_subaccount \
  --headed
```

This saves `artifacts/open_member_subaccount/v1.json` and evidence
(structured log + screenshots) under `evidence/discovery-<id>/`.

**2. Enrich with known error branches** -- adds business-outcome/hard-failure
annotations based on the target app's known error states (not-found,
restricted, session-expired, validation) -- see `REPORT.md` section 3 for why
this is a separate, explicit step rather than something the discovery LLM
invents:

```bash
python -m scripts.enrich_artifact --name open_member_subaccount
```

**3. Replay** -- deterministic, no LLM, using the enriched v2 artifact:

```bash
# Happy path
python -m scripts.run_replay --capability open_member_subaccount \
  --param member_id=10234 --param initial_deposit=150 --allow-risky

# Business outcome (not a crash): unknown member
python -m scripts.run_replay --capability open_member_subaccount \
  --param member_id=99999 --param initial_deposit=150 --allow-risky

# Business outcome: compliance-restricted member
python -m scripts.run_replay --capability open_member_subaccount \
  --param member_id=30500 --param initial_deposit=150 --allow-risky

# Hard failure: session expires mid-flow
python -m scripts.run_replay --capability open_member_subaccount \
  --param member_id=40999 --param initial_deposit=150 --allow-risky

# Guardrail: risky (irreversible) step blocked by default without --allow-risky
python -m scripts.run_replay --capability open_member_subaccount \
  --param member_id=10234 --param initial_deposit=150
```

Each replay prints a structured result (`status`, `outputs`,
`business_outcome`, or `failure` with step/expected/observed) and writes
logs + screenshots to `evidence/replay-<id>/`.

## Running without live services

`pytest` exercises the browser mechanics, locator resolution, guardrails,
replay error taxonomy (success / business outcome / recoverable / hard
failure), and the escalation handoff -- all against the real mock app and a
real (headless) browser, but with **no Anthropic API calls**, so the whole
suite runs offline once dependencies are installed:

```bash
pytest tests/ -v
```

## Human escalation demo

The discovery loop and the replay engine (`--escalate-on-failure`) both
route through `escalation/handoff.py`: automation pauses, an intervention
request with full context is written to `evidence/`, a minimal local
operator console comes up at `http://localhost:5056`, and resuming there
unblocks the same live (headed) browser session. `tests/test_escalation.py`
exercises this end-to-end over real HTTP without a human present, for
repeatable evidence -- see `REPORT.md` section 5 for the design and what's
intentionally mocked.

## Guardrails

`config/allowlist.yaml` is the single policy source both discovery and
replay enforce: allowed domains/routes/action types, and which routes are
classified risky (irreversible) -- risky steps are blocked by default during
unattended replay and require `--allow-risky` after review. See
`REPORT.md` section 6.
