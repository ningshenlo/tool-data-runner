"""Persistent OpenAI Batch API runner for multidimensional taxonomy.

The module submits Responses API JSONL batches and persists every canonical
taxonomy stage in D1. Accepted primary terms remain canonical taxonomy records;
the batch runner never mirrors them into the retired category schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from taxonomy_shadow import (
    PROVIDER_BLOCKED_RE,
    SHADOW_EXTRACTOR_VERSION,
    TaxonomyCatalog,
    auto_primary_write_state,
    build_leaf_candidate_pool,
    build_product_profile,
    capabilities_prompt,
    capabilities_schema,
    insert_classification_run,
    leaf_adjudication_prompt,
    leaf_adjudication_schema,
    load_taxonomy_catalog,
    load_verified_manual_primary_term_id,
    parse_capabilities,
    parse_leaf_decision,
    parse_secondary_leaf_decisions,
    parse_top2_l1,
    profile_evidence_text,
    profile_extract_from_main_content_prompt,
    profile_extract_schema,
    profile_has_signal,
    recall_capability_candidates,
    resolve_entity_decision,
    supersede_auto_assignments,
    top2_l1_prompt,
    top2_l1_schema,
    update_classification_run_status,
    update_tool_entity_kind,
    upsert_assignment,
    upsert_product_profile,
    utc_now_iso,
)

BATCH_PIPELINE_VERSION = "openai-batch-p2a-v1-2026-08-24"
BATCH_PROMPT_VERSION = "openai-batch-markets-capabilities-v2-security-precision-2026-08-27"
OPENAI_BATCH_ENDPOINT = "/v1/responses"
FAILED_BATCH_STATUSES = {"failed", "expired", "cancelled"}
OPENAI_FILE_READY_STATUS = "processed"
OPENAI_FILE_FAILED_STATUS = "error"
STAGES = {
    "profile",
    "l1",
    "l1_escalation",
    "leaf",
    "leaf_escalation",
    "capability",
}
D1_REQUEST_RESERVATION_CHUNK_SIZE = 50
D1_SUBMISSION_PERSIST_ATTEMPTS = 3


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_error(value: Any, limit: int = 1000) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = _json(value)
        except (TypeError, ValueError):
            text = str(value)
    return text.strip()[:limit]


def is_provider_blocked_error(error: Any) -> bool:
    text = str(error or "")
    return bool(
        PROVIDER_BLOCKED_RE.search(text)
        or "(401)" in text
        or "(403)" in text
    )


def batch_failure_error(batch: dict[str, Any], status: str) -> str:
    """Preserve provider validation details instead of a generic batch failure."""
    details: list[str] = []
    errors = batch.get("errors")
    rows = errors.get("data") if isinstance(errors, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        param = str(row.get("param") or "").strip()
        message = str(row.get("message") or "").strip()
        prefix = ":".join(value for value in (code, param) if value)
        detail = f"{prefix}: {message}" if prefix and message else prefix or message
        if detail:
            details.append(detail)
    base = f"openai_batch_{status}"
    return _safe_error(f"{base}: {' | '.join(details)}" if details else base)


def is_batch_input_file_access_error(error: Any) -> bool:
    """Detect the OpenAI Batch validator outage without masking other failures."""
    text = str(error or "").strip().lower()
    return bool(
        "openai_batch_" in text
        and "cannot find file" in text
        and "does not have access" in text
        and "file_id" in text
    )


def taxonomy_retry_policy(config: Any) -> tuple[int, int]:
    """Return total attempts (including the first) and exponential base delay."""
    max_attempts = min(
        10, max(1, int(getattr(config, "taxonomy_batch_max_attempts", 3)))
    )
    base_seconds = min(
        21600,
        max(30, int(getattr(config, "taxonomy_batch_retry_base_seconds", 300))),
    )
    return max_attempts, base_seconds


def taxonomy_retry_delay_seconds(config: Any, failed_attempt: int) -> int:
    _, base_seconds = taxonomy_retry_policy(config)
    exponent = max(0, min(10, int(failed_attempt) - 1))
    return min(21600, base_seconds * (2**exponent))


def is_retryable_stage_error(error: Any) -> bool:
    """Reject deterministic request defects while retrying transport/provider faults."""
    text = str(error or "").strip().lower()
    if not text:
        return True
    deterministic_markers = (
        "http_400",
        "http_401",
        "http_403",
        "http_404",
        "http_422",
        "invalid_request_error",
        "unsupported_parameter",
        "model_not_found",
        "content_policy",
        "safety_policy",
    )
    return not any(marker in text for marker in deterministic_markers)


def extract_response_output_text(body: dict[str, Any]) -> str:
    """Extract assistant text from a Responses API result."""
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for output in body.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return str(content["text"]).strip()
    return ""


def response_usage(body: dict[str, Any]) -> dict[str, int]:
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    input_details = (
        usage.get("input_tokens_details")
        if isinstance(usage.get("input_tokens_details"), dict)
        else {}
    )
    output_details = (
        usage.get("output_tokens_details")
        if isinstance(usage.get("output_tokens_details"), dict)
        else {}
    )
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize existing schemas to OpenAI strict Structured Outputs rules.

    Strict mode requires every property of an object to be listed in
    ``required``. Existing Browser Rendering schemas intentionally allowed a
    few evidence metadata fields to be absent, so Batch asks the model to emit
    empty strings for those fields instead.
    """
    cloned = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
            for child in node["properties"].values():
                visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for keyword in ("anyOf", "oneOf", "allOf"):
            for child in node.get(keyword) or []:
                visit(child)

    visit(cloned)
    return cloned


@dataclass(frozen=True)
class ParsedBatchResult:
    custom_id: str
    ok: bool
    response_body: dict[str, Any]
    structured_output: dict[str, Any]
    usage: dict[str, int]
    error: str = ""


def parse_batch_output_line(line: str | dict[str, Any]) -> ParsedBatchResult:
    payload = json.loads(line) if isinstance(line, str) else line
    if not isinstance(payload, dict):
        raise ValueError("batch output line must be a JSON object")
    custom_id = str(payload.get("custom_id") or "").strip()
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    status_code = int(response.get("status_code") or 0)
    top_error = payload.get("error")
    if top_error or not (200 <= status_code < 300):
        return ParsedBatchResult(
            custom_id=custom_id,
            ok=False,
            response_body=body,
            structured_output={},
            usage=response_usage(body),
            error=_safe_error(top_error or body or f"http_{status_code}"),
        )
    output_text = extract_response_output_text(body)
    try:
        structured = json.loads(output_text)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return ParsedBatchResult(
            custom_id=custom_id,
            ok=False,
            response_body=body,
            structured_output={},
            usage=response_usage(body),
            error=f"invalid_structured_output: {error}",
        )
    if not isinstance(structured, dict):
        return ParsedBatchResult(
            custom_id=custom_id,
            ok=False,
            response_body=body,
            structured_output={},
            usage=response_usage(body),
            error="structured_output_not_object",
        )
    return ParsedBatchResult(
        custom_id=custom_id,
        ok=True,
        response_body=body,
        structured_output=structured,
        usage=response_usage(body),
    )


def responses_request_body(
    *,
    model: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """Build a strict Structured Outputs request for the Responses API."""
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": int(max_output_tokens),
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": strict_json_schema(schema),
            }
        },
        "store": False,
        "prompt_cache_key": (
            f"taxonomy-{schema_name}-"
            + hashlib.sha256(
                f"{BATCH_PROMPT_VERSION}:{schema_name}:{model}".encode("utf-8")
            ).hexdigest()[:16]
        ),
    }


def build_responses_batch_line(
    *,
    custom_id: str,
    model: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    reasoning_effort: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": OPENAI_BATCH_ENDPOINT,
        "body": responses_request_body(
            model=model,
            prompt=prompt,
            schema_name=schema_name,
            schema=schema,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        ),
    }


def should_escalate_l1(hits: list[dict[str, Any]], min_gap: float) -> bool:
    if not hits:
        return True
    if float(hits[0].get("confidence") or 0.0) < 0.55:
        return True
    if len(hits) >= 2:
        gap = float(hits[0].get("confidence") or 0.0) - float(
            hits[1].get("confidence") or 0.0
        )
        return gap < max(0.0, float(min_gap))
    return False


def should_escalate_leaf(
    decision: dict[str, Any] | None,
    l1_hits: list[dict[str, Any]],
    *,
    min_confidence: float,
    min_l1_gap: float,
) -> bool:
    if not decision or not decision.get("evidence"):
        return True
    if float(decision.get("confidence") or 0.0) < float(min_confidence):
        return True
    if len(l1_hits) >= 2:
        gap = float(l1_hits[0].get("confidence") or 0.0) - float(
            l1_hits[1].get("confidence") or 0.0
        )
        if gap < max(0.0, float(min_l1_gap)):
            return True
    return False


