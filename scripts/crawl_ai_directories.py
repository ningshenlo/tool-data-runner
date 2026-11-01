#!/usr/bin/env python3
"""
AI Directory Crawler (VPS / Python runner).
Crawls high-quality AI directories (Futurepedia, AITopTools, Dang.ai)
to discover new AI tools, extracts official websites, and feeds them into
Sigpik's PostgreSQL discovery_candidates table via the trusted bypass channel ('ai_directory_import').
"""

from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, AsyncIterator
import urllib.parse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import httpx
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ai_directory_crawler")

SKIPPED_DOMAINS = {
    "x.com", "twitter.com", "t.co", "facebook.com", "instagram.com",
    "linkedin.com", "youtube.com", "youtu.be", "producthunt.com",
    "reddit.com", "news.ycombinator.com", "medium.com", "substack.com",
    "dev.to", "github.com", "gitlab.com", "discord.com", "discord.gg",
    "apple.com", "google.com", "play.google.com", "apps.apple.com",
    "futurepedia.io", "aitoptools.com", "dang.ai", "topai.tools", "toolify.ai"
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

@dataclass
class DiscoveredCandidate:
    source: str
    source_id: str
    source_url: str
    official_url: str
    normalized_domain: str
    submitted_name: str
    description_hint: str | None
    confidence_score: int
    raw_payload: str
    evidence_fingerprint: str
    directory: str


def stable_hash(text: str) -> str:
    """FNV-1a 32-bit hash, byte-for-byte compatible with discovery-runner TypeScript implementation."""
    h = 2166136261
    for b in text.encode("utf-8"):
        h = (h ^ b) * 16777619 & 0xFFFFFFFF
    return f"{h:x}"


def normalize_domain(input_str: str | None) -> str | None:
    if not input_str or not input_str.strip():
        return None
    raw = input_str.strip()
    try:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            raw = f"https://{raw}"
        parsed = urllib.parse.urlparse(raw)
        host = parsed.hostname.lower().rstrip(".").removeprefix("www.")
        return host or None
    except Exception:
        clean = re.sub(r"^https?://", "", raw, flags=re.I).split("/")[0].split(":")[0].lower().rstrip(".").removeprefix("www.")
        return clean or None


def clean_url(url_str: str | None) -> str | None:
    if not url_str or not url_str.strip():
        return None
    raw = url_str.strip()
    try:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            raw = f"https://{raw}"
        parsed = urllib.parse.urlparse(raw)
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        cleaned_q = {
            k: v for k, v in q.items()
            if not k.lower().startswith("utm_")
            and k.lower() not in ["ref", "ref_src", "fbclid", "gclid", "msclkid"]
        }
        new_query = urllib.parse.urlencode(cleaned_q, doseq=True)
        cleaned = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
        return cleaned
    except Exception:
        return raw


def is_skipped_domain(domain: str | None) -> bool:
    if not domain:
        return True
    return domain in SKIPPED_DOMAINS or any(domain.endswith(f".{d}") for d in SKIPPED_DOMAINS)


def candidate_evidence_fingerprint(candidate: dict[str, Any]) -> str:
    fingerprint_obj = {
        "confidenceScore": candidate["confidence_score"],
        "descriptionHint": candidate["description_hint"].strip() if candidate.get("description_hint") else None,
        "normalizedDomain": candidate["normalized_domain"],
        "officialUrl": candidate["official_url"],
        "submittedName": candidate["submitted_name"].strip(),
    }
    return stable_hash(json.dumps(fingerprint_obj, separators=(",", ":")))


# -------------------------------------------------------------------------
# Directory Crawlers
# -------------------------------------------------------------------------

class BaseDirectoryCrawler:
    name: str = "base"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def discover_entries(self, limit: int) -> list[dict[str, str]]:
        """Return list of dicts with keys: slug, page_url, [lastmod]"""
        raise NotImplementedError

    async def extract_tool(self, entry: dict[str, str]) -> DiscoveredCandidate | None:
        """Extract tool details from its directory page."""
        raise NotImplementedError


class FuturepediaCrawler(BaseDirectoryCrawler):
    name = "futurepedia"

    async def discover_entries(self, limit: int) -> list[dict[str, str]]:
        url = "https://www.futurepedia.io/sitemap_tools.xml"
        logger.info(f"[{self.name}] Fetching sitemap: {url}")
        resp = await self.client.get(url, timeout=20.0)
        if resp.status_code != 200:
            logger.warning(f"[{self.name}] Failed to fetch sitemap: HTTP {resp.status_code}")
            return []

        root = ET.fromstring(resp.text)
        entries = []
        for url_elem in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
            loc = url_elem.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            lastmod = url_elem.find("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
            if loc is not None and loc.text and "/tool/" in loc.text:
                slug = loc.text.rstrip("/").split("/")[-1]
                entries.append({
                    "slug": slug,
                    "page_url": loc.text,
                    "lastmod": lastmod.text if lastmod is not None else ""
                })

        # Sort by lastmod descending (most recently updated/added first)
        entries.sort(key=lambda x: x["lastmod"], reverse=True)
        logger.info(f"[{self.name}] Discovered {len(entries)} tools in sitemap (returning top {limit})")
        return entries[:limit]

    async def extract_tool(self, entry: dict[str, str]) -> DiscoveredCandidate | None:
        try:
            resp = await self.client.get(entry["page_url"], timeout=12.0)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")

            h1 = soup.find("h1")
            name = h1.get_text(strip=True) if h1 else None
            meta_desc = soup.find("meta", attrs={"name": "description"})
            desc = meta_desc["content"] if meta_desc and "content" in meta_desc.attrs else None

            official_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True).lower()
                if "utm_source=futurepedia" in href or "visit" in text or "website" in text:
                    dom = normalize_domain(href)
                    if href.startswith("http") and "futurepedia.io" not in href and dom and not is_skipped_domain(dom):
                        official_url = href
                        break

            if not official_url:
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    dom = normalize_domain(href)
                    if href.startswith("http") and "futurepedia.io" not in href and dom and not is_skipped_domain(dom):
                        official_url = href
                        break

            if not official_url:
                return None

            cleaned_official = clean_url(official_url)
            norm_domain = normalize_domain(cleaned_official)
            if not cleaned_official or not norm_domain or is_skipped_domain(norm_domain):
                return None

            clean_name = name or norm_domain
            clean_desc = desc or f"Discovered by Futurepedia crawler: {clean_name}"
            candidate_dict = {
                "confidence_score": 88,
                "description_hint": clean_desc,
                "normalized_domain": norm_domain,
                "official_url": cleaned_official,
                "submitted_name": clean_name,
            }
            fp = candidate_evidence_fingerprint(candidate_dict)

            raw = {
                "import_batch": f"ai-directory-crawler-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "directory": self.name,
                "slug": entry["slug"],
                "source_url": entry["page_url"],
                "official_url": cleaned_official,
                "name": clean_name,
                "description": clean_desc,
            }

            return DiscoveredCandidate(
                source="ai_directory_import",
                source_id=f"ai_directory:{self.name}:{entry['slug']}",
                source_url=entry["page_url"],
                official_url=cleaned_official,
                normalized_domain=norm_domain,
                submitted_name=clean_name,
                description_hint=clean_desc,
                confidence_score=88,
                raw_payload=json.dumps(raw, ensure_ascii=False),
                evidence_fingerprint=fp,
                directory=self.name,
            )
        except Exception as e:
            logger.debug(f"[{self.name}] Failed to extract {entry['page_url']}: {e}")
            return None


class AITopToolsCrawler(BaseDirectoryCrawler):
    name = "aitoptools"

    async def discover_entries(self, limit: int) -> list[dict[str, str]]:
        url = "https://aitoptools.com/sitemap.xml"
        logger.info(f"[{self.name}] Fetching sitemap: {url}")
        resp = await self.client.get(url, timeout=25.0)
        if resp.status_code != 200:
            logger.warning(f"[{self.name}] Failed to fetch sitemap: HTTP {resp.status_code}")
            return []

        root = ET.fromstring(resp.text)
        entries = []
        for url_elem in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
            loc = url_elem.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            lastmod = url_elem.find("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
            if loc is not None and loc.text and "/tool/" in loc.text:
                slug = loc.text.rstrip("/").split("/")[-1]
                entries.append({
                    "slug": slug,
                    "page_url": loc.text,
                    "lastmod": lastmod.text if lastmod is not None else ""
                })

        entries.sort(key=lambda x: x["lastmod"], reverse=True)
        logger.info(f"[{self.name}] Discovered {len(entries)} tools in sitemap (returning top {limit})")
        return entries[:limit]

    async def extract_tool(self, entry: dict[str, str]) -> DiscoveredCandidate | None:
        try:
            resp = await self.client.get(entry["page_url"], timeout=12.0)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")

            h1 = soup.find("h1")
            name = h1.get_text(strip=True) if h1 else None
            if name:
                name = name.replace("Verified tool", "").strip()

            meta_desc = soup.find("meta", attrs={"name": "description"})
            desc = meta_desc["content"] if meta_desc and "content" in meta_desc.attrs else None

            official_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True).lower()
                if "visit" in text or "utm_source=aitoptools" in href:
                    dom = normalize_domain(href)
                    if dom and not is_skipped_domain(dom) and "aitoptools.com" not in href:
                        official_url = href
                        break

            if not official_url:
                return None

            cleaned_official = clean_url(official_url)
            norm_domain = normalize_domain(cleaned_official)
            if not cleaned_official or not norm_domain or is_skipped_domain(norm_domain):
                return None

            clean_name = name or norm_domain
            clean_desc = desc or f"Discovered by AITopTools crawler: {clean_name}"
            candidate_dict = {
                "confidence_score": 88,
                "description_hint": clean_desc,
                "normalized_domain": norm_domain,
                "official_url": cleaned_official,
                "submitted_name": clean_name,
            }
            fp = candidate_evidence_fingerprint(candidate_dict)

            raw = {
                "import_batch": f"ai-directory-crawler-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "directory": self.name,
                "slug": entry["slug"],
                "source_url": entry["page_url"],
                "official_url": cleaned_official,
                "name": clean_name,
                "description": clean_desc,
            }

            return DiscoveredCandidate(
                source="ai_directory_import",
                source_id=f"ai_directory:{self.name}:{entry['slug']}",
                source_url=entry["page_url"],
                official_url=cleaned_official,
                normalized_domain=norm_domain,
                submitted_name=clean_name,
                description_hint=clean_desc,
                confidence_score=88,
                raw_payload=json.dumps(raw, ensure_ascii=False),
                evidence_fingerprint=fp,
                directory=self.name,
            )
        except Exception as e:
            logger.debug(f"[{self.name}] Failed to extract {entry['page_url']}: {e}")
            return None


class DangCrawler(BaseDirectoryCrawler):
    name = "dang"

    async def discover_entries(self, limit: int) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        page = 1
        max_pages = max(1, (limit // 24) + 1)
        
        logger.info(f"[{self.name}] Crawling /new pages up to page {max_pages}")
        while len(entries) < limit and page <= max_pages:
            url = f"https://dang.ai/new?page={page}"
            try:
                resp = await self.client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                found_in_page = 0
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("/tool/"):
                        slug = href.rstrip("/").split("/")[-1]
                        page_url = f"https://dang.ai{href}"
                        if not any(e["slug"] == slug for e in entries):
                            entries.append({"slug": slug, "page_url": page_url})
                            found_in_page += 1
                if found_in_page == 0:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"[{self.name}] Error fetching page {page}: {e}")
                break

        logger.info(f"[{self.name}] Discovered {len(entries)} tools across {page - 1} pages")
        return entries[:limit]

    async def extract_tool(self, entry: dict[str, str]) -> DiscoveredCandidate | None:
        try:
            resp = await self.client.get(entry["page_url"], timeout=12.0)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")

            h1 = soup.find("h1")
            name = h1.get_text(strip=True) if h1 else None
            meta_desc = soup.find("meta", attrs={"name": "description"})
            desc = meta_desc["content"] if meta_desc and "content" in meta_desc.attrs else None

            target_link = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True).lower()
                if "view tool" in text or "visit" in text:
                    target_link = href
                    break
                if href.startswith("https://l.dang.ai/") and "view" in text:
                    target_link = href
                    break

            if not target_link:
                for a in soup.find_all("a", href=True):
                    if a["href"].startswith("https://l.dang.ai/"):
                        target_link = a["href"]
                        break

            if not target_link:
                return None

            real_url = None
            if target_link.startswith("https://l.dang.ai/"):
                redir_resp = await self.client.get(target_link, follow_redirects=False, timeout=8.0)
                if redir_resp.status_code in [301, 302, 303, 307, 308]:
                    real_url = redir_resp.headers.get("location")
            elif target_link.startswith("http"):
                real_url = target_link

            if not real_url:
                return None

            cleaned_official = clean_url(real_url)
            norm_domain = normalize_domain(cleaned_official)
            if not cleaned_official or not norm_domain or is_skipped_domain(norm_domain):
                return None

            clean_name = name or norm_domain
            clean_desc = desc or f"Discovered by Dang.ai crawler: {clean_name}"
            candidate_dict = {
                "confidence_score": 88,
                "description_hint": clean_desc,
                "normalized_domain": norm_domain,
                "official_url": cleaned_official,
                "submitted_name": clean_name,
            }
            fp = candidate_evidence_fingerprint(candidate_dict)

            raw = {
                "import_batch": f"ai-directory-crawler-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "directory": self.name,
                "slug": entry["slug"],
                "source_url": entry["page_url"],
                "official_url": cleaned_official,
                "name": clean_name,
                "description": clean_desc,
            }

            return DiscoveredCandidate(
                source="ai_directory_import",
                source_id=f"ai_directory:{self.name}:{entry['slug']}",
                source_url=entry["page_url"],
                official_url=cleaned_official,
                normalized_domain=norm_domain,
                submitted_name=clean_name,
                description_hint=clean_desc,
                confidence_score=88,
                raw_payload=json.dumps(raw, ensure_ascii=False),
                evidence_fingerprint=fp,
                directory=self.name,
            )
        except Exception as e:
            logger.debug(f"[{self.name}] Failed to extract {entry['page_url']}: {e}")
            return None


CRAWLER_REGISTRY: dict[str, type[BaseDirectoryCrawler]] = {
    "futurepedia": FuturepediaCrawler,
    "aitoptools": AITopToolsCrawler,
    "dang": DangCrawler,
}


# -------------------------------------------------------------------------
# Database Operations
# -------------------------------------------------------------------------

def resolve_db_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    for env_var in ["DATABASE_URL", "SIGPIK_DATABASE_URL"]:
        val = os.environ.get(env_var)
        if val:
            return val
    # Fallback to local sigpik/.dev.vars if present
    dev_vars_path = Path("sigpik/.dev.vars")
    if not dev_vars_path.exists():
        dev_vars_path = Path("../sigpik/.dev.vars")
    if dev_vars_path.exists():
        with open(dev_vars_path, encoding="utf-8") as f:
            for line in f:
                if "SIGPIK_DEV_DATABASE_URL" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ValueError("Database URL not configured (set DATABASE_URL or check sigpik/.dev.vars)")


def load_known_catalog(db_url: str) -> tuple[set[str], set[str], set[str]]:
    """
    Returns (tools_domains, candidate_domains, candidate_source_ids).
    Allows skipping known domains without making outbound HTTP requests.
    """
    with psycopg.connect(db_url, sslmode="disable", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT normalized_domain FROM tools;")
            tools_domains = {r["normalized_domain"] for r in cur.fetchall()}
            
            cur.execute("SELECT normalized_domain, source_id FROM discovery_candidates WHERE source = 'ai_directory_import';")
            rows = cur.fetchall()
            candidate_domains = {r["normalized_domain"] for r in rows}
            source_ids = {r["source_id"] for r in rows}
            
            return tools_domains, candidate_domains, source_ids


def insert_candidates(db_url: str, candidates: list[DiscoveredCandidate]) -> int:
    if not candidates:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    inserted_count = 0

    with psycopg.connect(db_url, sslmode="disable", row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for c in candidates:
                cur.execute(
                    """
                    INSERT INTO discovery_candidates (
                        source, source_id, source_url, official_url, normalized_domain,
                        submitted_name, description_hint, confidence_score, raw_payload, evidence_fingerprint,
                        first_seen_at, last_seen_at, candidate_status, ai_eligible, ai_eligibility_reason,
                        ai_eligible_at, created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, 'observed', 1, 'trusted_source',
                        %s, %s, %s
                    )
                    ON CONFLICT (source, source_id) DO UPDATE SET
                        source_url = coalesce(excluded.source_url, discovery_candidates.source_url),
                        official_url = excluded.official_url,
                        normalized_domain = excluded.normalized_domain,
                        submitted_name = excluded.submitted_name,
                        description_hint = coalesce(discovery_candidates.description_hint, excluded.description_hint),
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    RETURNING id;
                    """,
                    (
                        c.source, c.source_id, c.source_url, c.official_url, c.normalized_domain,
                        c.submitted_name, c.description_hint, c.confidence_score, c.raw_payload, c.evidence_fingerprint,
                        now_iso, now_iso, now_iso, now_iso, now_iso
                    )
                )
                if cur.fetchone():
                    inserted_count += 1
        conn.commit()

    return inserted_count


# -------------------------------------------------------------------------
# Crawler Execution Pipeline
# -------------------------------------------------------------------------

async def run_crawler_pipeline(
    sources: list[str],
    limit_per_source: int,
    concurrency: int,
    db_url: str | None,
    dry_run: bool
) -> dict[str, Any]:
    summary = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "total_discovered": 0,
        "total_extracted": 0,
        "total_new_domains": 0,
        "total_inserted": 0,
        "details_by_source": {},
    }

    tools_domains: set[str] = set()
    candidate_domains: set[str] = set()
    known_source_ids: set[str] = set()

    if not dry_run and db_url:
        try:
            tools_domains, candidate_domains, known_source_ids = load_known_catalog(db_url)
            logger.info(f"Catalog loaded: {len(tools_domains)} existing tools, {len(candidate_domains)} candidates, {len(known_source_ids)} source_ids.")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            if not dry_run:
                raise

    limits = httpx.Limits(max_keepalive_connections=concurrency * 2, max_connections=concurrency * 3)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=15.0, limits=limits, follow_redirects=True) as client:
        sem = asyncio.Semaphore(concurrency)

        for source_name in sources:
            crawler_cls = CRAWLER_REGISTRY.get(source_name)
            if not crawler_cls:
                logger.warning(f"Unknown crawler source '{source_name}', skipping.")
                continue

            crawler = crawler_cls(client)
            source_summary = {
                "discovered": 0,
                "already_known_source_id": 0,
                "extracted": 0,
                "skipped_known_domain": 0,
                "new_candidates": 0,
            }

            try:
                entries = await crawler.discover_entries(limit_per_source)
            except Exception as e:
                logger.error(f"[{source_name}] Discovery failed: {e}")
                continue

            source_summary["discovered"] = len(entries)
            summary["total_discovered"] += len(entries)

            # Filter out entries whose source_id is already in DB
            unseen_entries = []
            for e in entries:
                expected_sid = f"ai_directory:{source_name}:{e['slug']}"
                if expected_sid in known_source_ids:
                    source_summary["already_known_source_id"] += 1
                else:
                    unseen_entries.append(e)

            logger.info(f"[{source_name}] Unseen entries to fetch: {len(unseen_entries)} (skipped {source_summary['already_known_source_id']} known source_ids)")

            async def bounded_extract(entry: dict[str, str]):
                async with sem:
                    return await crawler.extract_tool(entry)

            tasks = [bounded_extract(e) for e in unseen_entries]
            extracted_results = await asyncio.gather(*tasks, return_exceptions=True)

            valid_candidates: list[DiscoveredCandidate] = []
            for r in extracted_results:
                if isinstance(r, DiscoveredCandidate):
                    source_summary["extracted"] += 1
                    summary["total_extracted"] += 1
                    dom = r.normalized_domain
                    # Check if domain already exists in tools or candidates
                    if dom in tools_domains or dom in candidate_domains:
                        source_summary["skipped_known_domain"] += 1
                    else:
                        valid_candidates.append(r)
                        candidate_domains.add(dom)
                        source_summary["new_candidates"] += 1
                        summary["total_new_domains"] += 1

            logger.info(
                f"[{source_name}] Extracted {source_summary['extracted']} tools -> "
                f"{source_summary['new_candidates']} brand new domains "
                f"({source_summary['skipped_known_domain']} already known in DB)."
            )

            if not dry_run and db_url and valid_candidates:
                inserted = insert_candidates(db_url, valid_candidates)
                summary["total_inserted"] += inserted
                logger.info(f"[{source_name}] Inserted {inserted} candidates into PostgreSQL discovery_candidates.")

            summary["details_by_source"][source_name] = source_summary

    return summary


def main():
    parser = argparse.ArgumentParser(description="AI Directory Batch Crawler for Sigpik Discovery Engine")
    parser.add_argument("--sources", default="futurepedia,aitoptools,dang", help="Comma-separated list of directories (futurepedia,aitoptools,dang)")
    parser.add_argument("--limit", type=int, default=50, help="Max candidates per directory (default: 50)")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent request limit (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Crawl and output JSON without writing to database")
    parser.add_argument("--db-url", default=None, help="Explicit PostgreSQL connection URL")

    args = parser.parse_args()
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]

    db_url = None
    if not args.dry_run:
        try:
            db_url = resolve_db_url(args.db_url)
        except Exception as e:
            logger.error(f"Cannot resolve database URL: {e}")
            sys.exit(1)

    result = asyncio.run(
        run_crawler_pipeline(
            sources=sources,
            limit_per_source=args.limit,
            concurrency=args.concurrency,
            db_url=db_url,
            dry_run=args.dry_run,
        )
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
