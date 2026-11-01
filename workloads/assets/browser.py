from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import ipaddress
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from anti_bot_signatures import detect_anti_bot_page
from shared.readiness import (
    accepted_primary_taxonomy_predicate,
    tool_auto_publish_admission_predicate,
    tool_live_ready_predicate,
)
from workloads.assets.constants import (
    ASSET_DB_STORAGE_BUCKET,
    ASSET_DEAD_LETTER_REVIVE_HOURS,
    ASSET_REQUIREMENT_ORDER,
    ASSET_SOURCE,
    AUTO_APPROVE_TOOL_NAME_CONFIDENCE,
    CATALOG_AUTO_PUBLISH_MAX_LIMIT,
    CATALOG_AUTO_PUBLISH_POLICY_VERSION,
    DEFAULT_R2_BUCKET,
    REVIEW_TOOL_NAME_CONFIDENCE,
)
from taxonomy_schema import strict_json_schema
from workloads.assets.content import *  # noqa: F403
from workloads.assets.types import (
    AssetFetchResult,
    AssetPipelineError,
    AssetTask,
    PageQualityAssessment,
    PageQualityError,
)

from runner import (
    DEFAULT_TAXONOMY_CLASSIFICATION_FALLBACK_MODEL,
    DEFAULT_TAXONOMY_CLASSIFICATION_MODEL,
    D1_API_BASE,
    HOMEPAGE_BROWSER_6000_RETRY_DELAY_SECONDS,
    HOMEPAGE_MAX_REDIRECTS,
    MAX_HOMEPAGE_HTML_BYTES,
    Config,
    browser_request_headers,
    log_debug,
    log_error,
    log_info,
    random_browser_user_agent,
)

