"""Claim state invariants shared by extraction, review, and publication.

The database stores five orthogonal state dimensions.  Keeping the allowed
combinations in one module prevents each pipeline stage from inventing its own
slightly different publication rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable


NORMALIZATION_STATUSES = frozenset({"pending", "normalized", "failed", "not_applicable"})
VALIDATION_STATUSES = frozenset({"pending", "entailed", "unsupported", "conflict"})
DECISION_STATUSES = frozenset(
    {"unreviewed", "auto_verified", "human_verified", "corrected", "unresolved", "rejected"}
)
LIFECYCLE_STATUSES = frozenset({"active", "aging", "stale", "superseded", "invalidated"})
PUBLICATION_STATUSES = frozenset({"not_eligible", "eligible", "published", "withdrawn"})

VERIFIED_DECISIONS = frozenset({"auto_verified", "human_verified", "corrected"})
NON_PUBLISHABLE_LIFECYCLES = frozenset({"stale", "superseded", "invalidated"})
PRESENCE_CLAIM_TYPES = frozenset(
    {
        "has_free_plan",
        "has_free_trial",
        "card_required",
        "has_paid_pricing",
        "has_usage_pricing",
        "has_custom_quote",
        "plan_exists",
    }
)


class ClaimStateInvariantError(ValueError):
    """Raised when a persisted claim state could not occur legally."""


@dataclass(frozen=True, slots=True)
class ClaimState:
    normalization_status: str = "pending"
    validation_status: str = "pending"
    decision_status: str = "unreviewed"
    lifecycle_status: str = "active"
    publication_status: str = "not_eligible"


def claim_invariant_errors(state: ClaimState, *, claim_type: str | None = None) -> tuple[str, ...]:
    errors: list[str] = []

    dimensions = (
        ("normalization_status", state.normalization_status, NORMALIZATION_STATUSES),
        ("validation_status", state.validation_status, VALIDATION_STATUSES),
        ("decision_status", state.decision_status, DECISION_STATUSES),
        ("lifecycle_status", state.lifecycle_status, LIFECYCLE_STATUSES),
        ("publication_status", state.publication_status, PUBLICATION_STATUSES),
    )
    for name, value, allowed in dimensions:
        if value not in allowed:
            errors.append(f"unknown {name}: {value}")

    if errors:
        return tuple(errors)

    publishable = state.publication_status in {"eligible", "published"}
    if state.normalization_status == "failed" and publishable:
        errors.append("failed normalization cannot be eligible or published")
    if state.validation_status in {"unsupported", "conflict"} and publishable:
        errors.append("unsupported or conflicting evidence cannot be eligible or published")
    if state.decision_status in {"unresolved", "rejected"} and publishable:
        errors.append("unresolved or rejected claims cannot be eligible or published")
    if state.lifecycle_status in NON_PUBLISHABLE_LIFECYCLES and publishable:
        errors.append("non-active claims cannot be eligible or published")

    if publishable:
        if state.normalization_status not in {"normalized", "not_applicable"}:
            errors.append("publishable claims must be normalized or explicitly not applicable")
        if state.validation_status != "entailed":
            errors.append("publishable claims must be entailed by evidence")
        if state.decision_status not in VERIFIED_DECISIONS:
            errors.append("publishable claims must have a verified decision")
        if state.publication_status == "eligible" and state.lifecycle_status != "active":
            errors.append("only active claims can become eligible")
        if state.publication_status == "published" and state.lifecycle_status not in {"active", "aging"}:
            errors.append("published claims must be active or aging")

    if state.normalization_status == "not_applicable" and claim_type is not None:
        if claim_type not in PRESENCE_CLAIM_TYPES:
            errors.append(f"normalization is not_applicable only for presence claims, not {claim_type}")

    return tuple(errors)


def assert_claim_invariants(state: ClaimState, *, claim_type: str | None = None) -> None:
    errors = claim_invariant_errors(state, claim_type=claim_type)
    if errors:
        raise ClaimStateInvariantError("; ".join(errors))


def valid_claim_states(*, claim_type: str | None = None) -> Iterable[ClaimState]:
    """Generate the legal-combination fixture from the canonical invariant rules."""

    for values in product(
        sorted(NORMALIZATION_STATUSES),
        sorted(VALIDATION_STATUSES),
        sorted(DECISION_STATUSES),
        sorted(LIFECYCLE_STATUSES),
        sorted(PUBLICATION_STATUSES),
    ):
        state = ClaimState(*values)
        if not claim_invariant_errors(state, claim_type=claim_type):
            yield state
