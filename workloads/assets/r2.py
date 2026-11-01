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
from runner import Config, log_debug, log_error, log_info, sha256_text

class R2AssetUploader:
    def __init__(self, config: Config):
        if not config.r2_access_key_id or not config.r2_secret_access_key:
            raise RuntimeError("Missing CLOUDFLARE_R2_ACCESS_KEY_ID or CLOUDFLARE_R2_SECRET_ACCESS_KEY.")
        if not config.r2_bucket:
            raise RuntimeError("Missing CLOUDFLARE_R2_BUCKET.")
        self.account_id = config.cloudflare_account_id
        self.access_key_id = config.r2_access_key_id
        self.secret_access_key = config.r2_secret_access_key
        self.bucket = config.r2_bucket

    def signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_access_key).encode("utf-8")
        for value in (date_stamp, "auto", "s3", "aws4_request"):
            key = hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
        return key

    async def put_object(self, key: str, body: bytes, content_type: str) -> None:
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        canonical_uri = f"/{quote(self.bucket, safe='')}/{quote(key, safe='/-_.~')}"
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "content-type": content_type,
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(self.signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.put(f"https://{host}{canonical_uri}", headers=headers, content=body)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"R2 upload failed for bucket {self.bucket}: HTTP {response.status_code} {response.text[:300]}")

    async def check_access(self) -> None:
        await self.put_object("_runner-healthcheck.txt", b"ok\n", "text/plain")


