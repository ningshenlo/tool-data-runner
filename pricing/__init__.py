"""Evidence-bound pricing claims pipeline primitives."""

from .claim_states import ClaimState, ClaimStateInvariantError, assert_claim_invariants
from .bundle import PricingSnapshotBundle, SnapshotArtifactPayload, build_pricing_snapshot_bundle
from .dom import DomNode, PricingDomMap, StructuredDataBlock, parse_pricing_dom
from .feature_flags import PricingClaimFeatureFlagError, assert_safe_pricing_claim_flags
from .identity import (
    ClaimHistory,
    ClaimContinuity,
    KnownSubject,
    ResolutionResult,
    SubjectCandidate,
    advance_claim_history,
    resolve_subject_identity,
)
from .normalize import NormalizationResult, normalize_raw_claim
from .snapshot import SnapshotArtifact, SnapshotCapturePlan, build_snapshot_artifact, plan_snapshot_capture
from .raw_claims import ClaimEvidence, RawPricingClaim, extract_level1_raw_claims
from .regions import PricingRegion, detect_pricing_region
from .validate import ValidationResult, validate_raw_claim

__all__ = [
    "ClaimContinuity",
    "ClaimHistory",
    "ClaimState",
    "ClaimStateInvariantError",
    "ClaimEvidence",
    "DomNode",
    "KnownSubject",
    "NormalizationResult",
    "PricingClaimFeatureFlagError",
    "PricingDomMap",
    "PricingRegion",
    "PricingSnapshotBundle",
    "RawPricingClaim",
    "ResolutionResult",
    "SubjectCandidate",
    "ValidationResult",
    "SnapshotArtifact",
    "SnapshotArtifactPayload",
    "SnapshotCapturePlan",
    "StructuredDataBlock",
    "advance_claim_history",
    "assert_claim_invariants",
    "assert_safe_pricing_claim_flags",
    "build_snapshot_artifact",
    "build_pricing_snapshot_bundle",
    "detect_pricing_region",
    "extract_level1_raw_claims",
    "normalize_raw_claim",
    "parse_pricing_dom",
    "plan_snapshot_capture",
    "resolve_subject_identity",
    "validate_raw_claim",
]
