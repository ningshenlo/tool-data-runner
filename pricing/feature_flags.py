"""Safety gates for the pricing claims rollout."""


class PricingClaimFeatureFlagError(RuntimeError):
    """Raised when a claims publishing flag combination is unsafe."""


def assert_safe_pricing_claim_flags(*, shadow_enabled: bool, publish_enabled: bool) -> None:
    if publish_enabled and not shadow_enabled:
        raise PricingClaimFeatureFlagError(
            "PRICING_CLAIMS_PUBLISH requires PRICING_CLAIMS_SHADOW=1"
        )
