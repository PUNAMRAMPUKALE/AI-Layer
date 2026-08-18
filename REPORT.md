# Design Report: Computer-Use Automation System

## 1. Architecture

Single process, five modules, one shared vocabulary:

- **`mock_app/`** -- the target. A deliberately legacy-styled Flask app
  (nested `<table>` layout, no test IDs, `bgcolor` attributes) standing in
  for the "core banking screens, servicing tools, admin consoles" described
  in the brief. It has a real multi-step flow (member search -> detail ->
  open sub-account -> confirm), a one-time interstitial, and four genuine
  runtime conditions: not-found, a compliance-restricted member, an
  expired-session route, and input validation. These aren't decorative --
  they're what make the error-taxonomy requirement (3.3) testable at all.
- **`core/`** -- the shared contract: the artifact schema, the locator
  resolver, the guardrail policy, and the result contract. Both
  `agent/` and `replay/` depend on `core/`; neither depends on the other.
  That's deliberate -- it's the seam described in section 3.7. Swap
  `agent/browser.py` for a desktop-automation backend and nothing in
  `core/` or `replay/` has to change, because both already only talk in
  terms of `ElementTarget` and `Step`, not Playwright objects.
- **`agent/`** -- the discovery loop. Perceives the page as an
  accessibility-role/name listing (not a screenshot+coordinates, not raw
  HTML), gives Claude a small tool set (click/fill/select_option/navigate/
  extract/finish/escalate), and turns the resulting transcript into a
  `CapabilityArtifact`.
- **`replay/`** -- the production path. No LLM. Walks the artifact's steps,
  resolves each `ElementTarget` through the same fallback chain discovery
  used, checks `known_outcomes` before the step's own checkpoint, and
  returns a `RunResult` with one of four statuses.
- **`escalation/`** -- pause/resume plus a minimal operator console, shared
  by both the discovery loop and replay.

**Why single-process, synchronous, one runner per invocation** (vs. a
service + queue): the brief explicitly discourages building scaling
infrastructure prematurely, and a queued/service architecture would add
nothing to how the core loop, artifact schema, or replay engine work --
it's a deployment concern, not a design one. `scripts/run_discovery.py` and
`scripts/run_replay.py` are the two entry points a real system would wrap in
an API/worker; the interesting logic doesn't change either way.

**Why Playwright + accessibility tree, not `computer-use` coordinate
clicking:** the brief explicitly allows both, and biases toward "an approach
that would still work when the surface has no clean DOM." Accessibility
role+name *is* that approach -- it's the representation a screen reader
uses, it's available on desktop apps too (see section 4), and unlike raw
coordinates it survives a resize, a different font, or a slightly
reflowed legacy table. It does assume the target remains usable by a human
via assistive tech, which is a safe assumption for internal staff tooling
that real employees operate today.

## 2. Artifact schema

The schema (`core/artifact.py`) is built around three ideas:

**A step's target is never one selector.** `ElementTarget.candidates` is an
ordered list: accessibility role+name first, then `<label>` association for
form controls, then a structural CSS path computed at recording time as a
last resort. Every candidate carries `robustness_notes` explaining why it's
there and how fragile it is -- the schema doesn't hide the trade-off, it
records it. `core/locators.py` resolves candidates in order and, on total
failure, reports every attempt it made, so a replay failure is debuggable
instead of a bare "element not found."

**Steps encode known alternate outcomes, not just the happy path.**
`Step.known_outcomes` is a list of `KnownOutcome`, each pairing a
`Checkpoint` (how to recognize the state) with a `classification`
(`business_outcome` / `recoverable` / `hard_failure`), a message, and --
for `recoverable` -- a `RecoveryAction`. This is what turns "no such
member" from a special case bolted onto replay into a first-class part of
the contract a reviewer (or a calling agent) can read directly off the
artifact.

**Parameters and outputs are declared once, at the artifact level.** A
calling agent gets a contract -- name, type, required, example -- without
reading the step list. `risk_level` and `risk_notes` are similarly
artifact-level, computed from whether any step's resulting route was
classified risky by policy.

Schema is versioned (`version`, `schema_version`) and stored one JSON file
per version (`ArtifactStore`), so an artifact is diffable and reviewable,
not a black box -- and enrichment (adding `known_outcomes` after the fact,
see section 3) produces a new version rather than mutating history.

## 3. Determinism & error handling

Replay resolves every target through the same fallback chain discovery
built, so "replay broke" is signal about the app or the recording, not
about two different implementations of "click the button" drifting apart.

