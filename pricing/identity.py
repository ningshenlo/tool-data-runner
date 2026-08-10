"""Cross-snapshot identity resolution for plans, offers, and allowances."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Literal


ResolutionStatus = Literal["matched", "new", "conflict"]
ContinuityAction = Literal["continue", "supersede", "new"]


def normalize_identity_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(character if character.isalnum() else " " for character in text).split())


def _normalized_tokens(values: Iterable[str]) -> frozenset[str]:
    return frozenset(token for value in values if (token := normalize_identity_text(value)))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class SubjectCandidate:
    candidate_key: str
    subject_type: str
    source_scope: str = "unknown"
    region_identity: str = "default"
    name: str = ""
    audience: str = ""
    billing_context: str = ""
    structured_id: str = ""
    card_signature: frozenset[str] = field(default_factory=frozenset)
    anchor_signature: frozenset[str] = field(default_factory=frozenset)
    price_signature: frozenset[str] = field(default_factory=frozenset)
    position_hint: int | None = None

    @classmethod
    def build(cls, **values: object) -> "SubjectCandidate":
        prepared = dict(values)
        for key in ("card_signature", "anchor_signature", "price_signature"):
            raw = prepared.get(key) or ()
            prepared[key] = _normalized_tokens(str(item) for item in raw)  # type: ignore[arg-type]
        return cls(**prepared)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class KnownSubject:
    subject_id: int
    subject_key: str
    subject_type: str
    source_scope: str = "unknown"
    region_identity: str = "default"
    current_name: str = ""
    name_aliases: frozenset[str] = field(default_factory=frozenset)
    audience: str = ""
    billing_context: str = ""
    structured_ids: frozenset[str] = field(default_factory=frozenset)
    card_signature: frozenset[str] = field(default_factory=frozenset)
    anchor_signature: frozenset[str] = field(default_factory=frozenset)
    price_signature: frozenset[str] = field(default_factory=frozenset)
    position_hint: int | None = None

    @classmethod
    def build(cls, **values: object) -> "KnownSubject":
        prepared = dict(values)
        for key in (
            "name_aliases",
            "structured_ids",
            "card_signature",
            "anchor_signature",
            "price_signature",
        ):
            raw = prepared.get(key) or ()
            prepared[key] = _normalized_tokens(str(item) for item in raw)  # type: ignore[arg-type]
        return cls(**prepared)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ScoredSubject:
    subject: KnownSubject
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    status: ResolutionStatus
    subject_id: int | None
    subject_key: str | None
    score: float
    best_candidate_subject_id: int | None = None
    competing_subject_id: int | None = None
    competing_score: float = 0.0
    reasons: tuple[str, ...] = ()


def score_subject_candidate(candidate: SubjectCandidate, subject: KnownSubject) -> ScoredSubject:
    if candidate.subject_type != subject.subject_type:
        return ScoredSubject(subject, 0.0, ("subject_type_mismatch",))
    if candidate.source_scope != subject.source_scope:
        return ScoredSubject(subject, 0.0, ("source_scope_mismatch",))
    if candidate.region_identity != subject.region_identity:
        return ScoredSubject(subject, 0.0, ("region_identity_mismatch",))

    score = 0.0
    reasons: list[str] = []
    structured_id = normalize_identity_text(candidate.structured_id)
    if structured_id and structured_id in subject.structured_ids:
        return ScoredSubject(subject, 1.0, ("structured_id_exact",))

    candidate_name = normalize_identity_text(candidate.name)
    subject_names = _normalized_tokens((subject.current_name, *subject.name_aliases))
    if candidate_name and candidate_name in subject_names:
        score += 0.38
        reasons.append("name_or_alias_exact")
    if normalize_identity_text(candidate.audience) and normalize_identity_text(candidate.audience) == normalize_identity_text(subject.audience):
        score += 0.10
        reasons.append("audience_exact")
    if normalize_identity_text(candidate.billing_context) and normalize_identity_text(candidate.billing_context) == normalize_identity_text(subject.billing_context):
        score += 0.12
        reasons.append("billing_context_exact")

    card_similarity = _jaccard(candidate.card_signature, subject.card_signature)
    anchor_similarity = _jaccard(candidate.anchor_signature, subject.anchor_signature)
    price_similarity = _jaccard(candidate.price_signature, subject.price_signature)
    if card_similarity:
        score += 0.24 * card_similarity
        reasons.append(f"card_similarity:{card_similarity:.2f}")
    if anchor_similarity:
        score += 0.12 * anchor_similarity
        reasons.append(f"anchor_similarity:{anchor_similarity:.2f}")
    if price_similarity:
        score += 0.16 * price_similarity
        reasons.append(f"price_similarity:{price_similarity:.2f}")
    if candidate.position_hint is not None and subject.position_hint is not None:
        distance = abs(candidate.position_hint - subject.position_hint)
        if distance == 0:
            score += 0.04
            reasons.append("position_equal")
        elif distance == 1:
            score += 0.02
            reasons.append("position_adjacent")

    return ScoredSubject(subject, min(score, 1.0), tuple(reasons or ["no_identity_signal"]))


def resolve_subject_identity(
    candidate: SubjectCandidate,
    known_subjects: Iterable[KnownSubject],
    *,
    match_threshold: float = 0.62,
    conflict_margin: float = 0.08,
) -> ResolutionResult:
    scored = sorted(
        (score_subject_candidate(candidate, subject) for subject in known_subjects),
        key=lambda item: (-item.score, item.subject.subject_id),
    )
    if not scored or scored[0].score < match_threshold:
        best_score = scored[0].score if scored else 0.0
        reasons = scored[0].reasons if scored else ("no_known_subject",)
        return ResolutionResult("new", None, None, best_score, reasons=reasons)

    best = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    if runner_up and runner_up.score >= match_threshold and best.score - runner_up.score < conflict_margin:
        return ResolutionResult(
            "conflict",
            None,
            None,
            best.score,
            best_candidate_subject_id=best.subject.subject_id,
            competing_subject_id=runner_up.subject.subject_id,
            competing_score=runner_up.score,
            reasons=(*best.reasons, "ambiguous_runner_up"),
        )

    return ResolutionResult(
        "matched",
        best.subject.subject_id,
        best.subject.subject_key,
        best.score,
        best_candidate_subject_id=best.subject.subject_id,
        competing_subject_id=runner_up.subject.subject_id if runner_up else None,
        competing_score=runner_up.score if runner_up else 0.0,
        reasons=best.reasons,
    )


@dataclass(frozen=True, slots=True)
class ClaimHistory:
    claim_id: int
    subject_id: int
    claim_type: str
    value_fingerprint: str
    first_seen_snapshot_id: int
    last_seen_snapshot_id: int
    consecutive_seen_count: int


@dataclass(frozen=True, slots=True)
class ClaimContinuity:
    action: ContinuityAction
    first_seen_snapshot_id: int
    last_seen_snapshot_id: int
    consecutive_seen_count: int
    superseded_claim_id: int | None = None


def advance_claim_history(
    previous: ClaimHistory | None,
    *,
    subject_id: int,
    claim_type: str,
    value_fingerprint: str,
    snapshot_id: int,
) -> ClaimContinuity:
    if previous is None or previous.subject_id != subject_id or previous.claim_type != claim_type:
        return ClaimContinuity("new", snapshot_id, snapshot_id, 1)
    if previous.value_fingerprint == value_fingerprint:
        return ClaimContinuity(
            "continue",
            previous.first_seen_snapshot_id,
            snapshot_id,
            previous.consecutive_seen_count + 1,
        )
    return ClaimContinuity(
        "supersede",
        snapshot_id,
        snapshot_id,
        1,
        superseded_claim_id=previous.claim_id,
    )
