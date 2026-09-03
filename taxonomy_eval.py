"""Gold evaluation for the canonical taxonomy classifier.

This command is read-only. It compares canonical taxonomy assignments and
immutable classification runs with a Gold CSV.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EVAL_REPORT_VERSION = "p2b-eval-v1-2026-08-06"
DEFAULT_AUTO_ACCEPTED_THRESHOLD = 0.85  # simulation until real auto_accepted status is calibrated


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug) else ""


def split_multi(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[|;,]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        slug = clean_slug(part)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


@dataclass
class GoldRow:
    tool_id: int
    canonical_slug: str
    official_url: str
    entity_kind: str
    primary_leaf_slug: str
    primary_acceptable_alternates: list[str] = field(default_factory=list)
    capabilities_ok: list[str] = field(default_factory=list)
    use_cases_ok: list[str] = field(default_factory=list)
    user_types_ok: list[str] = field(default_factory=list)
    primary_must_not: list[str] = field(default_factory=list)
    notes: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    is_draft: bool = True

    @property
    def accepted_primaries(self) -> set[str]:
        out = {self.primary_leaf_slug}
        out.update(self.primary_acceptable_alternates)
        out.discard("")
        return out


@dataclass
class Prediction:
    tool_id: int
    primary_slug: str = ""
    confidence: float | None = None
    decision_status: str = ""
    source: str = ""
    capabilities: list[str] = field(default_factory=list)
    capability_confidences: dict[str, float] = field(default_factory=dict)
    run_id: int | None = None
    taxonomy_version: int | None = None
    prompt_version: str = ""
    model_name: str = ""


def load_gold_csv(path: str | Path) -> list[GoldRow]:
    path = Path(path)
    rows: list[GoldRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            if not raw:
                continue
            tool_id_raw = str(raw.get("tool_id") or "").strip()
            if not tool_id_raw or not tool_id_raw.isdigit():
                continue
            notes = str(raw.get("notes") or "")
            primary = clean_slug(raw.get("primary_leaf_slug"))
            if not primary:
                continue
            rows.append(
                GoldRow(
                    tool_id=int(tool_id_raw),
                    canonical_slug=str(raw.get("canonical_slug") or "").strip(),
                    official_url=str(raw.get("official_url") or "").strip(),
                    entity_kind=str(raw.get("entity_kind") or "").strip(),
                    primary_leaf_slug=primary,
                    primary_acceptable_alternates=split_multi(raw.get("primary_acceptable_alternates")),
                    capabilities_ok=split_multi(raw.get("capabilities_ok")),
                    use_cases_ok=split_multi(raw.get("use_cases_ok")),
                    user_types_ok=split_multi(raw.get("user_types_ok")),
                    primary_must_not=split_multi(raw.get("primary_must_not")),
                    notes=notes,
                    reviewer=str(raw.get("reviewer") or "").strip(),
                    reviewed_at=str(raw.get("reviewed_at") or "").strip(),
                    is_draft=("DRAFT" in notes.upper()) or not str(raw.get("reviewer") or "").strip(),
                )
            )
    return rows


def is_auto_accepted(
    pred: Prediction | None,
    *,
    threshold: float = DEFAULT_AUTO_ACCEPTED_THRESHOLD,
    include_simulated: bool = True,
) -> bool:
    if pred is None or not pred.primary_slug:
        return False
    if pred.decision_status == "auto_accepted":
        return True
    if not include_simulated:
        return False
    if pred.decision_status in ("provisional", "auto_accepted", ""):
        conf = pred.confidence
        if conf is not None and conf >= threshold:
            return True
    return False


def primary_match(gold: GoldRow, pred_slug: str) -> bool:
    slug = clean_slug(pred_slug)
    if not slug:
        return False
    return slug in gold.accepted_primaries


def safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


@dataclass
class EvalBundle:
    gold_rows: list[GoldRow]
    shadow: dict[int, Prediction]
    auto_accepted_threshold: float = DEFAULT_AUTO_ACCEPTED_THRESHOLD
    generated_at: str = field(default_factory=utc_now_iso)
    prompt_version: str = ""
    taxonomy_version: int | None = None
    notes: list[str] = field(default_factory=list)

    def evaluate(self) -> dict[str, Any]:
        return build_evaluation_report(self)


def build_evaluation_report(bundle: EvalBundle) -> dict[str, Any]:
    gold_rows = bundle.gold_rows
    shadow = bundle.shadow
    thr = bundle.auto_accepted_threshold

    n = len(gold_rows)
    exact_hits = 0
    must_not_hits = 0
    missing_shadow = 0
    unresolved = 0
    provisional = 0
    auto_accepted_n = 0
    auto_accepted_correct = 0
    confusion: Counter[tuple[str, str]] = Counter()
    per_class_tp: Counter[str] = Counter()
    per_class_fp: Counter[str] = Counter()
    per_class_fn: Counter[str] = Counter()
    gold_label_counts: Counter[str] = Counter()

    cap_tp = cap_fp = cap_fn = 0
    rows_out: list[dict[str, Any]] = []

    for gold in gold_rows:
        gold_label_counts[gold.primary_leaf_slug] += 1
        pred = shadow.get(gold.tool_id)

        if pred is None or not pred.primary_slug:
            missing_shadow += 1
            status = "missing_shadow"
            pred_slug = ""
            conf = None
            dec = ""
            match = False
            aa = False
        else:
            pred_slug = pred.primary_slug
            conf = pred.confidence
            dec = pred.decision_status
            match = primary_match(gold, pred_slug)
            aa = is_auto_accepted(pred, threshold=thr)
            if dec == "unresolved" or (not pred_slug):
                unresolved += 1
            if dec == "provisional":
                provisional += 1
            if aa:
                auto_accepted_n += 1
                if match:
                    auto_accepted_correct += 1
            if match:
                exact_hits += 1
            if pred_slug in gold.primary_must_not:
                must_not_hits += 1
            if pred_slug:
                if match:
                    per_class_tp[gold.primary_leaf_slug] += 1
                else:
                    confusion[(gold.primary_leaf_slug, pred_slug)] += 1
                    per_class_fn[gold.primary_leaf_slug] += 1
                    per_class_fp[pred_slug] += 1
            status = "match" if match else "mismatch"

        # Capabilities micro counts (only when gold lists any)
        pred_caps = set(pred.capabilities) if pred else set()
        gold_caps = set(gold.capabilities_ok)
        if gold_caps:
            for c in pred_caps:
                if c in gold_caps:
                    cap_tp += 1
                else:
                    cap_fp += 1
            for c in gold_caps:
                if c not in pred_caps:
                    cap_fn += 1

        rows_out.append(
            {
                "tool_id": gold.tool_id,
                "canonical_slug": gold.canonical_slug,
                "gold_primary": gold.primary_leaf_slug,
                "gold_alternates": gold.primary_acceptable_alternates,
                "gold_must_not": gold.primary_must_not,
                "shadow_primary": pred_slug,
                "shadow_confidence": conf,
                "shadow_decision_status": dec,
                "shadow_capabilities": sorted(pred_caps),
                "match_gold": match if pred_slug else False,
                "auto_accepted_simulated": aa,
                "must_not_violation": bool(pred_slug and pred_slug in gold.primary_must_not),
                "status": status,
                "is_draft_gold": gold.is_draft,
                "notes": gold.notes,
                "run_id": pred.run_id if pred else None,
                "prompt_version": pred.prompt_version if pred else "",
                "model_name": pred.model_name if pred else "",
            }
        )

    # Macro P/R over gold labels that appear
    labels = sorted(gold_label_counts.keys())
    precs: list[float] = []
    recs: list[float] = []
    per_class: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = per_class_tp[label]
        fp = per_class_fp[label]
        fn = per_class_fn[label]
        p = safe_div(tp, tp + fp)
        r = safe_div(tp, tp + fn)
        precs.append(p)
        recs.append(r)
        per_class[label] = {
            "support": gold_label_counts[label],
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1(p, r), 4),
        }
    macro_p = safe_div(sum(precs), len(precs)) if precs else 0.0
    macro_r = safe_div(sum(recs), len(recs)) if recs else 0.0

    # Confusion pairs that are actual mismatches (gold != pred)
    confuse_pairs = [
        {"gold": g, "pred": p, "count": c}
        for (g, p), c in confusion.most_common()
        if g != p
    ]

    evaluated = n - missing_shadow
    report = {
        "report_version": EVAL_REPORT_VERSION,
        "generated_at": bundle.generated_at,
        "gold_n": n,
        "gold_draft_n": sum(1 for g in gold_rows if g.is_draft),
        "gold_reviewed_n": sum(1 for g in gold_rows if not g.is_draft),
        "shadow_present_n": evaluated,
        "missing_shadow_n": missing_shadow,
        "auto_accepted_threshold": thr,
        "prompt_version": bundle.prompt_version,
        "taxonomy_version": bundle.taxonomy_version,
        "metrics": {
            "overall_exact_accuracy": round(safe_div(exact_hits, evaluated), 4) if evaluated else None,
            "overall_exact_accuracy_incl_missing": round(safe_div(exact_hits, n), 4) if n else None,
            "macro_precision": round(macro_p, 4),
            "macro_recall": round(macro_r, 4),
            "macro_f1": round(f1(macro_p, macro_r), 4),
            "auto_accepted_n": auto_accepted_n,
            "auto_accepted_precision": round(safe_div(auto_accepted_correct, auto_accepted_n), 4)
            if auto_accepted_n
            else None,
            "unresolved_rate": round(safe_div(unresolved, n), 4) if n else None,
            "provisional_rate": round(safe_div(provisional, n), 4) if n else None,
            "missing_shadow_rate": round(safe_div(missing_shadow, n), 4) if n else None,
            "must_not_violation_rate": round(safe_div(must_not_hits, evaluated), 4) if evaluated else None,
            "manual_review_rate_proxy": round(
                safe_div(missing_shadow + unresolved + (evaluated - auto_accepted_n), n), 4
            )
            if n
            else None,
            "capability_precision": round(safe_div(cap_tp, cap_tp + cap_fp), 4)
            if (cap_tp + cap_fp)
            else None,
            "capability_recall": round(safe_div(cap_tp, cap_tp + cap_fn), 4)
            if (cap_tp + cap_fn)
            else None,
            "capability_f1": round(
                f1(safe_div(cap_tp, cap_tp + cap_fp), safe_div(cap_tp, cap_tp + cap_fn)), 4
            )
            if (cap_tp + cap_fp + cap_fn)
            else None,
        },
        "per_class": per_class,
        "confusion_mismatches": confuse_pairs[:50],
        "evaluation_summary": {
            "shadow_match_gold": sum(1 for r in rows_out if r["match_gold"]),
            "shadow_mismatch_gold": sum(
                1 for r in rows_out if r["shadow_primary"] and not r["match_gold"]
            ),
        },
        "rows": rows_out,
        "notes": bundle.notes
        + [
            "Gold rows marked DRAFT / without reviewer are not production-grade labels.",
            "auto_accepted_precision uses decision_status=auto_accepted OR simulated conf>=threshold on provisional.",
            "manual_review_rate_proxy ≈ (missing + unresolved + non-auto_accepted) / gold_n.",
        ],
    }
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    m = report.get("metrics") or {}
    d = report.get("evaluation_summary") or {}
    lines = [
        f"# Shadow Gold Evaluation ({report.get('report_version')})",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Gold N: **{report.get('gold_n')}** (draft={report.get('gold_draft_n')}, reviewed={report.get('gold_reviewed_n')})",
        f"- Shadow present: **{report.get('shadow_present_n')}** / missing **{report.get('missing_shadow_n')}**",
        f"- auto_accepted threshold (simulated): **{report.get('auto_accepted_threshold')}**",
        f"- prompt_version: `{report.get('prompt_version') or 'n/a'}` taxonomy_version: `{report.get('taxonomy_version')}`",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Overall Exact Accuracy (evaluated) | {m.get('overall_exact_accuracy')} |",
        f"| Overall Exact Accuracy (incl missing) | {m.get('overall_exact_accuracy_incl_missing')} |",
        f"| Macro Precision | {m.get('macro_precision')} |",
        f"| Macro Recall | {m.get('macro_recall')} |",
        f"| Macro F1 | {m.get('macro_f1')} |",
        f"| **auto_accepted Precision** | **{m.get('auto_accepted_precision')}** (n={m.get('auto_accepted_n')}) |",
        f"| Unresolved Rate | {m.get('unresolved_rate')} |",
        f"| Provisional Rate | {m.get('provisional_rate')} |",
        f"| Missing Shadow Rate | {m.get('missing_shadow_rate')} |",
        f"| Must-not Violation Rate | {m.get('must_not_violation_rate')} |",
        f"| Manual Review Rate (proxy) | {m.get('manual_review_rate_proxy')} |",
        f"| Capability Precision | {m.get('capability_precision')} |",
        f"| Capability Recall | {m.get('capability_recall')} |",
        f"| Capability F1 | {m.get('capability_f1')} |",
        "",
        "## Evaluation summary",
        "",
        f"- shadow match gold: **{d.get('shadow_match_gold')}**",
        f"- shadow mismatch gold: **{d.get('shadow_mismatch_gold')}**",
        "",
        "## Confusion mismatches (gold → pred)",
        "",
    ]
    conf = report.get("confusion_mismatches") or []
    if not conf:
        lines.append("_None_")
    else:
        lines.append("| Gold | Pred | Count |")
        lines.append("|------|------|------:|")
        for item in conf[:30]:
            lines.append(f"| {item['gold']} | {item['pred']} | {item['count']} |")

    lines.extend(["", "## Per-class", ""])
    per = report.get("per_class") or {}
    if not per:
        lines.append("_None_")
    else:
        lines.append("| Class | Support | P | R | F1 |")
        lines.append("|-------|--------:|--:|--:|---:|")
        for label, stats in sorted(per.items(), key=lambda kv: (-kv[1].get("support", 0), kv[0])):
            lines.append(
                f"| {label} | {stats.get('support')} | {stats.get('precision')} | {stats.get('recall')} | {stats.get('f1')} |"
            )

    lines.extend(["", "## Rows", ""])
    lines.append(
        "| tool | gold | shadow | conf | AA? | match | status |"
    )
    lines.append("|------|------|--------|-----:|:---:|:-----:|--------|")
    for row in report.get("rows") or []:
        lines.append(
            "| {tool_id}:{canonical_slug} | {gold_primary} | {shadow_primary} | {shadow_confidence} | {aa} | {match} | {status} |".format(
                tool_id=row.get("tool_id"),
                canonical_slug=row.get("canonical_slug"),
                gold_primary=row.get("gold_primary"),
                shadow_primary=row.get("shadow_primary") or "—",
                shadow_confidence=row.get("shadow_confidence")
                if row.get("shadow_confidence") is not None
                else "—",
                aa="Y" if row.get("auto_accepted_simulated") else "",
                match="Y" if row.get("match_gold") else "",
                status=row.get("status"),
            )
        )

    notes = report.get("notes") or []
    if notes:
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {note}")

    lines.append("")
    return "\n".join(lines)


def write_report_files(report: dict[str, Any], report_dir: str | Path) -> dict[str, str]:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = report_dir / f"gold-eval-{stamp}.json"
    md_path = report_dir / f"gold-eval-{stamp}.md"
    latest_json = report_dir / "gold-eval-latest.json"
    latest_md = report_dir / "gold-eval-latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_markdown_report(report)
    md_path.write_text(md, encoding="utf-8")
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    return {
        "json": str(json_path),
        "md": str(md_path),
        "latest_json": str(latest_json),
        "latest_md": str(latest_md),
    }

# ---------------------------------------------------------------------------
# D1 loaders
# ---------------------------------------------------------------------------

SHADOW_PRIMARY_SQL = """
SELECT
  a.tool_id,
  a.confidence,
  a.decision_status,
  a.source,
  a.run_id,
  t.slug AS primary_slug,
  r.prompt_version,
  r.model_name,
  r.taxonomy_version