class CloudflareBrowserRunAssetClient:
    def __init__(self, config: Config):
        self.endpoint_base = f"{D1_API_BASE}/accounts/{config.cloudflare_account_id}/browser-rendering"
        self.headers = {
            "Authorization": f"Bearer {config.browser_rendering_api_token}",
            "Content-Type": "application/json",
        }
        self.timeout_seconds = config.browser_rendering_timeout_seconds
        self.taxonomy_api_token = config.taxonomy_classification_api_token
        self.taxonomy_deepseek_api_key = config.taxonomy_classification_deepseek_api_key
        self.taxonomy_openai_api_key = getattr(config, "openai_api_key", "")
        self.taxonomy_model = normalize_taxonomy_model_id(config.taxonomy_classification_model)
        self.taxonomy_fallback_model = normalize_taxonomy_model_id(
            config.taxonomy_classification_fallback_model
        )
        self.taxonomy_main_content_max_chars = config.taxonomy_main_content_max_chars
        self.taxonomy_deepseek_max_output_tokens = config.taxonomy_deepseek_max_output_tokens
        self._validated_pages: dict[int, tuple[str, str, PageQualityAssessment]] = {}

    async def call_quick_action_envelope(
        self,
        endpoint: str,
        body: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        async with httpx.AsyncClient(timeout=float(self.timeout_seconds)) as client:
            response = await client.post(f"{self.endpoint_base}/{endpoint}", headers=self.headers, json=body)
        text = response.text
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        if response.status_code < 200 or response.status_code >= 300 or (isinstance(parsed, dict) and parsed.get("success") is False):
            messages: list[str] = []
            error_codes: list[int] = []
            if isinstance(parsed, dict):
                errors = parsed.get("errors")
                if isinstance(errors, list):
                    for error in errors:
                        if not isinstance(error, dict):
                            continue
                        code = error.get("code")
                        message = str(error.get("message") or "").strip()
                        try:
                            if code is not None:
                                error_codes.append(int(code))
                        except (TypeError, ValueError):
                            pass
                        if message and code is not None:
                            messages.append(f"{code}: {message}")
                        elif message:
                            messages.append(message)
                        elif code is not None:
                            messages.append(f"code={code}")
            detail = "; ".join(messages)
            if not detail and parsed is not None:
                detail = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))[:300]
            if not detail:
                detail = text[:300] or f"HTTP {response.status_code}"
            retryable = (
                (endpoint == "json" and response.status_code in (400, 409, 422))
                or response.status_code in (408, 425, 429)
                or response.status_code >= 500
                or (200 <= response.status_code < 300 and isinstance(parsed, dict) and parsed.get("success") is False)
            )
            error_code = f"browser_run_{error_codes[0]}" if error_codes else f"browser_run_http_{response.status_code}"
            max_attempts: int | None = None
            retry_after_seconds: int | None = None
            if endpoint == "content" and 6000 in error_codes:
                # A second attempt may succeed after the target/CDN state changes,
                # but immediate identical retries only amplify this high-volume error.
                retryable = True
                max_attempts = 2
                retry_after_seconds = HOMEPAGE_BROWSER_6000_RETRY_DELAY_SECONDS
            elif endpoint == "content" and any(code in {2001, 5006, 6002} for code in error_codes):
                retryable = True
            raise AssetPipelineError(
                f"browser_run_{endpoint}_api_error: {detail}",
                retryable=retryable,
                error_code=error_code,
                max_attempts=max_attempts,
                retry_after_seconds=retry_after_seconds,
            )
        result = parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else parsed
        meta = parsed.get("meta") if isinstance(parsed, dict) and isinstance(parsed.get("meta"), dict) else {}
        return result, meta

    async def call_quick_action(self, endpoint: str, body: dict[str, Any]) -> Any:
        result, _ = await self.call_quick_action_envelope(endpoint, body)
        return result

    async def fetch_structured_text_data(
        self,
        *,
        source_url: str,
        prompt: str,
        json_schema: dict[str, Any],
        stage: str,
        custom_ai: list[dict[str, Any]] | None = None,
        allow_empty_required_arrays: bool = False,
        empty_object_means_empty_required_arrays: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Classify supplied cleaned text without navigating to a model-side product URL."""
        parsed_source = urlsplit(source_url)
        transport_task = AssetTask(
            tool_id=0,
            canonical_slug="cleaned-homepage-transport",
            normalized_domain=parsed_source.hostname or "invalid.local",
            official_url=source_url or "https://invalid.local/",
            attempts=0,
            max_attempts=1,
            generation=0,
            lease_token="prompt-only-classification-transport",
        )
        log_info(
            "classification.cleaned_text.request",
            stage=stage,
            source_url=source_url,
            prompt_chars=len(prompt),
            models=[str(item.get("model") or "") for item in (custom_ai or []) if item],
            deepseek_thinking_requested=(
                "disabled"
                if any(
                    str(item.get("model") or "").startswith("deepseek/")
                    and item.get("thinking") == {"type": "disabled"}
                    for item in (custom_ai or [])
                    if item
                )
                else "not_applicable"
            ),
        )
        _, metadata = await self.fetch_structured_asset_data(
            transport_task,
            prompt=prompt,
            json_schema=json_schema,
            stage=stage,
            custom_ai=custom_ai,
            allow_empty_required_arrays=allow_empty_required_arrays,
            empty_object_means_empty_required_arrays=empty_object_means_empty_required_arrays,
            inline_html=(
                '<main data-classification-transport="prompt-only">'
                "Cleaned homepage content is embedded only in the classification prompt."
                "</main>"
            ),
            result_url=source_url,
        )
        return source_url, metadata

    def asset_candidate_urls(self, task: AssetTask) -> list[str]:
        # Redirects are followed deliberately by the source fetcher. Do not send
        # a second immediate Browser Run request to an invented HTTP downgrade.
        return [asset_page_url(task)]

    def browser_payload(self, target_url: str, *, reject_heavy_resources: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": target_url,
            "userAgent": random_browser_user_agent(),
            "setExtraHTTPHeaders": {
                "Accept-Language": "en-US,en;q=0.9",
            },
            "gotoOptions": {
                "waitUntil": "domcontentloaded",
                "timeout": self.timeout_seconds * 1000,
            },
        }
        if reject_heavy_resources:
            payload["rejectResourceTypes"] = ["image", "media", "font"]
        return payload

    def taxonomy_custom_ai(self) -> list[dict[str, Any]]:
        models = list(
            dict.fromkeys(
                filter(
                    None,
                    [
                        normalize_taxonomy_model_id(
                            getattr(self, "taxonomy_model", DEFAULT_TAXONOMY_CLASSIFICATION_MODEL)
                        ),
                        normalize_taxonomy_model_id(
                            getattr(
                                self,
                                "taxonomy_fallback_model",
                                DEFAULT_TAXONOMY_CLASSIFICATION_FALLBACK_MODEL,
                            )
                        ),
                    ],
                )
            )
        )
        configs: list[dict[str, Any]] = []
        for model in models:
            if model.startswith("workers-ai/"):
                log_info(
                    "assets.taxonomy_custom_ai.exclude_untrusted_model",
                    model=model,
                    provider="workers-ai",
                )
                continue
            token = taxonomy_model_auth_token(model, self)
            if not token:
                log_info(
                    "assets.taxonomy_custom_ai.skip_missing_token",
                    model=model,
                    provider=(
                        "deepseek"
                        if model.startswith("deepseek/")
                        else "openai"
                        if model.startswith("openai/")
                        else "workers-ai"
                    ),
                )
                continue
            item: dict[str, Any] = {
                "model": model,
                "authorization": f"Bearer {token}",
            }
            # Some providers (gpt-oss, deepseek) spend tokens on reasoning; request a
            # higher completion budget where Browser Run honors it.
            if "gpt-oss" in model or model.startswith("deepseek/"):
                max_output_tokens = int(getattr(self, "taxonomy_deepseek_max_output_tokens", 1024))
                item["max_tokens"] = max_output_tokens
                item["max_completion_tokens"] = max_output_tokens
            if model.startswith("deepseek/"):
                item["thinking"] = {"type": "disabled"}
            configs.append(item)
        return configs

    @staticmethod
    def structured_payload_has_required_fields(
        metadata: dict[str, Any],
        json_schema: dict[str, Any],
        *,
        allow_empty_required_arrays: bool = False,
        empty_object_means_empty_required_arrays: bool = False,
    ) -> bool:
        required = json_schema.get("required")
        if not isinstance(required, list) or not required:
            return bool(metadata)
        if allow_empty_required_arrays:
            properties = json_schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            if empty_object_means_empty_required_arrays and not metadata:
                return all(
                    isinstance(properties.get(key), dict)
                    and properties[key].get("type") == "array"
                    for key in required
                )
            for key in required:
                if key not in metadata:
                    return False
                value = metadata.get(key)
                property_schema = properties.get(key)
                property_schema = property_schema if isinstance(property_schema, dict) else {}
                if property_schema.get("type") == "array" and isinstance(value, list):
                    continue
                if isinstance(value, str) and value.strip():
                    continue
                if value not in (None, "", [], {}):
                    continue
                return False
            return True
        for key in required:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if value not in (None, "", [], {}):
                return True
        return False

    async def fetch_structured_asset_data(
        self,
        task: AssetTask,
        *,
        prompt: str,
        json_schema: dict[str, Any],
        stage: str,
        custom_ai: list[dict[str, Any]] | None = None,
        allow_empty_required_arrays: bool = False,
        empty_object_means_empty_required_arrays: bool = False,
        inline_html: str | None = None,
        result_url: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        errors: list[str] = []
        retryable_errors: list[bool] = []
        # Browser Run only falls through custom_ai entries on hard API errors.
        # Empty / truncated model answers (e.g. gpt-oss finish_reason=length) still
        # count as HTTP success, so we walk models ourselves.
        model_attempts: list[list[dict[str, Any]] | None]
        if custom_ai:
            model_attempts = [[item] for item in custom_ai if item]
        else:
            model_attempts = [None]

        target_urls = (
            [str(result_url or asset_page_url(task))]
            if inline_html is not None
            else self.asset_candidate_urls(task)
        )
        for target_url in target_urls:
            for ai_config in model_attempts:
                model_name = ""
                if ai_config and isinstance(ai_config[0], dict):
                    model_name = str(ai_config[0].get("model") or "")
                response_format = build_browser_response_format(
                    json_schema,
                    model=model_name or None,
                    stage=stage,
                )
                request_prompt = augment_prompt_for_response_format(prompt, json_schema, response_format)
                try:
                    page_payload = (
                        {"html": inline_html}
                        if inline_html is not None
                        else self.browser_payload(target_url)
                    )
                    body = {
                        **page_payload,
                        "prompt": request_prompt,
                        "response_format": response_format,
                    }
                    if ai_config:
                        body["custom_ai"] = ai_config
                    extracted = await self.call_quick_action("json", body)
                    metadata = normalize_structured_json_payload(extracted)
                    if custom_ai and not self.structured_payload_has_required_fields(
                        metadata,
                        json_schema,
                        allow_empty_required_arrays=allow_empty_required_arrays,
                        empty_object_means_empty_required_arrays=empty_object_means_empty_required_arrays,
                    ):
                        log_info(
                            "assets.browser_json.empty_schema_result",
                            stage=stage,
                            url=target_url,
                            model=model_name,
                            response_format_type=response_format.get("type"),
                            raw_preview=json.dumps(extracted, ensure_ascii=False)[:200]
                            if not isinstance(extracted, (bytes, bytearray))
                            else "",
                        )
                        errors.append(f"{target_url}/{model_name or 'default'}: empty_schema_result")
                        retryable_errors.append(True)
                        continue
                    return target_url, metadata
                except Exception as error:
                    message = str(error)[:220]
                    # If a provider rejects our schema envelope, retry once with json_object.
                    if (
                        model_name.startswith("deepseek/")
                        and "missing field" in message.lower()
                        and "schema" in message.lower()
                        and response_format.get("type") != "json_object"
                    ):
                        try:
                            fallback_format = {"type": "json_object"}
                            fallback_prompt = augment_prompt_for_response_format(
                                prompt,
                                json_schema,
                                fallback_format,
                            )
                            page_payload = (
                                {"html": inline_html}
                                if inline_html is not None
                                else self.browser_payload(target_url)
                            )
                            body = {
                                **page_payload,
                                "prompt": fallback_prompt,
                                "response_format": fallback_format,
                            }
                            if ai_config:
                                body["custom_ai"] = ai_config
                            extracted = await self.call_quick_action("json", body)
                            metadata = normalize_structured_json_payload(extracted)
                            if self.structured_payload_has_required_fields(
                                metadata,
                                json_schema,
                                allow_empty_required_arrays=allow_empty_required_arrays,
                                empty_object_means_empty_required_arrays=empty_object_means_empty_required_arrays,
                            ):
                                return target_url, metadata
                            errors.append(f"{target_url}/{model_name}: empty_schema_result_after_json_object")
                            retryable_errors.append(True)
                            continue
                        except Exception as fallback_error:
                            message = str(fallback_error)[:220]
                    errors.append(f"{target_url}: {message}")
                    retryable_errors.append(bool(getattr(error, "retryable", True)))

        raise AssetPipelineError(
            f"Browser Run {stage} extraction failed. " + " | ".join(errors),
            retryable=any(retryable_errors),
        )

    async def fetch_static_homepage_content(self, task: AssetTask) -> tuple[str, str]:
        target_url = asset_page_url(task)
        if not is_safe_homepage_url(target_url):
            raise AssetPipelineError(
                f"homepage_static_unsafe_url:{target_url[:220]}",
                retryable=False,
                error_code="unsafe_homepage_url",
            )

        current_url = target_url
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=float(self.timeout_seconds),
                follow_redirects=False,
                headers=browser_request_headers(target_url),
            ) as client:
                for redirect_count in range(HOMEPAGE_MAX_REDIRECTS + 1):
                    response = await client.get(current_url)
                    location = response.headers.get("location", "").strip()
                    if response.status_code in {301, 302, 303, 307, 308} and location:
                        next_url = urljoin(current_url, location)
                        if not is_safe_homepage_url(next_url):
                            raise AssetPipelineError(
                                f"homepage_static_unsafe_redirect:{next_url[:220]}",
                                retryable=False,
                                error_code="unsafe_homepage_redirect",
                            )
                        if not homepage_urls_share_site(target_url, next_url):
                            raise AssetPipelineError(
                                f"homepage_static_unrelated_redirect:{target_url[:160]}->{next_url[:160]}",
                                retryable=False,
                                error_code="unrelated_homepage_redirect",
                            )
                        if redirect_count >= HOMEPAGE_MAX_REDIRECTS:
                            raise AssetPipelineError(
                                f"homepage_static_redirect_limit:{target_url[:220]}",
                                retryable=True,
                                error_code="homepage_redirect_limit",
                            )
                        current_url = next_url
                        continue
                    break
                else:  # pragma: no cover - loop exits through a response or an exception
                    raise AssetPipelineError("homepage_static_redirect_limit", retryable=True)
        except AssetPipelineError:
            raise
        except Exception as error:
            raise AssetPipelineError(
                f"homepage_static_request_error:{str(error)[:300] or type(error).__name__}",
                retryable=True,
                error_code="homepage_static_request_error",
            ) from error

        final_url = str(response.url)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        content_type = response.headers.get("content-type", "")
        raw_body = response.content[:MAX_HOMEPAGE_HTML_BYTES]
        looks_like_markup = raw_body.lstrip()[:200].lower().startswith((b"<!doctype html", b"<html", b"<?xml"))
        html_body = ""
        if any(kind in content_type.lower() for kind in ("html", "xml", "text")) or looks_like_markup:
            html_body = raw_body.decode(response.encoding or "utf-8", errors="replace")
        assessment = classify_page_state(
            html_body,
            page_title=read_html_title(html_body),
            http_status=response.status_code,
        )
        log_info(
            "assets.homepage_static.response",
            tool_id=task.tool_id,
            url=target_url,
            final_url=final_url,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            content_type=content_type[:120],
            page_state=assessment.state,
        )

        if assessment.state in {"not_found", "parked_domain"}:
            raise AssetPipelineError(
                f"homepage_static_terminal:{assessment.state}:{assessment.reason}",
                retryable=False,
                error_code=assessment.state,
            )
        if not 200 <= response.status_code < 300:
            raise AssetPipelineError(
                f"homepage_static_http_{response.status_code}:{assessment.state}:{assessment.reason}",
                retryable=True,
                error_code=f"homepage_static_http_{response.status_code}",
            )
        main_content = extract_homepage_main_text(html_body, limit=1000)
        if not assessment.is_valid or len(main_content) < 80:
            raise AssetPipelineError(
                f"homepage_static_requires_browser:{assessment.state}:{assessment.reason}:main_chars={len(main_content)}",
                retryable=True,
                error_code="homepage_static_requires_browser",
            )
        return final_url, html_body

    async def fetch_browser_homepage_content(self, task: AssetTask) -> tuple[str, str]:
        target_url = asset_page_url(task)
        content, meta = await self.call_quick_action_envelope(
            "content",
            self.browser_payload(target_url, reject_heavy_resources=True),
        )
        if isinstance(content, dict):
            html_body = str(content.get("content") or content.get("html") or "")
        else:
            html_body = str(content or "")
        final_url = str(meta.get("finalUrl") or target_url).strip() or target_url
        try:
            origin_status = int(meta.get("status")) if meta.get("status") is not None else None
        except (TypeError, ValueError):
            origin_status = None
        assessment = classify_page_state(
            html_body,
            page_title=str(meta.get("title") or ""),
            http_status=origin_status,
        )
        if not is_safe_homepage_url(final_url):
            raise AssetPipelineError(
                f"homepage_browser_unsafe_final_url:{final_url[:220]}",
                retryable=False,
                error_code="unsafe_homepage_final_url",
            )
        if not homepage_urls_share_site(target_url, final_url):
            raise AssetPipelineError(
                f"homepage_browser_unrelated_redirect:{target_url[:160]}->{final_url[:160]}",
                retryable=False,
                error_code="unrelated_homepage_redirect",
            )
        if assessment.state in {"not_found", "parked_domain"}:
            raise AssetPipelineError(
                f"homepage_browser_terminal:{assessment.state}:{assessment.reason}",
                retryable=False,
                error_code=assessment.state,
            )
        if not assessment.is_valid:
            raise AssetPipelineError(
                f"homepage_browser_invalid:{assessment.state}:{assessment.reason}",
                retryable=assessment.state in {"anti_bot", "access_denied", "captcha", "empty_page", "error_page"},
                error_code=f"homepage_browser_{assessment.state}",
            )
        log_info(
            "assets.homepage_browser.success",
            tool_id=task.tool_id,
            url=target_url,
            final_url=final_url,
            origin_status=origin_status,
            page_state=assessment.state,
        )
        return final_url, html_body

    async def fetch_homepage_content(self, task: AssetTask) -> tuple[str, str]:
        static_error: Exception | None = None
        try:
            final_url, html_body = await self.fetch_static_homepage_content(task)
            log_info(
                "assets.homepage_fetch.success",
                tool_id=task.tool_id,
                transport="static_http",
                final_url=final_url,
            )
            return final_url, html_body
        except AssetPipelineError as error:
            static_error = error
            if not error.retryable:
                raise
            log_info(
                "assets.homepage_static.fallback",
                tool_id=task.tool_id,
                error=str(error)[:300],
            )
        except Exception as error:  # defensive: a static transport defect must not suppress Browser Run
            static_error = error
            log_info(
                "assets.homepage_static.fallback",
                tool_id=task.tool_id,
                error=str(error)[:300] or type(error).__name__,
            )

        try:
            final_url, html_body = await self.fetch_browser_homepage_content(task)
            log_info(
                "assets.homepage_fetch.success",
                tool_id=task.tool_id,
                transport="browser_run",
                final_url=final_url,
            )
            return final_url, html_body
        except Exception as browser_error:
            static_detail = str(static_error)[:260] if static_error else "not_attempted"
            browser_detail = str(browser_error)[:320] or type(browser_error).__name__
            raise AssetPipelineError(
                f"homepage_content_failed:static={static_detail};browser={browser_detail}",
                retryable=bool(getattr(browser_error, "retryable", True)),
                error_code=str(getattr(browser_error, "error_code", "") or "homepage_browser_failed"),
                max_attempts=getattr(browser_error, "max_attempts", None),
                retry_after_seconds=getattr(browser_error, "retry_after_seconds", None),
            ) from browser_error

    async def preflight_homepage(self, task: AssetTask) -> PageQualityAssessment:
        cached = getattr(self, "_validated_pages", {}).get(task.tool_id)
        if cached:
            return cached[2]
        final_url, html_body = await self.fetch_homepage_content(task)
        assessment = classify_page_state(html_body)
        if not hasattr(self, "_validated_pages"):
            self._validated_pages = {}
        self._validated_pages[task.tool_id] = (final_url, html_body, assessment)
        return assessment

    async def fetch_homepage_core_metadata(self, task: AssetTask) -> AssetFetchResult:
        final_url = asset_page_url(task)
        html_body = ""
        cached = getattr(self, "_validated_pages", {}).get(task.tool_id)
        if cached:
            final_url, html_body, _ = cached

        if not html_body:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                async with httpx.AsyncClient(timeout=3.0, follow_redirects=True, verify=False) as client:
                    async with client.stream("GET", final_url, headers=headers) as response:
                        final_url = str(response.url)
                        chunks = []
                        total_bytes = 0
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                            total_bytes += len(chunk)
                            if total_bytes >= 65536:
                                break
                        raw_bytes = b"".join(chunks)
                        encoding = response.encoding or "utf-8"
                        html_body = raw_bytes.decode(encoding, errors="replace")
            except Exception as error:
                log_info("assets.static_homepage_fetch.bypass", url=final_url, error=str(error)[:200])

        resolution = resolve_tool_name(html_body, task.normalized_domain)
        description = read_html_meta(html_body, {"description", "og:description", "twitter:description"})
        favicon_href = extract_favicon_href(html_body, final_url) or ""

        is_adult = bool(_CONTENT_SAFETY_STRONG_DOMAIN_RE.search(task.normalized_domain))
        safety_status = "nsfw" if is_adult else "safe"

        return AssetFetchResult(
            final_url=final_url,
            html=html_body,
            title=resolution.product_name,
            page_title=resolution.page_title,
            description=clean_asset_text(description, 500),
            favicon_href=favicon_href,
            name_source=resolution.source,
            name_confidence=resolution.confidence,
            name_evidence=resolution.evidence,
            name_review_status=resolution.review_status,
            page_state="valid_product_page",
            page_state_reason="static_fetch",
            content_safety_status=safety_status,
            content_safety_score=100 if is_adult else 0,
            content_safety_confidence=95,
            content_safety_reason="domain_regex" if is_adult else "default_safe",
            content_safety_evidence=[],
            content_safety_source="domain_check",
            metadata_error="",
            metadata_retryable=False,
        )

    async def capture_homepage_screenshot(self, task: AssetTask) -> AssetFetchResult:

        errors: list[str] = []
        retryable_errors: list[bool] = []
        for target_url in self.asset_candidate_urls(task):
            try:
                snapshot = await self.call_quick_action("snapshot", self.browser_payload(target_url))
                screenshot_raw = snapshot.get("screenshot") if isinstance(snapshot, dict) else None
                if not screenshot_raw:
                    raise RuntimeError("snapshot returned no screenshot")
                if isinstance(screenshot_raw, str) and "," in screenshot_raw[:40]:
                    screenshot_raw = screenshot_raw.split(",", 1)[1]
                screenshot = base64.b64decode(str(screenshot_raw), validate=False)
                return AssetFetchResult(
                    final_url=target_url,
                    screenshot=screenshot,
                )
            except Exception as error:
                errors.append(f"{target_url}: {str(error)[:220]}")
                retryable_errors.append(bool(getattr(error, "retryable", True)))

        raise AssetPipelineError(
            "Browser Run asset capture failed. " + " | ".join(errors),
            retryable=any(retryable_errors),
        )