The error taxonomy is enforced by ordering: after every step,
`known_outcomes` are checked *before* the step's own checkpoint.
`business_outcome` stops the run and returns that status (not a crash);
`recoverable` runs the recorded recovery action and retries; `hard_failure`
stops with a structured `FailureDetail` (step, expected, observed,
message); anything matching neither, that also fails the step's own
checkpoint, is an *unrecognized* state -- a hard failure by default, or
routed to a human if `escalate_on_failure` is set.

**Where `known_outcomes` come from, honestly:** a single discovery run can
only ever demonstrate the path it actually took. It cannot observe branches
it didn't take. So `scripts/enrich_artifact.py` adds the known error
branches (not-found, restricted, session-expired, validation) as a
separate, explicit step after discovery, based on knowledge of the target
app -- standing in for what a human reviewer would annotate before
approving an artifact for unattended replay (the "Confidence & approval"
stretch goal would formalize exactly this gate). The alternative --
letting the discovery LLM guess at error branches it never saw -- would be
fabrication dressed up as observation, so I didn't do that. One outcome I
deliberately did *not* enrich: the one-time "system notice" interstitial is
guaranteed to appear once per fresh session regardless of input, so
discovery already records dismissing it as an ordinary step; there's no
input-dependent branch to encode, and I have a test
(`test_replay_engine.py::test_replay_success`) proving the `recoverable`
mechanism itself works correctly against that exact page, independent of
whether the production artifact happens to use it.

**Checkpoints** come from two sources: the model supplies `expect_text`
after actions it expects to change page state (becomes a `TEXT_PRESENT`
checkpoint), falling back to `URL_CONTAINS` if that text isn't found or
wasn't given. The final `finish` tool call's `checkpoint_text` becomes the
artifact's `success_checkpoint`.

**Secondary to all this: UI drift.** Because the brief's environment has
stable UIs, drift matters less here than runtime conditions -- but the same
fallback-candidate mechanism that survives a legacy DOM also buys some
resilience to small structural drift (an added wrapper `<div>`, a
reordered attribute) without any special-casing.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is exactly `agent/browser.py` /
`replay/engine.py`'s use of `core/locators.py`: everything above that line
speaks in `Step` and `ElementTarget`, never Playwright. A legacy web app
(framesets, nested tables) already works today via `ElementTarget.frame_path`
and the CSS-fallback tier -- both exist for exactly that case. A desktop
app would mean swapping the *perceive* and *act* implementations
(accessibility API instead of Playwright's `accessibility.snapshot()`,
OS-level click/type instead of Playwright locators) while keeping
`core/artifact.py`, `core/locators.py`'s candidate-ordering *concept*, and
all of `replay/`'s error-taxonomy logic unchanged. `LocatorStrategy` would
gain a desktop-native variant; it wouldn't need a new engine.

**Multi-tenant reuse.** `TargetApp.vendor_product` is deliberately separate
from `base_url`: two tenants running the same underlying vendor product
(branded and hosted differently) share a `vendor_product` id even though
their `base_url`/`app_id` differ. An artifact recorded against one tenant's
instance is keyed for reuse by `vendor_product`, with `base_url` supplied
per invocation like any other environment parameter. What I did **not**
build (per the brief's explicit "design, not necessarily build" scope for
3.7): per-tenant override files that patch specific `LocatorCandidate`s or
route patterns when a tenant's configuration diverges slightly, and a drift
detector that would flag when a tenant's actual page stops matching an
artifact's primary candidates (the resolver already surfaces exactly that
signal per-attempt -- collecting it across runs into a per-tenant health
score is the natural next step, close to the "Confidence & approval" stretch
goal). Canonicalizing concrete values into route patterns (`/members/12345`
-> `/members/:id`) is straightforward given `ElementTarget` doesn't
currently encode route templates directly, but a target's `role_name`
candidates are already tenant-branding-agnostic (accessible names describe
the control, not the tenant), which is most of what makes cross-tenant
reuse plausible in the first place.

## 5. Escalation & handoff

**Detecting "stuck."** Three triggers, all routed through the same
`HandoffController`: the model explicitly calls `escalate` during
discovery; discovery's harness detects `max_steps` reached without
`finish` (the model kept trying but made no verifiable progress) --
`agent/loop.py`; and replay hitting a checkpoint that neither the primary
checkpoint nor any `known_outcomes` recognizes, when run with
`--escalate-on-failure`.

