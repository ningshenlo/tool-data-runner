"""Small text / crypto helpers."""

from __future__ import annotations

import hashlib
import random
import re
import string
from typing import Any


def generate_session_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def d1_request_metadata(body: dict[str, Any], operation: str | None = None) -> dict[str, Any]:
    statements: list[str] = []
    request_kind = "unknown"
    if isinstance(body.get("sql"), str):
        request_kind = "query"
        statements.append(str(body["sql"]))
    elif isinstance(body.get("batch"), list):
        request_kind = "batch"
        statements.extend(
            str(statement.get("sql") or "")
            for statement in body["batch"]
            if isinstance(statement, dict)
        )

    normalized_statements = [re.sub(r"\s+", " ", sql).strip() for sql in statements if sql.strip()]
    sql_verbs: list[str] = []
    for sql in normalized_statements:
        match = re.match(r"(?:--[^\n]*\n\s*)*([A-Za-z]+)", sql)
        verb = match.group(1).upper() if match else "UNKNOWN"
        if verb not in sql_verbs:
            sql_verbs.append(verb)
    fingerprint_source = "\n".join(normalized_statements)
    return {
        "operation": operation or f"d1.{request_kind}",
        "request_kind": request_kind,
        "statement_count": len(normalized_statements),
        "sql_verbs": sql_verbs,
        "sql_fingerprint": sha256_text(fingerprint_source)[:16] if fingerprint_source else "none",
    }
