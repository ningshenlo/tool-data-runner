"""Structured JSON logging for runner processes."""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_LOG_LEVEL_PRIORITY = {"debug": 10, "info": 20, "error": 40}
_LOG_LEVEL = (os.getenv("RUNNER_LOG_LEVEL") or "info").strip().lower()
if _LOG_LEVEL not in _LOG_LEVEL_PRIORITY:
    _LOG_LEVEL = "info"
_LOG_CONTEXT: dict[str, Any] = {
    "service": (os.getenv("RUNNER_SERVICE_NAME") or "tool-data-runner").strip(),
}
LOG_WORKLOAD: ContextVar[str | None] = ContextVar("runner_log_workload", default=None)


def configure_logging(service: str, instance_id: str, workloads: tuple[str, ...]) -> None:
    global _LOG_LEVEL
    configured_level = (os.getenv("RUNNER_LOG_LEVEL") or _LOG_LEVEL).strip().lower()
    _LOG_LEVEL = configured_level if configured_level in _LOG_LEVEL_PRIORITY else "info"
    _LOG_CONTEXT.clear()
    _LOG_CONTEXT.update(
        {
            "service": service,
            "instance_id": instance_id,
            "workloads": list(workloads),
        }
    )


def emit_log(level: str, message: str, fields: dict[str, Any]) -> None:
    if _LOG_LEVEL_PRIORITY[level] < _LOG_LEVEL_PRIORITY[_LOG_LEVEL]:
        return
    workload = LOG_WORKLOAD.get()
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": level,
        **_LOG_CONTEXT,
        **({"workload": workload} if workload else {}),
        "message": message,
        **fields,
    }
    print(
        json.dumps(payload, ensure_ascii=False),
        file=sys.stderr if level == "error" else sys.stdout,
        flush=True,
    )


def log_debug(message: str, **fields: Any) -> None:
    emit_log("debug", message, fields)


def log_info(message: str, **fields: Any) -> None:
    emit_log("info", message, fields)


def log_error(message: str, **fields: Any) -> None:
    emit_log("error", message, fields)


def log_task_result(message: str, status: str, **fields: Any) -> None:
    logger = log_debug if status in {"done", "succeeded"} else log_info
    logger(message, status=status, **fields)


def mask_value(value: str, prefix: int = 18, suffix: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix + 3:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def redact_log_text(value: str) -> str:
    import re

    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+", "Bearer <redacted>", value)
    redacted = re.sub(
        r'(?i)("?(?:authorization|api[_-]?token|password|secret|cookie)"?\s*[:=]\s*")([^"\r\n]+)(")',
        r"\1<redacted>\3",
        redacted,
    )
    return redacted
