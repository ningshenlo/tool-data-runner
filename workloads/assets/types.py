from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetTask:
    tool_id: int
    canonical_slug: str
    normalized_domain: str
    official_url: str
    attempts: int
    max_attempts: int
    generation: int
    lease_token: str


@dataclass
class AssetFetchResult:
    final_url: str
    screenshot: bytes = b""
    html: str = ""
    title: str = ""
    page_title: str = ""
    description: str = ""
    favicon_href: str = ""
    key_features: list[dict[str, str]] | None = None
    name_source: str = "internal"
    name_confidence: int = 100
    name_evidence: str = ""
    name_review_status: str = "auto_approved"
    page_state: str = "valid_product_page"
    page_state_reason: str = ""
    content_safety_status: str = "unknown"
    content_safety_score: int = 0
    content_safety_confidence: int = 0
    content_safety_reason: str = ""
    content_safety_evidence: list[str] | None = None
    content_safety_source: str = ""
    metadata_error: str = ""
    metadata_retryable: bool = True


@dataclass(frozen=True)
class PageQualityAssessment:
    state: str
    reason: str
    evidence: str = ""

    @property
    def is_valid(self) -> bool:
        return self.state == "valid_product_page"


@dataclass(frozen=True)
class ToolNameResolution:
    product_name: str
    page_title: str
    source: str
    confidence: int
    evidence: str
    review_status: str


@dataclass(frozen=True)
class ContentSafetyAssessment:
    status: str
    risk_score: int
    confidence: int
    reason: str
    evidence: list[str]
    source: str


class AssetPipelineError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        error_code: str = "",
        max_attempts: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.error_code = error_code
        self.max_attempts = max_attempts
        self.retry_after_seconds = retry_after_seconds


class PageQualityError(AssetPipelineError):
    def __init__(self, assessment: PageQualityAssessment, *, retry_after_hours: int):
        super().__init__(
            f"page_invalid:{assessment.state}:{assessment.reason}:{assessment.evidence}"[:900],
            retryable=assessment.state in {"anti_bot", "access_denied", "captcha", "empty_page", "error_page"},
        )
        self.assessment = assessment
        self.retry_after_hours = retry_after_hours


class ContentSafetyError(AssetPipelineError):
    def __init__(self, assessment: ContentSafetyAssessment):
        super().__init__(
            f"content_safety_blocked:{assessment.status}:{assessment.reason}:"
            f"{','.join(assessment.evidence[:8])}"[:900],
            retryable=False,
        )
        self.assessment = assessment


@dataclass(frozen=True)
class FaviconAsset:
    body: bytes
    key: str
    mime_type: str