class OpenAIBatchClient:
    """Small async REST client; no OpenAI SDK dependency is required."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com",
        timeout_seconds: int = 60,
        file_ready_timeout_seconds: float | None = None,
        file_ready_poll_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ):
        key = str(api_key or "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY or OPENAI_API is required for taxonomy Batch API")
        self.headers = {"Authorization": f"Bearer {key}"}
        self.base_url = str(base_url or "https://api.openai.com").rstrip("/")
        self.file_ready_timeout_seconds = max(
            1.0,
            float(
                timeout_seconds
                if file_ready_timeout_seconds is None
                else file_ready_timeout_seconds
            ),
        )
        self.file_ready_poll_seconds = max(0.0, float(file_ready_poll_seconds))
        self.client = client or httpx.AsyncClient(timeout=float(timeout_seconds))
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "OpenAIBatchClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def _json_request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self.client.request(
            method,
            f"{self.base_url}{path}",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
        )
        if response.is_error:
            raise RuntimeError(
                f"OpenAI {path} failed ({response.status_code}): "
                f"{_safe_error(response.text, 600)}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"OpenAI {path} returned a non-object response")
        return data

    async def upload_jsonl(self, content: bytes, filename: str) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}/v1/files",
            headers=self.headers,
            data={"purpose": "batch"},
            files={"file": (filename, content, "application/jsonl")},
        )
        if response.is_error:
            raise RuntimeError(
                f"OpenAI file upload failed ({response.status_code}): "
                f"{_safe_error(response.text, 600)}"
            )
        data = response.json()
        uploaded = data if isinstance(data, dict) else {}
        file_id = str(uploaded.get("id") or "")
        if not file_id:
            raise RuntimeError("OpenAI file upload returned no file id")
        return await self.wait_for_file_ready(file_id)

    async def retrieve_file(self, file_id: str) -> dict[str, Any]:
        return await self._json_request("GET", f"/v1/files/{file_id}")

    async def wait_for_file_ready(self, file_id: str) -> dict[str, Any]:
        """Wait until a Batch input file is visible and processed by OpenAI."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.file_ready_timeout_seconds
        last_status = "unknown"
        last_error = ""
        while True:
            try:
                file_object = await self.retrieve_file(file_id)
            except RuntimeError as error:
                if is_provider_blocked_error(error):
                    raise
                last_error = _safe_error(error)
            else:
                last_error = ""
                last_status = str(file_object.get("status") or "").strip().lower()
                # The status field is deprecated. A successfully retrieved object
                # without it is therefore considered ready for forward compatibility.
                if not last_status or last_status == OPENAI_FILE_READY_STATUS:
                    return file_object
                if last_status == OPENAI_FILE_FAILED_STATUS:
                    detail = file_object.get("status_details") or file_object.get("error")
                    raise RuntimeError(
                        "OpenAI batch input file processing failed: "
                        f"file_id={file_id} detail={_safe_error(detail)}"
                    )

            remaining = deadline - loop.time()
            if remaining <= 0:
                suffix = f" error={last_error}" if last_error else ""
                raise RuntimeError(
                    "OpenAI batch input file was not ready before timeout: "
                    f"file_id={file_id} status={last_status}{suffix}"
                )
            await asyncio.sleep(min(self.file_ready_poll_seconds, remaining))

    async def create_batch(
        self, input_file_id: str, *, metadata: dict[str, str]
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/v1/batches",
            payload={
                "input_file_id": input_file_id,
                "endpoint": OPENAI_BATCH_ENDPOINT,
                "completion_window": "24h",
                "metadata": metadata,
            },
        )

    async def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute one persisted Batch line through the synchronous Responses API."""
        return await self._json_request("POST", OPENAI_BATCH_ENDPOINT, payload=payload)

    async def retrieve_batch(self, batch_id: str) -> dict[str, Any]:
        return await self._json_request("GET", f"/v1/batches/{batch_id}")

    async def download_file(self, file_id: str) -> str:
        response = await self.client.get(
            f"{self.base_url}/v1/files/{file_id}/content", headers=self.headers
        )
        if response.is_error:
            raise RuntimeError(
                f"OpenAI output download failed ({response.status_code}): "
                f"{_safe_error(response.text, 600)}"
            )
        return response.text


async def load_batch_item(d1: Any, item_id: int) -> dict[str, Any]:
    rows = await d1.query(
        "SELECT * FROM taxonomy_batch_items WHERE id = ? LIMIT 1", [item_id]
    )
    return rows[0] if rows else {}


async def load_request_by_custom_id(d1: Any, custom_id: str) -> dict[str, Any]:
    rows = await d1.query(
        """
        SELECT r.*, i.tool_id, i.source_url, i.source_text,
               i.existing_entity_kind, i.existing_entity_source,
               i.current_stage AS item_current_stage,
               i.status AS item_status,
               EXISTS (
                 SELECT 1 FROM taxonomy_batch_requests newer
                 WHERE newer.item_id = r.item_id
                   AND newer.stage = r.stage
                   AND newer.attempt > r.attempt
               ) AS newer_attempt_exists
        FROM taxonomy_batch_requests r
        JOIN taxonomy_batch_items i ON i.id = r.item_id
        WHERE r.custom_id = ?
        LIMIT 1
        """,
        [custom_id],
    )
    return rows[0] if rows else {}


def stage_policy(config: Any, stage: str) -> tuple[str, str, int]:
    escalation = stage.endswith("_escalation")
    model = str(
        getattr(
            config,
            "taxonomy_batch_escalation_model" if escalation else "taxonomy_batch_model",
            "gpt-5.6-terra" if escalation else "gpt-5.6-luna",
        )
        or ("gpt-5.6-terra" if escalation else "gpt-5.6-luna")
    ).strip()
    effort_field = {
        "profile": "taxonomy_batch_profile_reasoning_effort",
        "l1": "taxonomy_batch_l1_reasoning_effort",
        "leaf": "taxonomy_batch_leaf_reasoning_effort",
        "capability": "taxonomy_batch_capability_reasoning_effort",
    }.get(stage.replace("_escalation", ""), "taxonomy_batch_leaf_reasoning_effort")
    if escalation:
        effort_field = "taxonomy_batch_escalation_reasoning_effort"
    default_effort = "high" if escalation or stage.startswith("leaf") else "medium"
    if stage == "l1":
        default_effort = "low"
    effort = str(getattr(config, effort_field, default_effort) or default_effort).strip()
    max_all = max(1024, int(getattr(config, "taxonomy_batch_max_output_tokens", 4096)))
    max_tokens = min(max_all, 2048) if stage.startswith("l1") else max_all
    return model, effort, max_tokens


def _stage_prompt_and_schema(
    item: dict[str, Any],
    catalog: TaxonomyCatalog,
    stage: str,
    *,
    capability_candidate_limit: int = 96,
) -> tuple[str, dict[str, Any]]:
    profile = _json_object(item.get("profile_json"))
    l1_raw = _json_object(item.get("l1_json"))
    leaf_raw = _json_object(item.get("leaf_json"))
    if stage == "profile":
        source_url = str(item.get("source_url") or "")
        source_text = str(item.get("source_text") or "")
        return (
            profile_extract_from_main_content_prompt(source_url, source_text),
            profile_extract_schema(),
        )
    if stage in {"l1", "l1_escalation"}:
        prompt = top2_l1_prompt(catalog.primary_roots(), catalog, profile)
        if stage == "l1_escalation":
            prompt += (
                "\n\nA cheaper model produced the following ambiguous result. Re-evaluate "
                "from the catalog and evidence; do not merely copy it:\n" + _json(l1_raw)
            )
        return prompt, top2_l1_schema()
    l1_hits = parse_top2_l1(l1_raw, catalog)
    pool = build_leaf_candidate_pool(l1_hits, catalog)
    if stage in {"leaf", "leaf_escalation"}:
        prompt = leaf_adjudication_prompt(
            pool,
            [hit["term"].slug for hit in l1_hits],
            catalog,
            profile,
        )
        if stage == "leaf_escalation":
            prompt += (
                "\n\nA cheaper model produced the following low-confidence or ambiguous "
                "leaf result. Re-adjudicate it from the evidence and binding catalog:\n"
                + _json(leaf_raw)
            )
        return prompt, leaf_adjudication_schema()
    if stage == "capability":
        metadata = _json_object(item.get("model_trace_json"))
        markets = [
            term
            for term in (
                catalog.get("primary_category", str(slug))
                for slug in metadata.get("existing_market_slugs") or []
            )
            if term is not None
        ]
        if not markets:
            primary = parse_leaf_decision(
                leaf_raw,
                pool,
                catalog,
                source_url=str(item.get("source_url") or ""),
                source_text=profile_evidence_text(profile),
            )
            secondary = parse_secondary_leaf_decisions(
                leaf_raw,
                pool,
                catalog,
                primary_slug=primary["term"].slug if primary else "",
                source_url=str(item.get("source_url") or ""),
                source_text=profile_evidence_text(profile),
            )
            markets = [
                decision["term"]
                for decision in ([primary] if primary else []) + secondary
            ]
        candidates = recall_capability_candidates(
            profile,
            catalog,
            markets=markets,
            limit=int(capability_candidate_limit),
        )
        return capabilities_prompt(candidates, catalog, profile), capabilities_schema()
    raise ValueError(f"unsupported taxonomy batch stage: {stage}")


async def enqueue_stage(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    *,
    item_id: int,
    stage: str,
) -> None:
    if stage not in STAGES:
        raise ValueError(f"invalid taxonomy batch stage: {stage}")
    item = await load_batch_item(d1, item_id)
    if not item:
        raise RuntimeError(f"taxonomy batch item {item_id} not found")
    model, effort, max_tokens = stage_policy(config, stage)
    prompt, schema = _stage_prompt_and_schema(
        item,
        catalog,
        stage,
        capability_candidate_limit=int(
            getattr(config, "taxonomy_capability_candidate_limit", 96)
        ),
    )
    existing = await d1.query(
        """
        SELECT id FROM taxonomy_batch_requests
        WHERE item_id = ? AND stage = ? AND status IN ('queued','submitted','succeeded')
        ORDER BY id DESC LIMIT 1
        """,
        [item_id, stage],
    )
    if existing:
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET current_stage = ?, status = 'running', retry_kind = NULL,
                retry_attempt = 0, next_retry_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            [stage, utc_now_iso(), item_id],
        )
        return
    attempt_rows = await d1.query(
        """
        SELECT COALESCE(MAX(attempt), 0) AS max_attempt
        FROM taxonomy_batch_requests
        WHERE item_id = ? AND stage = ?
        """,
        [item_id, stage],
    )
    attempt = int(attempt_rows[0].get("max_attempt") or 0) + 1 if attempt_rows else 1
    custom_id = f"taxonomy-{item_id}-{stage}-{attempt}"
    line = build_responses_batch_line(
        custom_id=custom_id,
        model=model,
        prompt=prompt,
        schema_name=f"taxonomy_{stage}",
        schema=schema,
        reasoning_effort=effort,
        max_output_tokens=max_tokens,
    )
    now = utc_now_iso()
    await d1.run(
        """
        INSERT INTO taxonomy_batch_requests (
          item_id, custom_id, stage, attempt, model, reasoning_effort,
          max_output_tokens, request_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
        """,
        [
            item_id,
            custom_id,
            stage,
            attempt,
            model,
            effort,
            max_tokens,
            _json(line),
            now,
            now,
        ],
    )
    await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET current_stage = ?, status = 'running', error = NULL,
            retry_kind = NULL, retry_attempt = 0, next_retry_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        [stage, now, item_id],
    )


async def _create_item(
    d1: Any,
    *,
    row: dict[str, Any],
    catalog: TaxonomyCatalog,
    source_url: str,
    source_text: str,
) -> int:
    now = utc_now_iso()
    meta = await d1.run(
        """
        INSERT INTO taxonomy_batch_items (
          tool_id, pipeline_version, prompt_version, taxonomy_version,
          status, current_stage, source_url, source_text, source_content_hash,
          existing_entity_kind, existing_entity_source, model_trace_json,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', 'profile', ?, ?, ?, ?, ?, '{}', ?, ?)
        ON CONFLICT(tool_id, pipeline_version, prompt_version, taxonomy_version)
        DO NOTHING
        """,
        [
            int(row.get("tool_id") or 0),
            BATCH_PIPELINE_VERSION,
            BATCH_PROMPT_VERSION,
            catalog.taxonomy_version,
            source_url,
            source_text,
            hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            str(row.get("entity_kind") or "unresolved"),
            str(row.get("entity_kind_source") or ""),
            now,
            now,
        ],
    )
    item_id = int(meta.get("last_row_id") or 0)
    if item_id:
        return item_id
    rows = await d1.query(
        """
        SELECT id FROM taxonomy_batch_items
        WHERE tool_id = ? AND pipeline_version = ? AND prompt_version = ?
          AND taxonomy_version = ?
        LIMIT 1
        """,
        [
            int(row.get("tool_id") or 0),
            BATCH_PIPELINE_VERSION,
            BATCH_PROMPT_VERSION,
            catalog.taxonomy_version,
        ],
    )
    return int(rows[0].get("id") or 0) if rows else 0


async def schedule_source_retry(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    item_id: int,
    error: Any,
) -> bool:
    """Schedule another homepage capture, or move the exhausted item to review."""
    item = await load_batch_item(d1, item_id)
    if not item:
        return False
    configured_max_attempts, _ = taxonomy_retry_policy(config)
    error_max_attempts = getattr(error, "max_attempts", None)
    try:
        max_attempts = min(
            configured_max_attempts,
            max(1, int(error_max_attempts)),
        ) if error_max_attempts is not None else configured_max_attempts
    except (TypeError, ValueError):
        max_attempts = configured_max_attempts
    retryable = bool(getattr(error, "retryable", True))
    failed_attempt = int(item.get("retry_attempt") or 0) + 1
    safe_error = f"source_fetch_failed:{_safe_error(error)}"
    if not retryable or failed_attempt >= max_attempts:
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET retry_attempt = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            [failed_attempt, safe_error, utc_now_iso(), item_id],
        )
        item = await load_batch_item(d1, item_id)
        await complete_without_leaf(
            d1,
            item,
            catalog,
            status="needs_review",
            error=(
                f"{safe_error}:non_retryable"
                if not retryable
                else f"{safe_error}:attempts_exhausted:{failed_attempt}"
            ),
        )
        return False
    delay = taxonomy_retry_delay_seconds(config, failed_attempt)
    error_retry_after = getattr(error, "retry_after_seconds", None)
    try:
        if error_retry_after is not None:
            delay = max(delay, min(86400, max(1, int(error_retry_after))))
    except (TypeError, ValueError):
        pass
    meta = await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET status = 'pending', current_stage = 'profile', retry_kind = 'source',
            retry_attempt = ?,
            next_retry_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),
            error = ?, completed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        [failed_attempt, f"+{delay} seconds", safe_error, utc_now_iso(), item_id],
    )
    return int(meta.get("changes") or 0) > 0


async def schedule_model_retry(
    d1: Any,
    config: Any,
    request: dict[str, Any],
    error: Any,
) -> bool:
    """Persist a bounded retry for the same model stage."""
    if not is_retryable_stage_error(error):
        return False
    attempt = max(1, int(request.get("attempt") or 1))
    max_attempts, _ = taxonomy_retry_policy(config)
    if attempt >= max_attempts:
        return False
    delay = taxonomy_retry_delay_seconds(config, attempt)
    meta = await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET status = 'pending', retry_kind = 'model', retry_attempt = ?,
            next_retry_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),
            error = ?, completed_at = NULL, updated_at = ?
        WHERE id = ? AND current_stage = ?
        """,
        [
            attempt,
            f"+{delay} seconds",
            f"{request.get('stage')}_failed:{_safe_error(error)}",
            utc_now_iso(),
            int(request.get("item_id") or 0),
            str(request.get("stage") or ""),
        ],
    )
    return int(meta.get("changes") or 0) > 0


async def resume_due_model_retries(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    *,
    limit: int = 500,
) -> int:
    rows = await d1.query(
        """
        SELECT id, current_stage
        FROM taxonomy_batch_items
        WHERE status = 'pending' AND retry_kind = 'model'
          AND next_retry_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ORDER BY next_retry_at, id
        LIMIT ?
        """,
        [max(1, int(limit))],
    )
    resumed = 0
    for row in rows:
        stage = str(row.get("current_stage") or "")
        if stage not in STAGES:
            continue
        await enqueue_stage(
            d1, config, catalog, item_id=int(row.get("id") or 0), stage=stage
        )
        resumed += 1
    return resumed


async def load_due_source_retry_tasks(
    d1: Any, *, catalog: TaxonomyCatalog, limit: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return await d1.query(
        """
        SELECT
          t.id AS tool_id,
          t.canonical_slug,
          t.normalized_domain,
          t.official_url,
          COALESCE((
            SELECT source.source_url
            FROM tool_sources source
            WHERE source.tool_id = t.id
              AND source.source_type = 'official_site'
              AND source.verification_status = 'verified'
              AND json_extract(source.raw_payload, '$.taxonomy_evidence') = 1
            ORDER BY source.confidence_score DESC, source.id DESC
            LIMIT 1
          ), t.official_url) AS taxonomy_evidence_url,
          t.entity_kind,
          t.entity_kind_source,
          pp.profile_json AS stored_profile_json,
          'source_retry' AS selection_mode,
          item.id AS existing_item_id
        FROM taxonomy_batch_items item
        JOIN tools t ON t.id = item.tool_id
        LEFT JOIN product_profiles pp ON pp.tool_id = t.id
        WHERE item.pipeline_version = ?
          AND item.prompt_version = ?
          AND item.taxonomy_version = ?
          AND item.status = 'pending'
          AND item.retry_kind = 'source'
          AND item.next_retry_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
          AND t.status IN ('pending_enrich', 'pending_review', 'published')
          AND t.duplicate_of_tool_id IS NULL
          AND trim(COALESCE(t.normalized_domain, '')) <> ''
          AND (
            t.entity_kind IN ('independent_product', 'app_or_extension')
            OR (
              t.entity_kind = 'unresolved'
              AND COALESCE(t.entity_kind_source, '') <> 'manual'
            )
          )
        ORDER BY item.next_retry_at, item.id
        LIMIT ?
        """,
        [
            BATCH_PIPELINE_VERSION,
            BATCH_PROMPT_VERSION,
            catalog.taxonomy_version,
            int(limit),
        ],
    )


async def load_new_batch_tasks(
    d1: Any, *, catalog: TaxonomyCatalog, limit: int
) -> list[dict[str, Any]]:
    return await d1.query(
        """
        SELECT
          t.id AS tool_id,
          t.canonical_slug,
          t.normalized_domain,
          t.official_url,
          COALESCE((
            SELECT source.source_url
            FROM tool_sources source
            WHERE source.tool_id = t.id
              AND source.source_type = 'official_site'
              AND source.verification_status = 'verified'
              AND json_extract(source.raw_payload, '$.taxonomy_evidence') = 1
            ORDER BY source.confidence_score DESC, source.id DESC
            LIMIT 1
          ), t.official_url) AS taxonomy_evidence_url,
          t.entity_kind,
          t.entity_kind_source,
          pp.profile_json AS stored_profile_json,
          'full' AS selection_mode
        FROM tools t
        LEFT JOIN product_profiles pp ON pp.tool_id = t.id
        WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
          AND t.duplicate_of_tool_id IS NULL
          AND trim(COALESCE(t.normalized_domain, '')) <> ''
          AND (
            t.entity_kind IN ('independent_product', 'app_or_extension')
            OR (
              t.entity_kind = 'unresolved'
              AND COALESCE(t.entity_kind_source, '') <> 'manual'
            )
          )
          AND NOT EXISTS (
            SELECT 1
            FROM product_taxonomy_assignments trusted_primary
            JOIN taxonomy_terms trusted_term
              ON trusted_term.id = trusted_primary.term_id
             AND trusted_term.dimension = 'primary_category'
             AND trusted_term.status = 'active'
            WHERE trusted_primary.tool_id = t.id
              AND trusted_primary.is_primary = 1
              AND trusted_primary.decision_status IN ('verified', 'auto_accepted')
              AND NOT EXISTS (
                SELECT 1 FROM taxonomy_terms active_child
                WHERE active_child.parent_id = trusted_term.id
                  AND active_child.status = 'active'
              )
          )
          AND NOT EXISTS (
            SELECT 1 FROM taxonomy_batch_items item
            WHERE item.tool_id = t.id
              AND item.pipeline_version = ?
              AND item.prompt_version = ?
              AND item.taxonomy_version = ?
          )
          AND NOT EXISTS (
            SELECT 1 FROM classification_runs completed
            WHERE completed.tool_id = t.id
              AND completed.prompt_version = ?
              AND completed.run_status IN ('succeeded', 'partial', 'skipped')
          )
        ORDER BY t.id ASC
        LIMIT ?
        """,
        [
            BATCH_PIPELINE_VERSION,
            BATCH_PROMPT_VERSION,
            catalog.taxonomy_version,
            BATCH_PROMPT_VERSION,
            max(1, int(limit)),
        ],
    )


async def load_capability_only_batch_tasks(
    d1: Any, *, catalog: TaxonomyCatalog, limit: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return await d1.query(
        """
        SELECT
          t.id AS tool_id,
          t.canonical_slug,
          t.normalized_domain,
          t.official_url,
          COALESCE(json_extract(pp.profile_json, '$.source_url'), t.official_url)
            AS taxonomy_evidence_url,
          t.entity_kind,
          t.entity_kind_source,
          pp.profile_json AS stored_profile_json,
          'capability_only' AS selection_mode,
          COALESCE((
            SELECT json_group_array(market.slug)
            FROM (
              SELECT existing_term.slug AS slug
              FROM product_taxonomy_assignments existing_market
              JOIN taxonomy_terms existing_term
                ON existing_term.id = existing_market.term_id
               AND existing_term.dimension = 'primary_category'
               AND existing_term.status = 'active'
              WHERE existing_market.tool_id = t.id
                AND existing_market.decision_status IN (
                  'verified', 'auto_accepted', 'provisional'
                )
              ORDER BY existing_market.is_primary DESC,
                       existing_market.confidence DESC,
                       existing_market.id DESC
              LIMIT 4
            ) market
          ), '[]') AS existing_market_slugs_json
        FROM tools t
        JOIN product_profiles pp ON pp.tool_id = t.id
        WHERE t.status IN ('pending_enrich', 'pending_review', 'published')
          AND t.duplicate_of_tool_id IS NULL
          AND t.entity_kind IN ('independent_product', 'app_or_extension')
          AND json_valid(pp.profile_json) = 1
          AND json_extract(pp.profile_json, '$.extractor_version') = ?
          AND COALESCE(
            json_array_length(json_extract(pp.profile_json, '$.capabilities_raw')),
            0
          ) > 0
          AND EXISTS (
            SELECT 1
            FROM product_taxonomy_assignments trusted_primary
            JOIN taxonomy_terms trusted_term
              ON trusted_term.id = trusted_primary.term_id
             AND trusted_term.dimension = 'primary_category'
             AND trusted_term.status = 'active'
            WHERE trusted_primary.tool_id = t.id
              AND trusted_primary.is_primary = 1
              AND trusted_primary.decision_status IN ('verified', 'auto_accepted')
              AND NOT EXISTS (
                SELECT 1 FROM taxonomy_terms active_child
                WHERE active_child.parent_id = trusted_term.id
                  AND active_child.status = 'active'
              )
          )
          AND NOT EXISTS (
            SELECT 1
            FROM product_taxonomy_assignments existing_capability
            JOIN taxonomy_terms capability_term
              ON capability_term.id = existing_capability.term_id
             AND capability_term.dimension = 'capability'
             AND capability_term.status = 'active'
            WHERE existing_capability.tool_id = t.id
              AND existing_capability.decision_status IN (
                'verified', 'auto_accepted', 'provisional'
              )
          )
          AND NOT EXISTS (
            SELECT 1 FROM taxonomy_batch_items item
            WHERE item.tool_id = t.id
              AND item.pipeline_version = ?
              AND item.prompt_version = ?
              AND item.taxonomy_version = ?
          )
        ORDER BY t.id ASC
        LIMIT ?
        """,
        [
            SHADOW_EXTRACTOR_VERSION,
            BATCH_PIPELINE_VERSION,
            BATCH_PROMPT_VERSION,
            catalog.taxonomy_version,
            int(limit),
        ],
    )


async def seed_batch_items(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    *,
    limit: int,
) -> dict[str, int]:
    from runner import (
        AssetTask,
        CloudflareBrowserRunAssetClient,
        classify_page_state,
        extract_homepage_main_text,
    )

    source_retry_rows = await load_due_source_retry_tasks(
        d1, catalog=catalog, limit=limit
    )
    remaining = max(0, int(limit) - len(source_retry_rows))
    full_rows = await load_new_batch_tasks(d1, catalog=catalog, limit=remaining)
    remaining = max(0, remaining - len(full_rows))
    capability_rows = (
        await load_capability_only_batch_tasks(
            d1, catalog=catalog, limit=remaining
        )
        if bool(getattr(config, "taxonomy_capabilities_enabled", True))
        and bool(getattr(config, "taxonomy_capability_backfill_enabled", True))
        and remaining > 0
        else []
    )
    rows = [*source_retry_rows, *full_rows, *capability_rows]
    browser = CloudflareBrowserRunAssetClient(config)
    counts = {
        "selected": len(rows),
        "prepared": 0,
        "source_retry_selected": len(source_retry_rows),
        "source_retry_scheduled": 0,
        "source_retry_exhausted": 0,
        "profile_reused": 0,
        "capability_only_selected": len(capability_rows),
        "capability_only_prepared": 0,
        "capability_only_without_candidates": 0,
        "source_failed": 0,
    }
    for row in rows:
        tool_id = int(row.get("tool_id") or 0)
        task = AssetTask(
            tool_id=tool_id,
            canonical_slug=str(row.get("canonical_slug") or f"tool-{tool_id}"),
            normalized_domain=str(row.get("normalized_domain") or ""),
            official_url=str(row.get("taxonomy_evidence_url") or row.get("official_url") or ""),
            attempts=0,
            max_attempts=1,
            generation=0,
            lease_token="openai-taxonomy-batch",
        )
        stored_profile = _json_object(row.get("stored_profile_json"))
        if str(row.get("selection_mode") or "") == "capability_only":
            try:
                market_slugs = json.loads(
                    str(row.get("existing_market_slugs_json") or "[]")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                market_slugs = []
            market_slugs = [
                str(slug)
                for slug in market_slugs
                if catalog.get("primary_category", str(slug))
            ][:4]
            item_id = await _create_item(
                d1,
                row=row,
                catalog=catalog,
                source_url=str(
                    stored_profile.get("source_url") or task.official_url or ""
                ),
                source_text="",
            )
            if item_id:
                await d1.run(
                    """
                    UPDATE taxonomy_batch_items
                    SET profile_json = ?, source_text = NULL,
                        model_trace_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    [
                        _json(stored_profile),
                        _json(
                            {
                                "mode": "capability_only",
                                "profile_reused": True,
                                "existing_market_slugs": market_slugs,
                            }
                        ),
                        utc_now_iso(),
                        item_id,
                    ],
                )
                market_terms = [
                    term
                    for term in (
                        catalog.get("primary_category", slug)
                        for slug in market_slugs
                    )
                    if term is not None
                ]
                candidates = recall_capability_candidates(
                    stored_profile,
                    catalog,
                    markets=market_terms,
                    limit=int(
                        getattr(config, "taxonomy_capability_candidate_limit", 96)
                    ),
                )
                if candidates:
                    await enqueue_stage(
                        d1, config, catalog, item_id=item_id, stage="capability"
                    )
                    counts["capability_only_prepared"] += 1
                else:
                    await finalize_capability_only(
                        d1,
                        config,
                        catalog,
                        item_id,
                    )
                    counts["capability_only_without_candidates"] += 1
                counts["prepared"] += 1
            continue
        stored_entity = resolve_entity_decision(
            stored_profile.get("entity_decision") or {},
            existing_kind=str(row.get("entity_kind") or "unresolved"),
            existing_source=str(row.get("entity_kind_source") or ""),
        )
        profile_entity_is_product = (
            bool(stored_entity.get("accepted"))
            and str(stored_entity.get("kind") or "")
            in {"independent_product", "app_or_extension"}
        )
        existing_entity_is_product = str(row.get("entity_kind") or "") in {
            "independent_product",
            "app_or_extension",
        }
        reusable_profile = (
            profile_has_signal(stored_profile)
            and str(stored_profile.get("extractor_version") or "")
            == SHADOW_EXTRACTOR_VERSION
            and (profile_entity_is_product or existing_entity_is_product)
        )
        if reusable_profile:
            stored_profile["entity_decision"] = stored_entity
            item_id = await _create_item(
                d1,
                row=row,
                catalog=catalog,
                source_url=str(
                    stored_profile.get("source_url")
                    or task.official_url
                    or ""
                ),
                source_text="",
            )
            if item_id:
                await d1.run(
                    """
                    UPDATE taxonomy_batch_items
                    SET profile_json = ?, source_text = NULL,
                        model_trace_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    [
                        _json(stored_profile),
                        _json({"profile_reused": True}),
                        utc_now_iso(),
                        item_id,
                    ],
                )
                await enqueue_stage(
                    d1, config, catalog, item_id=item_id, stage="l1"
                )
                counts["profile_reused"] += 1
                counts["prepared"] += 1
            continue
        try:
            source_url, html_body = await browser.fetch_homepage_content(task)
            assessment = classify_page_state(html_body)
            if not assessment.is_valid:
                raise RuntimeError(
                    f"page_quality_gate:{assessment.state}:{assessment.reason}"
                )
            source_text = extract_homepage_main_text(
                html_body,
                limit=int(getattr(config, "taxonomy_main_content_max_chars", 10000)),
            )
            if not source_text.strip():
                raise RuntimeError("homepage content contained no usable main content")
            if str(row.get("selection_mode") or "") == "source_retry":
                item_id = int(row.get("existing_item_id") or 0)
                await d1.run(
                    """
                    UPDATE taxonomy_batch_items
                    SET source_url = ?, source_text = ?, source_content_hash = ?,
                        error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    [
                        source_url,
                        source_text,
                        hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                        utc_now_iso(),
                        item_id,
                    ],
                )
            else:
                item_id = await _create_item(
                    d1,
                    row=row,
                    catalog=catalog,
                    source_url=source_url,
                    source_text=source_text,
                )
            if item_id:
                await enqueue_stage(d1, config, catalog, item_id=item_id, stage="profile")
                counts["prepared"] += 1
        except Exception as error:
            counts["source_failed"] += 1
            from runner import log_error

            log_error(
                "taxonomy_batch.source_failed",
                tool_id=tool_id,
                error=str(error)[:500],
            )
            item_id = int(row.get("existing_item_id") or 0)
            if not item_id:
                item_id = await _create_item(
                    d1,
                    row=row,
                    catalog=catalog,
                    source_url=str(task.official_url or ""),
                    source_text="",
                )
            if item_id:
                scheduled = await schedule_source_retry(
                    d1, config, catalog, item_id, error
                )
                if scheduled:
                    counts["source_retry_scheduled"] += 1
                else:
                    counts["source_retry_exhausted"] += 1
    return counts


def _request_id_chunks(requests: list[dict[str, Any]]) -> list[list[int]]:
    request_ids = [int(row.get("id") or 0) for row in requests]
    if any(request_id <= 0 for request_id in request_ids):
        raise RuntimeError("taxonomy batch request reservation contains an invalid id")
    return [
        request_ids[offset : offset + D1_REQUEST_RESERVATION_CHUNK_SIZE]
        for offset in range(0, len(request_ids), D1_REQUEST_RESERVATION_CHUNK_SIZE)
    ]


async def _reserve_requests_for_job(
    d1: Any,
    *,
    job_id: int,
    requests: list[dict[str, Any]],
    now: str,
) -> None:
    """Fence requests before the irreversible OpenAI Batch creation call."""
    try:
        for request_ids in _request_id_chunks(requests):
            placeholders = ",".join("?" for _ in request_ids)
            await d1.run(
                f"""
                UPDATE taxonomy_batch_requests
                SET job_id = ?, updated_at = ?
                WHERE id IN ({placeholders})
                  AND status = 'queued' AND job_id IS NULL
                """,
                [job_id, now, *request_ids],
            )
        rows = await d1.query(
            """
            SELECT COUNT(*) AS reserved_count
            FROM taxonomy_batch_requests
            WHERE job_id = ? AND status = 'queued'
            """,
            [job_id],
        )
        reserved_count = int(rows[0].get("reserved_count") or 0) if rows else 0
        if reserved_count != len(requests):
            raise RuntimeError(
                "taxonomy_batch_request_reservation_incomplete: "
                f"expected={len(requests)} reserved={reserved_count}"
            )
    except Exception:
        await d1.run(
            """
            UPDATE taxonomy_batch_requests
            SET job_id = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            [now, job_id],
        )
        raise


async def _mark_job_requests_submitted(d1: Any, job_id: int, now: str) -> None:
    await d1.run(
        """
        UPDATE taxonomy_batch_requests
        SET status = 'submitted', submitted_at = COALESCE(submitted_at, ?),
            updated_at = ?
        WHERE job_id = ? AND status = 'queued'
        """,
        [now, now, job_id],
    )


async def _persist_remote_batch_job(
    d1: Any,
    *,
    job_id: int,
    batch_id: str,
    input_file_id: str,
    status: str,
    batch: dict[str, Any],
    now: str,
) -> None:
    """Retry the idempotent local checkpoint after remote Batch creation."""
    last_error: Exception | None = None
    for attempt in range(D1_SUBMISSION_PERSIST_ATTEMPTS):
        try:
            await d1.run(
                """
                UPDATE taxonomy_batch_jobs
                SET openai_batch_id = ?, input_file_id = ?, status = ?, raw_json = ?,
                    submitted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                [batch_id, input_file_id, status, _json(batch), now, now, job_id],
            )
            return
        except Exception as error:
            last_error = error
            if attempt < D1_SUBMISSION_PERSIST_ATTEMPTS - 1:
                await asyncio.sleep(0.25 * (2**attempt))
    try:
        await d1.run(
            """
            UPDATE taxonomy_batch_jobs
            SET openai_batch_id = ?, input_file_id = ?, status = ?,
                submitted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            [batch_id, input_file_id, status, now, now, job_id],
        )
        return
    except Exception as fallback_error:
        raise RuntimeError(
            "remote_batch_created_but_checkpoint_failed: "
            f"batch_id={batch_id} error={_safe_error(fallback_error)}"
        ) from (last_error or fallback_error)


async def submit_queued_batches(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    openai: OpenAIBatchClient,
) -> dict[str, int]:
    limit = max(1, int(getattr(config, "taxonomy_batch_request_limit", 500)))
    rows = await d1.query(
        """
        SELECT * FROM taxonomy_batch_requests
        WHERE status = 'queued' AND job_id IS NULL
        ORDER BY stage, model, reasoning_effort, id
        LIMIT ?
        """,
        [limit],
    )
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("stage") or ""),
            str(row.get("model") or ""),
            str(row.get("reasoning_effort") or ""),
        )
        grouped.setdefault(key, []).append(row)
    counts = {
        "batches_submitted": 0,
        "requests_submitted": 0,
        "submit_failed": 0,
        "submit_retries_scheduled": 0,
        "reservation_failed": 0,
        "submission_persist_failed": 0,
        "provider_blocked": 0,
    }
    for (stage, model, effort), requests in grouped.items():
        now = utc_now_iso()
        meta = await d1.run(
            """
            INSERT INTO taxonomy_batch_jobs (
              stage, model, reasoning_effort, status, request_count,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'building', ?, ?, ?)
            """,
            [stage, model, effort, len(requests), now, now],
        )
        job_id = int(meta.get("last_row_id") or 0)
        try:
            await _reserve_requests_for_job(
                d1, job_id=job_id, requests=requests, now=now
            )
        except Exception as error:
            counts["submit_failed"] += len(requests)
            counts["reservation_failed"] += len(requests)
            await d1.run(
                """
                UPDATE taxonomy_batch_jobs
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                [_safe_error(error), now, now, job_id],
            )
            from runner import log_error

            log_error(
                "taxonomy_batch.reservation_failed",
                local_job_id=job_id,
                stage=stage,
                model=model,
                request_count=len(requests),
                error=str(error)[:500],
            )
            continue

        remote_batch_created = False
        try:
            jsonl = "\n".join(str(row.get("request_json") or "") for row in requests) + "\n"
            uploaded = await openai.upload_jsonl(
                jsonl.encode("utf-8"), f"taxonomy-{stage}-{job_id}.jsonl"
            )
            input_file_id = str(uploaded.get("id") or "")
            if not input_file_id:
                raise RuntimeError("OpenAI file upload returned no file id")
            batch = await openai.create_batch(
                input_file_id,
                metadata={
                    "pipeline": BATCH_PIPELINE_VERSION[:64],
                    "stage": stage[:64],
                    "local_job_id": str(job_id),
                },
            )
            batch_id = str(batch.get("id") or "")
            if not batch_id:
                raise RuntimeError("OpenAI batch creation returned no batch id")
            remote_batch_created = True
            status = str(batch.get("status") or "validating")
            await _persist_remote_batch_job(
                d1,
                job_id=job_id,
                batch_id=batch_id,
                input_file_id=input_file_id,
                status=status,
                batch=batch,
                now=now,
            )
            counts["batches_submitted"] += 1
            counts["requests_submitted"] += len(requests)
            try:
                await _mark_job_requests_submitted(d1, job_id, now)
            except Exception as error:
                counts["submission_persist_failed"] += len(requests)
                from runner import log_error

                log_error(
                    "taxonomy_batch.request_checkpoint_deferred",
                    local_job_id=job_id,
                    openai_batch_id=batch_id,
                    stage=stage,
                    model=model,
                    request_count=len(requests),
                    error=str(error)[:500],
                )
        except Exception as error:
            if remote_batch_created:
                counts["batches_submitted"] += 1
                counts["requests_submitted"] += len(requests)
                counts["submission_persist_failed"] += len(requests)
                from runner import log_error

                log_error(
                    "taxonomy_batch.remote_checkpoint_failed",
                    local_job_id=job_id,
                    stage=stage,
                    model=model,
                    request_count=len(requests),
                    error=str(error)[:500],
                )
                continue
            counts["submit_failed"] += len(requests)
            if is_provider_blocked_error(error):
                counts["provider_blocked"] = 1
            await d1.run(
                """
                UPDATE taxonomy_batch_jobs
                SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                [_safe_error(error), now, now, job_id],
            )
            if counts["provider_blocked"]:
                await d1.run(
                    """
                    UPDATE taxonomy_batch_requests
                    SET job_id = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    [now, job_id],
                )
            else:
                await d1.run(
                    """
                    UPDATE taxonomy_batch_requests
                    SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    [_safe_error(error), now, now, job_id],
                )
                for request in requests:
                    outcome = await _handle_stage_failure(
                        d1, config, catalog, request, _safe_error(error)
                    )
                    if outcome == "retry_scheduled":
                        counts["submit_retries_scheduled"] += 1
            from runner import log_error

            log_error(
                "taxonomy_batch.submit_failed",
                stage=stage,
                model=model,
                request_count=len(requests),
                error=str(error)[:500],
            )
            if counts["provider_blocked"]:
                break
    return counts


async def _request_trace(d1: Any, item_id: int) -> list[dict[str, Any]]:
    rows = await d1.query(
        """
        SELECT stage, attempt, model, reasoning_effort, status,
               input_tokens, cached_input_tokens, cache_write_tokens,
               output_tokens, reasoning_tokens, total_tokens, error
        FROM taxonomy_batch_requests
        WHERE item_id = ?
        ORDER BY id
        """,
        [item_id],
    )
    return [dict(row) for row in rows]


async def complete_without_leaf(
    d1: Any,
    item: dict[str, Any],
    catalog: TaxonomyCatalog,
    *,
    status: str,
    error: str,
) -> None:
    item_id = int(item.get("id") or 0)
    tool_id = int(item.get("tool_id") or 0)
    trace = await _request_trace(d1, item_id)
    raw = {
        "pipeline": BATCH_PIPELINE_VERSION,
        "prompt_version": BATCH_PROMPT_VERSION,
        "taxonomy_version": catalog.taxonomy_version,
        "profile": _json_object(item.get("profile_json")),
        "l1_raw": _json_object(item.get("l1_json")),
        "leaf_raw": _json_object(item.get("leaf_json")),
        "model_trace": trace,
        "profile_reused": not any(row.get("stage") == "profile" for row in trace),
        "error": error,
    }
    run_id = int(item.get("classification_run_id") or 0)
    classification_status = "partial" if status == "needs_review" else status
    if not run_id:
        run_id = await insert_classification_run(
            d1,
            tool_id=tool_id,
            taxonomy_version=catalog.taxonomy_version,
            run_status=classification_status,
            provider="openai_batch_api",
            model_name=trace[-1]["model"] if trace else "",
            raw_output=raw,
            error=error,
            prompt_version=BATCH_PROMPT_VERSION,
            extractor_version=SHADOW_EXTRACTOR_VERSION,
        )
    now = utc_now_iso()
    await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET status = ?, current_stage = 'complete', error = ?,
            classification_run_id = ?, model_trace_json = ?,
            source_text = NULL, retry_kind = NULL, next_retry_at = NULL,
            completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        [status, error, run_id or None, _json(trace), now, now, item_id],
    )


async def finalize_capability_only(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    item_id: int,
    *,
    capability_error: str = "",
) -> None:
    """Persist capability backfill without replacing a trusted existing primary."""
    item = await load_batch_item(d1, item_id)
    if not item:
        return
    metadata = _json_object(item.get("model_trace_json"))
    profile = _json_object(item.get("profile_json"))
    cap_raw = _json_object(item.get("capability_json"))
    market_slugs = [
        str(slug) for slug in metadata.get("existing_market_slugs") or []
    ][:4]
    markets = [
        term
        for term in (
            catalog.get("primary_category", slug) for slug in market_slugs
        )
        if term is not None
    ]
    candidates = recall_capability_candidates(
        profile,
        catalog,
        markets=markets,
        limit=int(getattr(config, "taxonomy_capability_candidate_limit", 96)),
    )
    capabilities = (
        parse_capabilities(
            cap_raw,
            catalog,
            source_url=str(item.get("source_url") or ""),
            source_text=profile_evidence_text(
                profile, keys=("capabilities_raw",)
            ),
            whitelist_terms=candidates,
        )
        if cap_raw
        else []
    )
    error = capability_error
    if not error and not candidates:
        error = "capability_candidates_empty"
    if not error and not capabilities:
        error = "capability_empty"
    run_status = "failed" if capability_error else (
        "succeeded" if capabilities else "partial"
    )
    trace = await _request_trace(d1, item_id)
    raw = {
        "pipeline": BATCH_PIPELINE_VERSION,
        "prompt_version": BATCH_PROMPT_VERSION,
        "extractor_version": SHADOW_EXTRACTOR_VERSION,
        "taxonomy_version": catalog.taxonomy_version,
        "capability_backfill": 1,
        "primary_preserved": True,
        "profile": profile,
        "source_url": str(item.get("source_url") or ""),
        "existing_market_slugs": market_slugs,
        "capability_candidate_recall": {
            "catalog_count": len(catalog.capabilities()),
            "candidate_count": len(candidates),
            "candidate_limit": int(
                getattr(config, "taxonomy_capability_candidate_limit", 96)
            ),
            "slugs": [term.slug for term in candidates],
        },
        "capabilities_raw_model": cap_raw,
        "capabilities_accepted": [
            {
                "slug": decision["term"].slug,
                "role": decision["role"],
                "confidence": decision["confidence"],
            }
            for decision in capabilities
        ],
        "model_trace": trace,
        "error": error or None,
    }
    run_id = int(item.get("classification_run_id") or 0)
    if not run_id:
        run_id = await insert_classification_run(
            d1,
            tool_id=int(item.get("tool_id") or 0),
            taxonomy_version=catalog.taxonomy_version,
            run_status="partial" if capabilities else run_status,
            provider="openai_batch_api",
            model_name=trace[-1]["model"] if trace else "",
            candidate_terms={
                "existing_markets": market_slugs,
                "capability_candidates": [term.slug for term in candidates],
            },
            raw_output=raw,
            error=error or None,
            prompt_version=BATCH_PROMPT_VERSION,
            extractor_version=SHADOW_EXTRACTOR_VERSION,
        )
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET classification_run_id = ?, updated_at = ? WHERE id = ?
            """,
            [run_id or None, utc_now_iso(), item_id],
        )
    for decision in capabilities:
        await upsert_assignment(
            d1,
            tool_id=int(item.get("tool_id") or 0),
            term_id=decision["term"].term_id,
            run_id=run_id or None,
            is_primary=False,
            confidence=float(decision["confidence"]),
            decision_status="provisional",
            evidence={
                "role": decision["role"],
                "evidence": decision.get("evidence") or [],
                "backfill": True,
            },
            source="auto",
        )
    if capabilities:
        await supersede_auto_assignments(
            d1,
            int(item.get("tool_id") or 0),
            dimensions=["capability"],
            exclude_run_id=run_id or None,
        )
        await update_classification_run_status(
            d1,
            run_id,
            run_status=run_status,
            error=error or None,
        )
    now = utc_now_iso()
    await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET status = ?, current_stage = 'complete', error = ?,
            classification_run_id = ?, model_trace_json = ?, source_text = NULL,
            retry_kind = NULL, next_retry_at = NULL, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            run_status,
            error or None,
            run_id or None,
            _json(trace),
            now,
            now,
            item_id,
        ],
    )


async def finalize_classification(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    item_id: int,
    *,
    capability_error: str = "",
) -> None:
    item = await load_batch_item(d1, item_id)
    if not item:
        return
    metadata = _json_object(item.get("model_trace_json"))
    if metadata.get("mode") == "capability_only":
        await finalize_capability_only(
            d1,
            config,
            catalog,
            item_id,
            capability_error=capability_error,
        )
        return
    if item.get("current_stage") == "complete" and item.get("status") in {
        "succeeded",
        "partial",
    }:
        return
    profile = _json_object(item.get("profile_json"))
    l1_raw = _json_object(item.get("l1_json"))
    leaf_raw = _json_object(item.get("leaf_json"))
    cap_raw = _json_object(item.get("capability_json"))
    l1_hits = parse_top2_l1(l1_raw, catalog)
    pool = build_leaf_candidate_pool(l1_hits, catalog)
    source_url = str(item.get("source_url") or "")
    evidence_text = profile_evidence_text(profile)
    primary = parse_leaf_decision(
        leaf_raw,
        pool,
        catalog,
        source_url=source_url,
        source_text=evidence_text,
    )
    if not primary:
        await complete_without_leaf(
            d1,
            item,
            catalog,
            status="needs_review",
            error="no_valid_primary_leaf_after_escalation",
        )
        return
    secondary = parse_secondary_leaf_decisions(
        leaf_raw,
        pool,
        catalog,
        primary_slug=primary["term"].slug,
        source_url=source_url,
        source_text=evidence_text,
    )
    markets = [decision["term"] for decision in [primary, *secondary]]
    cap_candidates = recall_capability_candidates(
        profile,
        catalog,
        markets=markets,
        limit=int(getattr(config, "taxonomy_capability_candidate_limit", 96)),
    )
    capabilities = parse_capabilities(
        cap_raw,
        catalog,
        source_url=source_url,
        source_text=profile_evidence_text(profile, keys=("capabilities_raw",)),
        whitelist_terms=cap_candidates,
    ) if cap_raw else []
    trace = await _request_trace(d1, item_id)
    raw = {
        "pipeline": BATCH_PIPELINE_VERSION,
        "prompt_version": BATCH_PROMPT_VERSION,
        "extractor_version": SHADOW_EXTRACTOR_VERSION,
        "taxonomy_version": catalog.taxonomy_version,
        "profile": profile,
        "l1_raw": l1_raw,
        "l1_accepted": [
            {
                "slug": hit["term"].slug,
                "confidence": hit["confidence"],
                "reason": hit["reason"],
            }
            for hit in l1_hits
        ],
        "leaf_raw": leaf_raw,
        "leaf_accepted": {
            "slug": primary["term"].slug,
            "confidence": primary["confidence"],
            "reason": primary["reason"],
        },
        "secondary_markets_accepted": [
            {
                "slug": decision["term"].slug,
                "confidence": decision["confidence"],
                "reason": decision["reason"],
            }
            for decision in secondary
        ],
        "capability_candidate_recall": {
            "catalog_count": len(catalog.capabilities()),
            "candidate_count": len(cap_candidates),
            "slugs": [term.slug for term in cap_candidates],
        },
        "capabilities_raw_model": cap_raw,
        "capabilities_accepted": [
            {
                "slug": decision["term"].slug,
                "role": decision["role"],
                "confidence": decision["confidence"],
            }
            for decision in capabilities
        ],
        "model_trace": trace,
        "profile_reused": not any(row.get("stage") == "profile" for row in trace),
    }
    if capability_error:
        raw["capabilities_error"] = capability_error
    run_status = "partial" if capability_error else "succeeded"
    run_id = int(item.get("classification_run_id") or 0)
    if not run_id:
        run_id = await insert_classification_run(
            d1,
            tool_id=int(item.get("tool_id") or 0),
            taxonomy_version=catalog.taxonomy_version,
            run_status="partial",
            provider="openai_batch_api",
            model_name=trace[-1]["model"] if trace else "",
            candidate_terms={
                "l1": raw["l1_accepted"],
                "leaf_pool": [term.slug for term in pool],
            },
            raw_output=raw,
            error=capability_error or None,
            prompt_version=BATCH_PROMPT_VERSION,
            extractor_version=SHADOW_EXTRACTOR_VERSION,
        )
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET classification_run_id = ?, updated_at = ? WHERE id = ?
            """,
            [run_id or None, utc_now_iso(), item_id],
        )

    tool_id = int(item.get("tool_id") or 0)
    manual_primary_id = await load_verified_manual_primary_term_id(d1, tool_id)
    is_primary, primary_status = auto_primary_write_state(
        float(primary["confidence"]),
        manual_primary_id,
        float(getattr(config, "taxonomy_auto_accept_confidence", 0.5)),
    )
    await upsert_assignment(
        d1,
        tool_id=tool_id,
        term_id=primary["term"].term_id,
        run_id=run_id or None,
        is_primary=is_primary,
        confidence=float(primary["confidence"]),
        decision_status=primary_status,
        evidence={
            "role": "primary_market",
            "reason": primary.get("reason"),
            "l1_candidates": raw["l1_accepted"],
            "evidence": primary.get("evidence") or [],
            "profile_primary_job": profile.get("primary_job"),
        },
        source="auto",
    )
    for decision in secondary:
        await upsert_assignment(
            d1,
            tool_id=tool_id,
            term_id=decision["term"].term_id,
            run_id=run_id or None,
            is_primary=False,
            confidence=float(decision["confidence"]),
            decision_status="provisional",
            evidence={
                "role": "secondary_market",
                "reason": decision.get("reason"),
                "evidence": decision.get("evidence") or [],
            },
            source="auto",
        )
    for decision in capabilities:
        await upsert_assignment(
            d1,
            tool_id=tool_id,
            term_id=decision["term"].term_id,
            run_id=run_id or None,
            is_primary=False,
            confidence=float(decision["confidence"]),
            decision_status="provisional",
            evidence={
                "role": decision.get("role") or "supporting",
                "evidence": decision.get("evidence") or [],
            },
            source="auto",
        )
    dimensions = ["primary_category"]
    if cap_raw:
        dimensions.append("capability")
    await supersede_auto_assignments(
        d1,
        tool_id,
        dimensions=dimensions,
        exclude_run_id=run_id or None,
    )
    await update_classification_run_status(
        d1, run_id, run_status=run_status, error=capability_error or None
    )
    now = utc_now_iso()
    await d1.run(
        """
        UPDATE taxonomy_batch_items
        SET status = ?, current_stage = 'complete', error = ?,
            classification_run_id = ?, model_trace_json = ?, source_text = NULL,
            retry_kind = NULL, next_retry_at = NULL, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            run_status,
            capability_error or None,
            run_id or None,
            _json(trace),
            now,
            now,
            item_id,
        ],
    )


async def _prepare_capability_or_finalize(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    item_id: int,
) -> None:
    item = await load_batch_item(d1, item_id)
    profile = _json_object(item.get("profile_json"))
    if not bool(getattr(config, "taxonomy_capabilities_enabled", True)):
        await finalize_classification(d1, config, catalog, item_id)
        return
    if not profile.get("capabilities_raw"):
        await finalize_classification(d1, config, catalog, item_id)
        return
    l1_hits = parse_top2_l1(_json_object(item.get("l1_json")), catalog)
    pool = build_leaf_candidate_pool(l1_hits, catalog)
    leaf_raw = _json_object(item.get("leaf_json"))
    primary = parse_leaf_decision(
        leaf_raw,
        pool,
        catalog,
        source_url=str(item.get("source_url") or ""),
        source_text=profile_evidence_text(profile),
    )
    secondary = parse_secondary_leaf_decisions(
        leaf_raw,
        pool,
        catalog,
        primary_slug=primary["term"].slug if primary else "",
        source_url=str(item.get("source_url") or ""),
        source_text=profile_evidence_text(profile),
    )
    markets = [decision["term"] for decision in ([primary] if primary else []) + secondary]
    candidates = recall_capability_candidates(
        profile,
        catalog,
        markets=markets,
        limit=int(getattr(config, "taxonomy_capability_candidate_limit", 96)),
    )
    if not candidates:
        await finalize_classification(d1, config, catalog, item_id)
        return
    await enqueue_stage(d1, config, catalog, item_id=item_id, stage="capability")


async def _handle_stage_failure(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    request: dict[str, Any],
    error: str,
) -> str:
    item_id = int(request.get("item_id") or 0)
    stage = str(request.get("stage") or "")
    item = await load_batch_item(d1, item_id)
    if not item:
        return "missing_item"
    retryable = is_retryable_stage_error(error)
    if retryable and await schedule_model_retry(d1, config, request, error):
        return "retry_scheduled"
    if not retryable:
        if stage == "capability":
            await finalize_classification(
                d1,
                config,
                catalog,
                item_id,
                capability_error=f"capability_request_invalid:{error}",
            )
            return "completed_with_capability_error"
        await complete_without_leaf(
            d1,
            item,
            catalog,
            status="needs_review",
            error=f"{stage}_request_invalid:{error}",
        )
        return "needs_review"
    if stage == "l1":
        await enqueue_stage(d1, config, catalog, item_id=item_id, stage="l1_escalation")
        return "escalated"
    if stage == "leaf":
        await enqueue_stage(d1, config, catalog, item_id=item_id, stage="leaf_escalation")
        return "escalated"
    if stage == "capability":
        await finalize_classification(
            d1, config, catalog, item_id, capability_error=f"capability_failed:{error}"
        )
        return "completed_with_capability_error"
    await complete_without_leaf(
        d1,
        item,
        catalog,
        status="needs_review",
        error=f"{stage}_failed_after_retries:{error}",
    )
    return "needs_review"


async def apply_request_result(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    result: ParsedBatchResult,
) -> bool:
    request = await load_request_by_custom_id(d1, result.custom_id)
    if not request:
        return False
    if int(request.get("newer_attempt_exists") or 0) > 0:
        return False
    if (
        str(request.get("item_current_stage") or "")
        != str(request.get("stage") or "")
        or str(request.get("item_status") or "")
        in {"succeeded", "partial", "failed", "skipped", "needs_review"}
    ):
        return False
    now = utc_now_iso()
    usage = result.usage
    if not result.ok:
        await d1.run(
            """
            UPDATE taxonomy_batch_requests
            SET status = 'failed', response_json = ?, error = ?,
                input_tokens = ?, cached_input_tokens = ?, cache_write_tokens = ?,
                output_tokens = ?, reasoning_tokens = ?, total_tokens = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            [
                _json(result.response_body) if result.response_body else None,
                result.error,
                usage["input_tokens"],
                usage["cached_input_tokens"],
                usage["cache_write_tokens"],
                usage["output_tokens"],
                usage["reasoning_tokens"],
                usage["total_tokens"],
                now,
                now,
                int(request["id"]),
            ],
        )
        await _handle_stage_failure(d1, config, catalog, request, result.error)
        return True
    await d1.run(
        """
        UPDATE taxonomy_batch_requests
        SET status = 'succeeded', response_json = ?, structured_output_json = ?,
            input_tokens = ?, cached_input_tokens = ?, cache_write_tokens = ?,
            output_tokens = ?, reasoning_tokens = ?, total_tokens = ?,
            completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            _json(result.response_body),
            _json(result.structured_output),
            usage["input_tokens"],
            usage["cached_input_tokens"],
            usage["cache_write_tokens"],
            usage["output_tokens"],
            usage["reasoning_tokens"],
            usage["total_tokens"],
            now,
            now,
            int(request["id"]),
        ],
    )
    item_id = int(request.get("item_id") or 0)
    item = await load_batch_item(d1, item_id)
    stage = str(request.get("stage") or "")
    output = result.structured_output
    if stage == "profile":
        source_text = str(item.get("source_text") or "")
        profile = build_product_profile(
            output,
            source_url=str(item.get("source_url") or ""),
            source_text=source_text,
        )
        entity = resolve_entity_decision(
            profile.get("entity_decision") or {},
            existing_kind=str(item.get("existing_entity_kind") or "unresolved"),
            existing_source=str(item.get("existing_entity_source") or ""),
        )
        profile["entity_decision"] = entity
        await upsert_product_profile(d1, int(item.get("tool_id") or 0), profile)
        if entity.get("source") == "auto":
            await update_tool_entity_kind(
                d1, int(item.get("tool_id") or 0), str(entity.get("kind") or "")
            )
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET profile_json = ?, source_text = NULL, updated_at = ?
            WHERE id = ?
            """,
            [_json(profile), now, item_id],
        )
        item = await load_batch_item(d1, item_id)
        eligible = bool(entity.get("accepted")) and str(entity.get("kind") or "") in {
            "independent_product",
            "app_or_extension",
        }
        if eligible and profile_has_signal(profile):
            await enqueue_stage(d1, config, catalog, item_id=item_id, stage="l1")
        else:
            status = "skipped" if entity.get("accepted") else "partial"
            await complete_without_leaf(
                d1,
                item,
                catalog,
                status=status,
                error="entity_ineligible_or_profile_without_signal",
            )
        return True
    if stage in {"l1", "l1_escalation"}:
        await d1.run(
            "UPDATE taxonomy_batch_items SET l1_json = ?, updated_at = ? WHERE id = ?",
            [_json(output), now, item_id],
        )
        hits = parse_top2_l1(output, catalog)
        if stage == "l1" and should_escalate_l1(
            hits, float(getattr(config, "taxonomy_batch_l1_min_gap", 0.08))
        ):
            await enqueue_stage(
                d1, config, catalog, item_id=item_id, stage="l1_escalation"
            )
        elif not hits:
            item = await load_batch_item(d1, item_id)
            await complete_without_leaf(
                d1,
                item,
                catalog,
                status="needs_review",
                error="no_valid_l1_after_escalation",
            )
        else:
            await enqueue_stage(d1, config, catalog, item_id=item_id, stage="leaf")
        return True
    if stage in {"leaf", "leaf_escalation"}:
        await d1.run(
            "UPDATE taxonomy_batch_items SET leaf_json = ?, updated_at = ? WHERE id = ?",
            [_json(output), now, item_id],
        )
        item = await load_batch_item(d1, item_id)
        hits = parse_top2_l1(_json_object(item.get("l1_json")), catalog)
        pool = build_leaf_candidate_pool(hits, catalog)
        profile = _json_object(item.get("profile_json"))
        decision = parse_leaf_decision(
            output,
            pool,
            catalog,
            source_url=str(item.get("source_url") or ""),
            source_text=profile_evidence_text(profile),
        )
        escalate = should_escalate_leaf(
            decision,
            hits,
            min_confidence=float(
                getattr(config, "taxonomy_batch_leaf_min_confidence", 0.60)
            ),
            min_l1_gap=float(getattr(config, "taxonomy_batch_l1_min_gap", 0.08)),
        )
        if stage == "leaf" and escalate:
            await enqueue_stage(
                d1, config, catalog, item_id=item_id, stage="leaf_escalation"
            )
        elif not decision:
            await complete_without_leaf(
                d1,
                item,
                catalog,
                status="needs_review",
                error="no_valid_primary_leaf_after_escalation",
            )
        else:
            await _prepare_capability_or_finalize(d1, config, catalog, item_id)
        return True
    if stage == "capability":
        await d1.run(
            """
            UPDATE taxonomy_batch_items
            SET capability_json = ?, updated_at = ? WHERE id = ?
            """,
            [_json(output), now, item_id],
        )
        await finalize_classification(d1, config, catalog, item_id)
        return True
    return False


