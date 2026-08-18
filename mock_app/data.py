"""In-memory 'core banking' data store for the mock legacy app.

Deliberately not a real DB: this app exists only to give the agent a
realistic, hostile-markup surface to drive. State is process-local and
reset via reset_state() so discovery/replay runs are reproducible.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field


@dataclass
class Member:
    member_id: str
    name: str
    status: str  # "active" | "restricted"
    savings_balance: float
    checking_balance: float
    subaccounts: list[dict] = field(default_factory=list)


_seed = {
    "10234": Member("10234", "Alicia Ferro", "active", 4210.55, 812.10),
    "10500": Member("10500", "Ben Okafor", "active", 150.00, 40.22),
    "30500": Member("30500", "Compliance Hold LLC Trust", "restricted", 98000.00, 0.0),
    "40999": Member("40999", "Session Demo Member", "active", 500.00, 500.00),
}

_ref_counter = itertools.count(101)

MEMBERS: dict[str, Member] = {}


def reset_state() -> None:
    """Restore the store to its seed state. Used before each discovery/replay run
    so runs are deterministic and don't leak state between them."""
    global MEMBERS, _ref_counter
    MEMBERS = {
        mid: Member(m.member_id, m.name, m.status, m.savings_balance, m.checking_balance, [])
        for mid, m in _seed.items()
    }
    _ref_counter = itertools.count(101)


def next_subaccount_ref() -> str:
    return f"SUB-{next(_ref_counter):06d}"


reset_state()