FROM product_taxonomy_assignments a
JOIN taxonomy_terms t
  ON t.id = a.term_id
 AND t.dimension = 'primary_category'
LEFT JOIN classification_runs r ON r.id = a.run_id
WHERE a.tool_id IN ({placeholders})
  AND a.is_primary = 1
  AND a.source = 'auto'
  AND a.decision_status IN ('provisional', 'auto_accepted', 'unresolved', 'verified')
ORDER BY a.tool_id, a.updated_at DESC, a.id DESC
"""

SHADOW_CAPS_SQL = """
SELECT
  a.tool_id,
  a.confidence,
  t.slug AS capability_slug
FROM product_taxonomy_assignments a
JOIN taxonomy_terms t
  ON t.id = a.term_id
 AND t.dimension = 'capability'
WHERE a.tool_id IN ({placeholders})
  AND a.source = 'auto'
  AND a.decision_status IN ('provisional', 'auto_accepted', 'verified')
  AND a.decision_status <> 'superseded'
"""

SHADOW_RUN_SQL = """
SELECT
  id AS run_id,
  tool_id,
  taxonomy_version,
  prompt_version,
  model_name,
  raw_output,
  run_status
FROM classification_runs
WHERE tool_id IN ({placeholders})
ORDER BY tool_id, id DESC
"""

def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def prediction_from_run_row(row: dict[str, Any]) -> Prediction:
    """Extract the model prediction from an immutable classification run.

    Human-reviewed assignments are intentionally locked during Shadow reruns,
    so evaluation must use the run payload rather than the effective assignment.
    """
    tool_id = int(row.get("tool_id") or 0)
    raw: dict[str, Any] = {}
    raw_text = row.get("raw_output")
    if isinstance(raw_text, str) and raw_text.strip():
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    elif isinstance(raw_text, dict):
        raw = raw_text

    leaf = raw.get("leaf_accepted")
    if not isinstance(leaf, dict):
        leaf = {}
    primary_slug = clean_slug(leaf.get("slug"))
    confidence: float | None = None
    try:
        if leaf.get("confidence") is not None:
            confidence = float(leaf.get("confidence"))
    except (TypeError, ValueError):
        confidence = None

    capabilities: list[str] = []
    capability_confidences: dict[str, float] = {}
    for item in raw.get("capabilities_accepted") or []:
        if not isinstance(item, dict):
            continue
        slug = clean_slug(item.get("slug"))
        if not slug or slug in capabilities:
            continue
        capabilities.append(slug)
        try:
            capability_confidences[slug] = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            pass

    run_id_raw = row.get("run_id")
    taxonomy_version_raw = row.get("taxonomy_version")
    return Prediction(
        tool_id=tool_id,
        primary_slug=primary_slug,
        confidence=confidence,
        decision_status="provisional" if primary_slug else "unresolved",
        source="classification_run",
        capabilities=capabilities,
        capability_confidences=capability_confidences,
        run_id=int(run_id_raw) if run_id_raw not in (None, "") else None,
        taxonomy_version=int(taxonomy_version_raw)
        if taxonomy_version_raw not in (None, "")
        else None,
        prompt_version=str(row.get("prompt_version") or ""),
        model_name=str(row.get("model_name") or ""),
    )


async def load_shadow_predictions(d1: Any, tool_ids: list[int]) -> dict[int, Prediction]:
    if not tool_ids:
        return {}
    ph = _placeholders(len(tool_ids))
    run_rows = await d1.query(SHADOW_RUN_SQL.format(placeholders=ph), list(tool_ids))
    primary_rows = await d1.query(SHADOW_PRIMARY_SQL.format(placeholders=ph), list(tool_ids))
    cap_rows = await d1.query(SHADOW_CAPS_SQL.format(placeholders=ph), list(tool_ids))

    preds: dict[int, Prediction] = {}
    for row in run_rows:
        tool_id = int(row.get("tool_id") or 0)
        if tool_id <= 0 or tool_id in preds:
            continue
        preds[tool_id] = prediction_from_run_row(row)

    for row in primary_rows:
        tool_id = int(row.get("tool_id") or 0)
        if tool_id <= 0 or tool_id in preds:
            # Prefer immutable latest-run output; otherwise first assignment is newest.
            continue
        conf_raw = row.get("confidence")
        try:
            conf = float(conf_raw) if conf_raw is not None else None
        except (TypeError, ValueError):
            conf = None
        preds[tool_id] = Prediction(
            tool_id=tool_id,
            primary_slug=clean_slug(row.get("primary_slug")),
            confidence=conf,
            decision_status=str(row.get("decision_status") or ""),
            source=str(row.get("source") or ""),
            run_id=int(row["run_id"]) if row.get("run_id") not in (None, "") else None,
            taxonomy_version=int(row["taxonomy_version"])
            if row.get("taxonomy_version") not in (None, "")
            else None,
            prompt_version=str(row.get("prompt_version") or ""),
            model_name=str(row.get("model_name") or ""),
        )

    caps_by_tool: dict[int, list[str]] = defaultdict(list)
    cap_conf: dict[int, dict[str, float]] = defaultdict(dict)
    for row in cap_rows:
        tool_id = int(row.get("tool_id") or 0)
        slug = clean_slug(row.get("capability_slug"))
        if tool_id <= 0 or not slug:
            continue
        if slug not in caps_by_tool[tool_id]:
            caps_by_tool[tool_id].append(slug)
        try:
            cap_conf[tool_id][slug] = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            pass

    for tool_id, pred in preds.items():
        if pred.source == "classification_run":
            continue
        pred.capabilities = caps_by_tool.get(tool_id, [])
        pred.capability_confidences = cap_conf.get(tool_id, {})

    # tools with only capabilities / no primary
    for tool_id, caps in caps_by_tool.items():
        if tool_id not in preds:
            preds[tool_id] = Prediction(
                tool_id=tool_id,
                capabilities=caps,
                capability_confidences=cap_conf.get(tool_id, {}),
                decision_status="unresolved",
            )
    return preds


async def run_gold_evaluation(
    config: Any,
    *,
    gold_csv: str | Path,
    report_dir: str | Path,
    auto_accepted_threshold: float = DEFAULT_AUTO_ACCEPTED_THRESHOLD,
    tool_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Load Gold and canonical taxonomy predictions, then write evaluation reports."""
    from runner import D1Client, log_info

    gold_rows = load_gold_csv(gold_csv)
    if tool_ids:
        allow = {int(x) for x in tool_ids}
        gold_rows = [g for g in gold_rows if g.tool_id in allow]
    if not gold_rows:
        raise ValueError(f"No gold rows loaded from {gold_csv}")

    ids = [g.tool_id for g in gold_rows]
    async with D1Client(config) as d1:
        shadow = await load_shadow_predictions(d1, ids)

    # Bind prompt/taxonomy from any shadow pred
    prompt_version = ""
    taxonomy_version = None
    for pred in shadow.values():
        if pred.prompt_version and not prompt_version:
            prompt_version = pred.prompt_version
        if pred.taxonomy_version is not None and taxonomy_version is None:
            taxonomy_version = pred.taxonomy_version
        if prompt_version and taxonomy_version is not None:
            break

    bundle = EvalBundle(
        gold_rows=gold_rows,
        shadow=shadow,
        auto_accepted_threshold=auto_accepted_threshold,
        prompt_version=prompt_version,
        taxonomy_version=taxonomy_version,
        notes=[f"gold_csv={gold_csv}"],
    )
    report = bundle.evaluate()
    paths = write_report_files(report, report_dir)
    summary = {
        "gold_n": report["gold_n"],
        "shadow_present_n": report["shadow_present_n"],
        "missing_shadow_n": report["missing_shadow_n"],
        "overall_exact_accuracy": (report["metrics"] or {}).get("overall_exact_accuracy"),
        "auto_accepted_precision": (report["metrics"] or {}).get("auto_accepted_precision"),
        "auto_accepted_n": (report["metrics"] or {}).get("auto_accepted_n"),
        "macro_f1": (report["metrics"] or {}).get("macro_f1"),
        "report_md": paths["latest_md"],
        "report_json": paths["latest_json"],
    }
    log_info("gold_eval.summary", **{k: v for k, v in summary.items() if k not in ("report_md", "report_json")})
    log_info("gold_eval.report_paths", **paths)
    return {"report": report, "paths": paths, "summary": summary}


def default_gold_csv_path() -> Path:
    """Resolve gold seed relative to repo roots commonly used in this monorepo."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "docs" / "taxonomy" / "gold-dataset-seed-draft.csv",
        here.parent / "docs" / "taxonomy" / "gold-dataset-seed-draft.csv",
        Path.cwd() / "docs" / "taxonomy" / "gold-dataset-seed-draft.csv",
        Path.cwd().parent / "docs" / "taxonomy" / "gold-dataset-seed-draft.csv",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def default_report_dir() -> Path:
    here = Path(__file__).resolve()
    # Prefer monorepo docs/taxonomy/reports
    candidates = [
        here.parent.parent / "docs" / "taxonomy" / "reports",
        here.parent / "reports",
        Path.cwd() / "docs" / "taxonomy" / "reports",
    ]
    return candidates[0]
