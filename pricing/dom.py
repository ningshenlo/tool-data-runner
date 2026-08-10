"""Deterministic, evidence-addressable DOM maps for pricing snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any


DOM_MAP_VERSION = "pricing-dom-v1"
_SPACE_RE = re.compile(r"\s+")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_NON_VISIBLE_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_KEPT_ATTRIBUTES = frozenset(
    {
        "id",
        "class",
        "role",
        "aria-label",
        "aria-checked",
        "aria-selected",
        "data-plan",
        "data-plan-id",
        "data-price-id",
        "data-testid",
        "itemprop",
        "itemtype",
        "name",
        "type",
    }
)
_STRUCTURED_SCRIPT_TYPES = frozenset(
    {
        "application/ld+json",
        "application/json",
        "application/schema+json",
    }
)


def normalize_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip()


@dataclass(frozen=True, slots=True)
class DomNode:
    node_id: str
    tag: str
    parent_id: str | None
    attributes: tuple[tuple[str, str], ...]
    text: str
    selector_hint: str
    table_row: int | None = None
    table_column: int | None = None

    def attribute(self, name: str) -> str | None:
        return dict(self.attributes).get(name)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "node_id": self.node_id,
            "tag": self.tag,
            "parent_id": self.parent_id,
            "attributes": dict(self.attributes),
            "text": self.text,
            "selector_hint": self.selector_hint,
        }
        if self.table_row is not None:
            payload["table_row"] = self.table_row
        if self.table_column is not None:
            payload["table_column"] = self.table_column
        return payload


@dataclass(frozen=True, slots=True)
class StructuredDataBlock:
    node_id: str
    script_type: str
    raw: str
    parsed: Any | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "script_type": self.script_type,
            "raw": self.raw,
            "parsed": self.parsed,
        }


@dataclass(frozen=True, slots=True)
class PricingDomMap:
    nodes: tuple[DomNode, ...]
    structured_data: tuple[StructuredDataBlock, ...]
    visible_text: str
    version: str = DOM_MAP_VERSION

    def node_by_id(self, node_id: str) -> DomNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

    def descendants(self, root_node_id: str) -> tuple[DomNode, ...]:
        included = {root_node_id}
        descendants: list[DomNode] = []
        for node in self.nodes:
            if node.node_id == root_node_id or node.parent_id in included:
                included.add(node.node_id)
                descendants.append(node)
        return tuple(descendants)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "visible_text": self.visible_text,
            "nodes": [node.as_dict() for node in self.nodes],
            "structured_data": [block.as_dict() for block in self.structured_data],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def structured_data_json(self) -> str:
        payload = {
            "version": self.version,
            "blocks": [block.as_dict() for block in self.structured_data],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class _PricingDomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict[str, Any]] = []
        self.stack: list[int] = []
        self.structured_data: list[StructuredDataBlock] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        normalized_attrs = {name.lower(): value or "" for name, value in attrs}
        parent_index = self.stack[-1] if self.stack else None
        parent_hidden = bool(parent_index is not None and self.nodes[parent_index]["hidden"])
        style = normalized_attrs.get("style", "").replace(" ", "").lower()
        hidden = (
            parent_hidden
            or normalized_tag in _NON_VISIBLE_TAGS
            or "hidden" in normalized_attrs
            or normalized_attrs.get("aria-hidden", "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        kept_attrs = tuple(
            sorted(
                (name, normalize_text(value)[:500])
                for name, value in normalized_attrs.items()
                if name in _KEPT_ATTRIBUTES and value
            )
        )
        node_id = f"n{len(self.nodes) + 1:06d}"
        node = {
            "node_id": node_id,
            "tag": normalized_tag,
            "parent_index": parent_index,
            "attributes": kept_attrs,
            "content": [],
            "hidden": hidden,
            "table_row": None,
            "table_column": None,
        }
        self.nodes.append(node)
        node_index = len(self.nodes) - 1
        if parent_index is not None:
            self.nodes[parent_index]["content"].append(("child", node_index))
        if normalized_tag not in _VOID_TAGS:
            self.stack.append(node_index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        matching_position = next(
            (
                position
                for position in range(len(self.stack) - 1, -1, -1)
                if self.nodes[self.stack[position]]["tag"] == normalized_tag
            ),
            None,
        )
        if matching_position is None:
            return
        closing_indexes = self.stack[matching_position:]
        del self.stack[matching_position:]
        for node_index in reversed(closing_indexes):
            if self.nodes[node_index]["tag"] == "script":
                self._capture_structured_script(node_index)

    def handle_data(self, data: str) -> None:
        if not self.stack:
            return
        node_index = self.stack[-1]
        if self.nodes[node_index]["tag"] == "script":
            self.nodes[node_index]["content"].append(("raw", data))
            return
        if self.nodes[node_index]["hidden"]:
            return
        clean = normalize_text(data)
        if clean:
            self.nodes[node_index]["content"].append(("text", clean))

    def close(self) -> None:
        for node_index in reversed(self.stack):
            if self.nodes[node_index]["tag"] == "script":
                self._capture_structured_script(node_index)
        self.stack.clear()
        super().close()

    def _capture_structured_script(self, node_index: int) -> None:
        node = self.nodes[node_index]
        if node.get("structured_captured"):
            return
        node["structured_captured"] = True
        attrs = dict(node["attributes"])
        script_type = attrs.get("type", "").lower()
        script_id = attrs.get("id", "")
        if script_type not in _STRUCTURED_SCRIPT_TYPES and script_id != "__NEXT_DATA__":
            return
        raw = "".join(value for kind, value in node["content"] if kind == "raw").strip()
        if not raw:
            return
        try:
            parsed: Any | None = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        self.structured_data.append(
            StructuredDataBlock(
                node_id=node["node_id"],
                script_type=script_type or "application/json",
                raw=raw,
                parsed=parsed,
            )
        )

    def build(self) -> PricingDomMap:
        aggregate_text: list[str] = ["" for _ in self.nodes]
        for node_index in range(len(self.nodes) - 1, -1, -1):
            node = self.nodes[node_index]
            if node["hidden"]:
                continue
            pieces: list[str] = []
            for kind, value in node["content"]:
                if kind == "text":
                    pieces.append(value)
                elif kind == "child" and aggregate_text[value]:
                    pieces.append(aggregate_text[value])
            aggregate_text[node_index] = normalize_text(" ".join(pieces))

        row_by_table: dict[int, int] = {}
        row_by_node: dict[int, int] = {}
        column_by_row: dict[int, int] = {}
        for node_index, node in enumerate(self.nodes):
            table_index = self._nearest_ancestor(node_index, "table")
            if node["tag"] == "tr" and table_index is not None:
                row_by_table[table_index] = row_by_table.get(table_index, -1) + 1
                row_by_node[node_index] = row_by_table[table_index]
                node["table_row"] = row_by_node[node_index]
            elif node["tag"] in {"td", "th"}:
                row_index = self._nearest_ancestor(node_index, "tr")
                if row_index is not None and row_index in row_by_node:
                    column_by_row[row_index] = column_by_row.get(row_index, -1) + 1
                    node["table_row"] = row_by_node[row_index]
                    node["table_column"] = column_by_row[row_index]

        frozen_nodes: list[DomNode] = []
        for node_index, node in enumerate(self.nodes):
            parent_index = node["parent_index"]
            parent_id = self.nodes[parent_index]["node_id"] if parent_index is not None else None
            attrs = dict(node["attributes"])
            selector_hint = node["tag"]
            if attrs.get("id"):
                selector_hint += f"#{attrs['id']}"
            elif attrs.get("class"):
                classes = [part for part in attrs["class"].split(" ") if part][:2]
                selector_hint += "".join(f".{part}" for part in classes)
            frozen_nodes.append(
                DomNode(
                    node_id=node["node_id"],
                    tag=node["tag"],
                    parent_id=parent_id,
                    attributes=node["attributes"],
                    text=aggregate_text[node_index],
                    selector_hint=selector_hint[:500],
                    table_row=node["table_row"],
                    table_column=node["table_column"],
                )
            )

        root_texts = [
            aggregate_text[index]
            for index, node in enumerate(self.nodes)
            if node["parent_index"] is None and aggregate_text[index]
        ]
        return PricingDomMap(
            nodes=tuple(frozen_nodes),
            structured_data=tuple(self.structured_data),
            visible_text=normalize_text(" ".join(root_texts)),
        )

    def _nearest_ancestor(self, node_index: int, tag: str) -> int | None:
        current: int | None = node_index
        while current is not None:
            if self.nodes[current]["tag"] == tag:
                return current
            current = self.nodes[current]["parent_index"]
        return None


def parse_pricing_dom(html: str) -> PricingDomMap:
    parser = _PricingDomParser()
    parser.feed(html or "")
    parser.close()
    return parser.build()