**Control transfer.** The browser runs headed (a real, visible OS window),
so "pausing" automation is literally: stop issuing Playwright calls and
block the automation thread on a `threading.Event`
(`HandoffController.wait_for_resume`). Nothing about the browser process is
torn down or recreated -- a human can click and type into that exact window
with their own hands. `HandoffController.state` tracks who's in control
explicitly rather than leaving it to be inferred. The operator console
(`escalation/operator_app.py`) runs as a Flask app in a background thread of
the *same process*, sharing the live controller; it shows the captured
context (goal, step, reason, a screenshot taken by the automation thread
just before pausing, since Playwright's sync API isn't safe to call
cross-thread) and a single control -- resume, with notes. Resuming sets the
event; the automation thread re-perceives the page and continues.

**What's mocked, deliberately (per the brief's explicit scope note):** the
operator console does not render or proxy the live page -- no co-browsing.
A real product would attach a viewer to the same CDP endpoint the
automation used (`--remote-debugging-port`), giving true remote co-browsing
without touching this control-transfer model at all; I didn't build that
because the brief calls it out of scope and the mechanism that matters --
pause, same-session control transfer, resume, recorded human action -- is
real and tested (`tests/test_escalation.py` drives it end-to-end over real
HTTP, with the resume signal coming from an actual second thread, not a
stub).

## 6. Safety

**Allowlist enforcement** (`config/allowlist.yaml`, `core/guardrails.py`) is
the single policy both discovery and replay consult before every navigation
and every action type -- domain, route pattern, and action-type allowlists,
plus an explicit `blocked_route_patterns` for the test-harness reset
endpoint, which is unreachable by either the agent or a replayed artifact by
construction (there's no code path to it, not just a missing tool).

**Risky vs. safe, and where I chose to enforce it.** Routes are classified
`risky` if they match `risky_route_patterns` (currently: opening a
sub-account -- irreversible through this UI). Discovery, an *attended* run
someone deliberately started and is (in a real deployment) watching, is
allowed to take risky actions -- that's the point of discovery. Replay, the
*unattended production path*, blocks any risky step by default and requires
an explicit `allow_risky=True` after review, or `--escalate-on-failure` to
route it to a human instead. That asymmetry is the actual safety
boundary: attended vs. unattended, not discovery vs. replay -- because the
brief's concern (per section 3.4) is irreversible actions happening without
a human aware of them, and that risk is specific to unattended execution.

**Redaction.** `core/guardrails.redact_dict`/`redact_text` run on every log
event and every extracted output before it's written to disk: pattern-based
scrubbing (SSN-shaped, card-shaped, long-numeric-ID-shaped values) plus
key-name-based scrubbing for anything literally named `password`,
`api_key`, `token`, etc. This runs at the point of writing (`EvidenceLog`,
the replay engine's `log()`), not left to callers to remember to call.

**Limits, honestly.** The redaction patterns are heuristic, not a real DLP
system -- they'd need tuning against real production data shapes. The
risky/safe classification is route-based and manually curated in YAML; it
doesn't infer risk from an action's semantics, so a new irreversible route
added to the target app needs a human to add it to the policy file. Neither
of these is hard to improve, but both are exactly the kind of thing I'd
want a security/compliance reviewer's eyes on before this touched a real
institution's data, not something I'd claim is production-hardened here.

## 7. Cuts

- **No true co-browsing operator console** -- explicitly out of scope per
  the brief; see section 5 for what's real vs. mocked and the CDP-based path
  to a full version.
- **No per-tenant override files or a drift-detection score** -- designed
  for (section 4), not built, per the brief's "design, not necessarily
  build" instruction for 3.7.
- **No desktop-automation backend** -- one concrete surface (web) was in
  scope; the seam that would carry a desktop backend is real (section 4)
  but unimplemented.
- **Redaction is heuristic**, not a tuned DLP system -- see section 6.
- **No stretch goals attempted** -- the brief asks for at most one or two,
  and I judged that the time was better spent making the core loop,
  schema, replay error taxonomy, and escalation mechanism all genuinely
  solid (and tested) rather than adding breadth on top of a thinner core.
- **What I'd build next**, in order: (1) a per-tenant override/drift layer
  on top of `vendor_product` (section 4) -- the highest-leverage gap given
  the brief's actual deployment environment; (2) an approval-gate stretch
  goal (`draft` -> `approved`) wired to whether an artifact has
  `known_outcomes` enrichment and at least N clean replays, since that's
  the natural checkpoint before trusting unattended replay in production;
  (3) real co-browsing via CDP attach, since the pause/resume/control-state
  mechanism underneath it is already there.
