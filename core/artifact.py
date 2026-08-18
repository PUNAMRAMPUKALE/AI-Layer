"""The capability artifact: the typed, versioned contract an AI agent invokes.

Design intent (see REPORT.md section 2 for the full rationale):

  - A step's target is never a single selector. It's an ordered list of
    LocatorCandidates (accessibility role+name first, then text, then a
    structural CSS fallback) so replay can survive small DOM changes the
    way a human would -- by recognizing "the Search button" rather than
    "#btn-3". This is the seam that has to hold up on legacy markup with
    no test IDs.
  - Steps don't just describe the happy path. Each step carries
    `known_outcomes`: alternate branches the discovery run observed or the
    author anticipated (not-found, restricted, session-expired, ...), each
    tagged with a classification (business_outcome / recoverable /
    hard_failure). Deterministic replay uses this instead of guessing at
    runtime, which is what makes the error-taxonomy requirement (section
    3.3) a first-class part of the schema instead of a try/except bolted
    onto the replay engine.
  - Parameters and outputs are declared once, at the artifact level, so a
    calling agent (or a function-calling shim, see stretch goal) has a
    contract it can validate against without reading the step list.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"           # accessibility role + accessible name, e.g. button "Search"
    LABEL = "label"                   # associated <label> text for a form control
    TEXT = "text"                     # visible text content (links, static buttons)
    TABLE_ROW_VALUE = "table_row_value"  # value cell in a label/value table row, keyed by the label text
    CSS = "css"                       # structural CSS fallback -- most brittle, last resort


class LocatorCandidate(BaseModel):
    strategy: LocatorStrategy
    role: str | None = None
    name: str | None = None
    css: str | None = None
    exact: bool = True
    robustness_notes: str = Field(
        default="", description="Why this candidate was chosen / how fragile it is."
    )


class ElementTarget(BaseModel):
    description: str
    candidates: list[LocatorCandidate]
    frame_path: list[str] = Field(
        default_factory=list,
        description="Names/indices of nested frames to enter before resolving candidates. "
        "Empty for a top-level document; populated for frameset-based legacy apps.",
    )


class CheckpointKind(str, Enum):
    URL_CONTAINS = "url_contains"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    ELEMENT_VISIBLE = "element_visible"


class Checkpoint(BaseModel):
    kind: CheckpointKind
    value: str
    description: str


class OutcomeClassification(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"  # expected, legitimate result the caller needs (not an error)
    RECOVERABLE = "recoverable"            # known transient/interstitial condition; replay handles it and continues
    HARD_FAILURE = "hard_failure"          # stop, surface a clear debuggable error


class RecoveryAction(BaseModel):
    """What to do to clear a recoverable condition before retrying the step."""
    target: ElementTarget
    action: str = "click"
    then: str = "retry_step"  # "retry_step" | "continue_next_step"


class KnownOutcome(BaseModel):
    """A non-happy-path branch this step may hit, recognized by a checkpoint match."""
    code: str
    match: Checkpoint
    classification: OutcomeClassification
    message_template: str
    recovery: RecoveryAction | None = None


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT_OPTION = "select_option"
    EXTRACT_TEXT = "extract_text"
    WAIT_FOR = "wait_for"


class Step(BaseModel):
    step_id: int
    action: ActionType
    target: ElementTarget | None = None
    url: str | None = None                # for ACTION.NAVIGATE
    value_param: str | None = None        # name of a declared Parameter whose runtime value fills this step
    value_literal: str | None = None      # or a fixed literal (e.g. a select option that isn't parameterized)
    extract_as: str | None = None         # name of a declared OutputField this step extracts, for EXTRACT_TEXT
    checkpoint: Checkpoint | None = None  # asserted after the action, before moving on
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    risk_level: str = "safe"              # "safe" | "risky" -- see core/guardrails.py
    timeout_ms: int = 8000
    reasoning: str = ""                   # discovery-time notes on why this step/locator was chosen


class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class Parameter(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: str | None = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


class RiskLevel(str, Enum):
    SAFE = "safe"      # read-only / reversible
    RISKY = "risky"    # irreversible or write-with-consequences


class TargetApp(BaseModel):
    app_id: str = Field(description="Stable id for the underlying vendor product/app, e.g. 'coreserv-member-admin'.")
    base_url: str
    entry_route: str = "/"
    vendor_product: str = Field(
        default="",
        description="Vendor product identity independent of tenant branding/base_url -- the key for "
        "cross-tenant reuse (see REPORT.md section 4).",
    )


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0"
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    version: int = 1
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_from_run_id: str

    target: TargetApp
    parameters: list[Parameter]
    outputs: list[OutputField]
    steps: list[Step]
    success_checkpoint: Checkpoint

    risk_level: RiskLevel
    risk_notes: str = ""
    tags: list[str] = Field(default_factory=list)

    def param_names(self) -> set[str]:
        return {p.name for p in self.parameters}


class ArtifactStore:
    """Flat-file JSON store, one file per (name, version). Newest version per
    name is what replay resolves by default, but old versions stay addressable --
    an artifact is meant to be reviewable and diffable, not a black box."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str, version: int) -> Path:
        return self.root / name / f"v{version}.json"

    def save(self, artifact: CapabilityArtifact) -> Path:
        path = self._path(artifact.name, artifact.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load(self, name: str, version: int | None = None) -> CapabilityArtifact:
        if version is None:
            version = self.latest_version(name)
        path = self._path(name, version)
        if not path.exists():
            raise FileNotFoundError(f"No artifact '{name}' version {version} at {path}")
        return CapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def latest_version(self, name: str) -> int:
        dir_ = self.root / name
        if not dir_.exists():
            raise FileNotFoundError(f"No artifact named '{name}' in {self.root}")
        versions = [int(p.stem[1:]) for p in dir_.glob("v*.json")]
        if not versions:
            raise FileNotFoundError(f"No versions found for artifact '{name}' in {dir_}")
        return max(versions)

    def list_names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())