async def _fail_unreturned_requests(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    job_id: int,
    seen_custom_ids: set[str],
    reason: str,
) -> int:
    rows = await d1.query(
        """
        SELECT * FROM taxonomy_batch_requests
        WHERE job_id = ? AND status = 'submitted'
        ORDER BY id
        """,
        [job_id],
    )
    failed = 0
    for row in rows:
        custom_id = str(row.get("custom_id") or "")
        if custom_id in seen_custom_ids:
            continue
        parsed = ParsedBatchResult(
            custom_id=custom_id,
            ok=False,
            response_body={},
            structured_output={},
            usage={
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
            error=reason,
        )
        if await apply_request_result(d1, config, catalog, parsed):
            failed += 1
    return failed


async def execute_job_via_responses(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    openai: OpenAIBatchClient,
    *,
    job_id: int,
    batch_error: str,
) -> dict[str, int]:
    """Recover a validator-level file outage through direct Responses calls.

    The request JSON stored in D1 is the canonical payload for both transports.
    Model calls run concurrently, while state-machine writes are applied in order
    so a fallback uses the same result handling and retry rules as normal Batch
    output.
    """
    rows = await d1.query(
        """
        SELECT * FROM taxonomy_batch_requests
        WHERE job_id = ? AND status = 'submitted'
        ORDER BY id
        """,
        [job_id],
    )
    counts = {
        "sync_fallback_requests": len(rows),
        "sync_fallback_completed": 0,
        "sync_fallback_failed": 0,
        "provider_blocked": 0,
    }
    if not rows:
        return counts

    concurrency = max(
        1,
        min(8, int(getattr(config, "taxonomy_concurrency", 3) or 3)),
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def execute(row: dict[str, Any]) -> ParsedBatchResult:
        custom_id = str(row.get("custom_id") or "")
        async with semaphore:
            try:
                line = _json_object(row.get("request_json"))
                payload = line.get("body")
                if not isinstance(payload, dict) or not payload:
                    raise RuntimeError("sync_fallback_request_body_missing")
                body = await openai.create_response(payload)
                return parse_batch_output_line(
                    {
                        "custom_id": custom_id,
                        "response": {"status_code": 200, "body": body},
                    }
                )
            except Exception as error:
                return ParsedBatchResult(
                    custom_id=custom_id,
                    ok=False,
                    response_body={},
                    structured_output={},
                    usage={
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 0,
                    },
                    error=f"sync_responses_fallback_failed: {_safe_error(error)}",
                )

    results = await asyncio.gather(*(execute(dict(row)) for row in rows))
    for result in results:
        if is_provider_blocked_error(result.error):
            counts["provider_blocked"] = 1
        if not await apply_request_result(d1, config, catalog, result):
            continue
        key = "sync_fallback_completed" if result.ok else "sync_fallback_failed"
        counts[key] += 1

    now = utc_now_iso()
    processed = counts["sync_fallback_completed"] + counts["sync_fallback_failed"]
    final_status = "completed" if processed == len(rows) else "failed"
    await d1.run(
        """
        UPDATE taxonomy_batch_jobs
        SET status = ?, completed_count = ?, failed_count = ?,
            error = ?, completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            final_status,
            counts["sync_fallback_completed"],
            counts["sync_fallback_failed"],
            _safe_error(
                f"{batch_error}; sync_responses_fallback="
                f"{counts['sync_fallback_completed']}/{len(rows)}"
            ),
            now,
            now,
            job_id,
        ],
    )
    return counts


async def poll_active_batches(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
    openai: OpenAIBatchClient,
) -> dict[str, int]:
    rows = await d1.query(
        """
        SELECT * FROM taxonomy_batch_jobs
        WHERE openai_batch_id IS NOT NULL
          AND (
            status IN ('validating', 'in_progress', 'finalizing', 'cancelling')
            OR EXISTS (
              SELECT 1 FROM taxonomy_batch_requests request
              WHERE request.job_id = taxonomy_batch_jobs.id
                AND request.status IN ('queued', 'submitted')
            )
          )
        ORDER BY id
        """
    )
    counts = {
        "batches_polled": len(rows),
        "batches_completed": 0,
        "batch_requests_completed": 0,
        "batch_requests_failed": 0,
        "sync_fallback_jobs": 0,
        "sync_fallback_requests": 0,
        "sync_fallback_completed": 0,
        "sync_fallback_failed": 0,
        "provider_blocked": 0,
    }
    for job in rows:
        job_id = int(job.get("id") or 0)
        batch_id = str(job.get("openai_batch_id") or "")
        try:
            await _mark_job_requests_submitted(
                d1,
                job_id,
                str(job.get("submitted_at") or utc_now_iso()),
            )
            batch = await openai.retrieve_batch(batch_id)
            status = str(batch.get("status") or job.get("status") or "in_progress")
            failure_error = (
                batch_failure_error(batch, status)
                if status in FAILED_BATCH_STATUSES
                else None
            )
            request_counts = (
                batch.get("request_counts")
                if isinstance(batch.get("request_counts"), dict)
                else {}
            )
            now = utc_now_iso()
            await d1.run(
                """
                UPDATE taxonomy_batch_jobs
                SET status = ?, output_file_id = ?, error_file_id = ?,
                    completed_count = ?, failed_count = ?, raw_json = ?,
                    error = ?,
                    completed_at = CASE WHEN ? IN ('completed','failed','expired','cancelled')
                      THEN ? ELSE completed_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                [
                    status,
                    batch.get("output_file_id"),
                    batch.get("error_file_id"),
                    int(request_counts.get("completed") or 0),
                    int(request_counts.get("failed") or 0),
                    _json(batch),
                    failure_error,
                    status,
                    now,
                    now,
                    job_id,
                ],
            )
            if status == "completed":
                seen: set[str] = set()
                output_file_id = str(batch.get("output_file_id") or "")
                if output_file_id:
                    content = await openai.download_file(output_file_id)
                    for line in content.splitlines():
                        if not line.strip():
                            continue
                        parsed = parse_batch_output_line(line)
                        if not parsed.ok and is_provider_blocked_error(parsed.error):
                            counts["provider_blocked"] = 1
                        if parsed.custom_id:
                            seen.add(parsed.custom_id)
                        if await apply_request_result(d1, config, catalog, parsed):
                            key = (
                                "batch_requests_completed"
                                if parsed.ok
                                else "batch_requests_failed"
                            )
                            counts[key] += 1
                counts["batch_requests_failed"] += await _fail_unreturned_requests(
                    d1,
                    config,
                    catalog,
                    job_id,
                    seen,
                    "batch_completed_without_output_line",
                )
                counts["batches_completed"] += 1
            elif status in FAILED_BATCH_STATUSES:
                can_fallback = callable(getattr(openai, "create_response", None))
                if can_fallback and is_batch_input_file_access_error(failure_error):
                    fallback = await execute_job_via_responses(
                        d1,
                        config,
                        catalog,
                        openai,
                        job_id=job_id,
                        batch_error=failure_error or f"openai_batch_{status}",
                    )
                    counts["sync_fallback_jobs"] += 1
                    for key in (
                        "sync_fallback_requests",
                        "sync_fallback_completed",
                        "sync_fallback_failed",
                    ):
                        counts[key] += int(fallback.get(key) or 0)
                    counts["batch_requests_completed"] += int(
                        fallback.get("sync_fallback_completed") or 0
                    )
                    counts["batch_requests_failed"] += int(
                        fallback.get("sync_fallback_failed") or 0
                    )
                    counts["provider_blocked"] = max(
                        counts["provider_blocked"],
                        int(fallback.get("provider_blocked") or 0),
                    )
                    counts["batches_completed"] += 1
                else:
                    counts["batch_requests_failed"] += await _fail_unreturned_requests(
                        d1,
                        config,
                        catalog,
                        job_id,
                        set(),
                        failure_error or f"openai_batch_{status}",
                    )
        except Exception as error:
            from runner import log_error

            log_error(
                "taxonomy_batch.poll_failed",
                local_job_id=job_id,
                openai_batch_id=batch_id,
                error=str(error)[:500],
            )
            if is_provider_blocked_error(error):
                counts["provider_blocked"] = 1
    return counts


async def reconcile_recorded_results(
    d1: Any,
    config: Any,
    catalog: TaxonomyCatalog,
) -> int:
    """Finish a stage whose result was recorded before a process interruption."""
    rows = await d1.query(
        """
        SELECT r.*
        FROM taxonomy_batch_requests r
        JOIN taxonomy_batch_items i ON i.id = r.item_id
        WHERE r.status IN ('succeeded', 'failed')
          AND i.status = 'running'
          AND i.current_stage = r.stage
          AND NOT EXISTS (
            SELECT 1 FROM taxonomy_batch_requests newer
            WHERE newer.item_id = r.item_id
              AND newer.stage = r.stage
              AND newer.attempt > r.attempt
          )
        ORDER BY r.id
        LIMIT 500
        """
    )
    reconciled = 0
    for row in rows:
        usage = {
            "input_tokens": int(row.get("input_tokens") or 0),
            "cached_input_tokens": int(row.get("cached_input_tokens") or 0),
            "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
        }
        parsed = ParsedBatchResult(
            custom_id=str(row.get("custom_id") or ""),
            ok=str(row.get("status") or "") == "succeeded",
            response_body=_json_object(row.get("response_json")),
            structured_output=_json_object(row.get("structured_output_json")),
            usage=usage,
            error=str(row.get("error") or "recorded_stage_failed"),
        )
        if await apply_request_result(d1, config, catalog, parsed):
            reconciled += 1
    return reconciled


async def run_openai_taxonomy_batch_once(
    config: Any, limit: int | None = None
) -> dict[str, int]:
    """Poll prior jobs, prepare fresh tools and submit all newly queued stages."""
    from runner import D1Client, log_info

    if not str(getattr(config, "openai_api_key", "") or "").strip():
        raise RuntimeError("OPENAI_API_KEY or OPENAI_API is required for taxonomy Batch API")
    effective_limit = max(1, int(limit or getattr(config, "taxonomy_limit", 50)))
    counts: dict[str, int] = {}
    async with D1Client(config) as d1:
        catalog = await load_taxonomy_catalog(d1)
        if not catalog.primary_roots():
            raise RuntimeError("active taxonomy catalog is empty")
        async with OpenAIBatchClient(
            str(config.openai_api_key),
            base_url=str(getattr(config, "openai_base_url", "https://api.openai.com")),
            timeout_seconds=int(getattr(config, "taxonomy_batch_timeout_seconds", 90)),
        ) as openai:
            counts["recorded_results_reconciled"] = await reconcile_recorded_results(
                d1, config, catalog
            )
            poll_counts = await poll_active_batches(d1, config, catalog, openai)
            counts.update(poll_counts)
            if not int(poll_counts.get("provider_blocked") or 0):
                counts.update(
                    await seed_batch_items(
                        d1, config, catalog, limit=effective_limit
                    )
                )
                counts["model_retries_resumed"] = await resume_due_model_retries(
                    d1, config, catalog
                )
                counts.update(
                    await submit_queued_batches(d1, config, catalog, openai)
                )
        active_rows = await d1.query(
            """
            SELECT COUNT(*) AS count FROM taxonomy_batch_jobs
            WHERE status IN ('validating','in_progress','finalizing','cancelling')
            """
        )
        counts["active_batches"] = int(active_rows[0].get("count") or 0) if active_rows else 0
    log_info("taxonomy_batch.pass", **counts)
    return counts
