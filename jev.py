"""Bounded choice client supporting TypeSafe SystemOne and OpenRouter perplexity/pplx-decider-v1.2."""
import json
import math
import os
from typing import Any

import httpx

JEV_MODEL = "jev-1.13.0"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "perplexity/pplx-decider-v1.2"


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


def _is_openrouter_call(key: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    openrouter_key = (
        key.strip() if key.strip().startswith("sk-or-")
        else os.getenv("OPENROUTER_API_KEY", "").strip() or os.getenv("openrouter_api_key", "").strip()
    )
    if openrouter_key:
        return True, openrouter_key
    if "openrouter.ai" in str(getattr(client, "base_url", "")):
        return True, key.strip()
    return False, ""


async def _call_openrouter(client: httpx.AsyncClient, key: str, request: dict, timeout: float = 12) -> dict:
    model = os.getenv("OPENROUTER_MODEL", OPENROUTER_MODEL).strip() or OPENROUTER_MODEL
    state = request.get("state") or {}
    questions = request.get("questions") or {}

    prompt_parts = [
        "You are an AI decision engine for product classification. Analyze the product evidence and answer each multiple-choice question by selecting one criterion key.",
        "Return strictly a JSON object: {\"answers\": {\"<question_id>\": {\"choice\": \"<selected_key>\", \"confidence\": <float between 0.0 and 1.0>}}}",
        "\n--- PRODUCT STATE ---",
    ]
    if state.get("primary_job"):
        prompt_parts.append(f"Primary Job: {state['primary_job']}")
    if state.get("negative_claims"):
        prompt_parts.append(f"Negative Claims: {state['negative_claims']}")
    
    evidence = state.get("evidence") or []
    if evidence:
        prompt_parts.append("\nEvidence Quotes:")
        for item in evidence:
            snippet_id = item.get("id", "")
            quote = item.get("quote", "")
            val = item.get("value", "")
            prompt_parts.append(f"[{snippet_id}]: {quote} (tag: {val})")

    prompt_parts.append("\n--- QUESTIONS TO ANSWER ---")
    for q_id, q_data in questions.items():
        prompt_parts.append(f"\nQuestion ID: {q_id}")
        prompt_parts.append(f"Instruction: {q_data.get('instructions', '')}")
        prompt_parts.append("Options (pick one key):")
        for c_key, c_val in (q_data.get("criteria") or {}).items():
            prompt_parts.append(f"  - {c_key}: {c_val}")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise classification engine. You must output JSON matching the required schema with answers for every question ID."
            },
            {
                "role": "user",
                "content": "\n".join(prompt_parts)
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://sigpik.com",
        "X-Title": "Sigpik Intelligence"
    }

    try:
        response = await client.post(
            OPENROUTER_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        if response.status_code != 200:
            raise JevError(f"jev_http_{response.status_code}")
        
        data = response.json()
        raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        parsed = json.loads(raw_content)
        raw_answers = parsed.get("answers") if isinstance(parsed.get("answers"), dict) else parsed

        answers: dict[str, Any] = {}
        for q_id, q_data in questions.items():
            criteria = q_data.get("criteria") or {}
            criteria_keys = list(criteria.keys())
            ans = raw_answers.get(q_id)
            if isinstance(ans, dict) and "choice" in ans:
                selected_choice = str(ans["choice"])
                confidence = float(ans.get("confidence", 0.95))
            elif isinstance(ans, str):
                selected_choice = ans
                confidence = 0.95
            else:
                selected_choice = "none" if "none" in criteria else (criteria_keys[0] if criteria_keys else "unknown")
                confidence = 0.6

            if selected_choice not in criteria:
                selected_choice = "none" if "none" in criteria else (criteria_keys[0] if criteria_keys else "unknown")
                confidence = 0.6

            confidence = max(0.0, min(1.0, confidence))
            probs = {k: 0.001 for k in criteria_keys}
            probs[selected_choice] = max(0.9, confidence)
            total = sum(probs.values())
            probs = {k: round(v / total, 4) for k, v in probs.items()}
            # normalize difference for sum = 1
            diff = 1.0 - sum(probs.values())
            probs[selected_choice] = round(probs[selected_choice] + diff, 4)

            answers[q_id] = {
                "type": "choice",
                "choice": selected_choice,
                "confidence": confidence,
                "probabilities": probs,
            }

        usage = data.get("usage") or {}
        return {
            "model": request.get("model", model),
            "answers": answers,
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
            }
        }
    except JevError:
        raise
    except Exception as exc:
        raise JevError("jev_transport_or_json_error") from exc


async def call_jev(client: httpx.AsyncClient, key: str, request: dict, timeout: float = 8) -> dict:
    if not key.strip() and not os.getenv("OPENROUTER_API_KEY", "").strip():
        raise JevError("jev_key_missing")

    is_openrouter, openrouter_key = _is_openrouter_call(key, client)
    if is_openrouter:
        return await _call_openrouter(client, openrouter_key, request, timeout=timeout)

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
