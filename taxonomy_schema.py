"""Shared strict schema normalization for taxonomy inference transports."""
import json
from typing import Any


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize existing schemas to OpenAI strict Structured Outputs rules.

    Strict mode requires every property of an object to be listed in
    ``required``. Existing Browser Rendering schemas intentionally allowed a
    few evidence metadata fields to be absent, so both transports ask the model to emit
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


