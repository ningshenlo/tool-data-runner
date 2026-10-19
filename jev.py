"""Bounded TypeSafe choice client; no implicit retries or provider-controlled endpoints."""
import json
import math
from typing import Any

import httpx

JEV_MODEL = "jev-1.13.0"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class JevError(ValueError):
    pass


def choice(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def validate_response(request: dict, response: Any) -> dict:
    if not isinstance(response, dict) or response.get("model") != request["model"] or not isinstance(response.get("answers"), dict):
        raise JevError("jev_invalid_response")
    for key, question in request["questions"].items():
        answer = response["answers"].get(key)
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise JevError("jev_invalid_answer")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]) or answer.get("choice") not in probabilities:
            raise JevError("jev_invalid_choice")
        for value in [*probabilities.values(), answer.get("confidence")]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise JevError("jev_invalid_probabilities")
        if abs(sum(probabilities.values()) - 1) > 0.03 or probabilities[answer["choice"]] + 0.001 < max(probabilities.values()):
            raise JevError("jev_invalid_probabilities")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
        raise JevError("jev_invalid_usage")
    return response


async def call_jev(client: httpx.AsyncClient, key: str, request: dict, timeout: float = 8) -> dict:
    if not key.strip():
        raise JevError("jev_key_missing")
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > 110_000:
        raise JevError("jev_request_too_large")
    try:
        async with client.stream("POST", JEV_ENDPOINT, content=body,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                 timeout=timeout, follow_redirects=False) as response:
            if response.status_code != 200:
                raise JevError(f"jev_http_{response.status_code}")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 262_144:
                    raise JevError("jev_response_too_large")
            return validate_response(request, json.loads(chunks))
    except JevError:
        raise
    except (httpx.HTTPError, ValueError, UnicodeError):
        # Avoid logging credentials, raw provider responses or arbitrary network messages.
        raise JevError("jev_transport_or_json_error") from None
