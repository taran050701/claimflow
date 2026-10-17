"""The claim lifecycle as a state machine. Pure Python.

TRANSITIONS is the single source of truth for which status changes are legal.
The database enforces the same table with a trigger (migration 0004), and a
test asserts the two never drift apart.
"""

from claimflow.domain.enums import ClaimStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.RECEIVED: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.ADJUDICATED, S.PENDED}),
    S.PENDED: frozenset({S.ADJUDICATED, S.PENDED}),  # re-adjudicated, still waiting
    S.ADJUDICATED: frozenset({S.PAID, S.DENIED}),  # payment release ("finalize")
    S.DENIED: frozenset({S.APPEALED}),
    S.APPEALED: frozenset({S.ADJUDICATED, S.PENDED}),  # re-adjudication decides
    S.PAID: frozenset(),
    S.REJECTED: frozenset(),  # provider must submit a new, corrected claim
}

INITIAL = S.RECEIVED
TERMINAL = frozenset(s for s, targets in TRANSITIONS.items() if not targets)

# What each API action can lead to. An action is available when the current
# status can reach at least one of its targets.
ACTIONS: dict[str, frozenset[S]] = {
    "validate": frozenset({S.VALIDATED, S.REJECTED}),
    "adjudicate": frozenset({S.ADJUDICATED, S.PENDED}),
    "finalize": frozenset({S.PAID, S.DENIED}),
    "appeal": frozenset({S.APPEALED}),
}


class IllegalTransitionError(Exception):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"Illegal status transition {current} -> {target}")
        self.current = current
        self.target = target


class ActionNotAllowedError(Exception):
    def __init__(self, current: S, action: str) -> None:
        allowed = ", ".join(allowed_actions(current)) or "none (terminal status)"
        super().__init__(f"Cannot {action} a claim in status {current}. Allowed actions: {allowed}")
        self.current = current
        self.action = action


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)


def allowed_actions(current: S) -> list[str]:
    return [action for action, targets in ACTIONS.items() if targets & TRANSITIONS[current]]


def assert_action_allowed(current: S, action: str) -> None:
    """Fail fast before doing any work (e.g. running the rules engine)."""
    if not ACTIONS[action] & TRANSITIONS[current]:
        raise ActionNotAllowedError(current, action)
