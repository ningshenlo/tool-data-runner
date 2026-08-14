from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .codec import decode_state, encode_diff, encode_state
from .config import MonitorLimits
from .diff import diff_states
from .discovery import discovery_candidates, parse_robots_sitemaps, robots_url
from .fetch import SitemapFetchError, SitemapHttpFetcher
from .fingerprint import fingerprint_document
from .models import (
    CheckOutcome,
    FetchResult,
    FetchValidators,
    FingerprintedDocument,
    ResourceState,
    SiteScanResult,
    SitemapEntry,
    SitemapKind,
    RunResult,
)
from .normalize import normalize_sitemap_url
from .parser import SitemapParseError, parse_sitemap
from .storage import MetadataStore, ObjectStore, resource_id_for, site_id_for, stable_id


def _now_ms() -> int:
    return int(time.time() * 1_000)


@dataclass(frozen=True, slots=True)
class _CheckedResource:
    index_entries: tuple[SitemapEntry, ...]
    outcome: CheckOutcome


class SitemapMonitor:
    def __init__(
        self,
        metadata: MetadataStore,
        objects: ObjectStore,
        *,
        fetcher: SitemapHttpFetcher | None = None,
        limits: MonitorLimits | None = None,
    ):
        self.limits = limits or MonitorLimits()
        self.fetcher = fetcher or SitemapHttpFetcher(self.limits)
        self.metadata = metadata
        self.objects = objects

    async def close(self) -> None:
        close = getattr(self.fetcher, "close", None)
        if close is not None:
            result = close()
            if result is not None:
                await result

    async def _record(
        self,
        site_id: str,
        outcome: CheckOutcome,
        started_at_ms: int,
        run_namespace: str | None,
    ) -> CheckOutcome:
        finished_at_ms = _now_ms()
        run_key = (
            stable_id("smrun", run_namespace, outcome.resource_id)
            if run_namespace
            else stable_id("smrun", outcome.resource_id, str(started_at_ms))
        )
        await self.metadata.record_run(
            site_id,
            outcome,
            finished_at_ms=finished_at_ms,
            run_key=run_key,
            site_scan_id=(
                stable_id("smscan", run_namespace) if run_namespace else None
            ),
            started_at_ms=started_at_ms,
        )
        return outcome

    def _outcome(
        self,
        *,
        resource_id: str,
        url: str,
        final_url: str | None = None,
        result: RunResult,
        http_status: int | None = None,
        bytes_downloaded: int = 0,
        url_count: int | None = None,
        sitemap_kind: SitemapKind | None = None,
        state_key: str | None = None,
        diff_key: str | None = None,
        error_code: str | None = None,
        added_count: int = 0,
        removed_count: int = 0,
        modified_count: int = 0,
        parent_id: str | None = None,
    ) -> CheckOutcome:
        return CheckOutcome(
            added_count=added_count,
            bytes_downloaded=bytes_downloaded,
            diff_key=diff_key,
            error_code=error_code,
            final_url=final_url or url,
            http_status=http_status,
            modified_count=modified_count,
            resource_id=resource_id,
            result=result,
            sitemap_kind=sitemap_kind,
            state_key=state_key,
            url=url,
            url_count=url_count,
            removed_count=removed_count,
            parent_id=parent_id,
        )

    async def _failure(
        self,
        *,
        site_id: str,
        resource_id: str,
        url: str,
        parent_id: str | None,
        started_at_ms: int,
        error_code: str,
        run_namespace: str | None,
        final_url: str | None = None,
        http_status: int | None = None,
        bytes_downloaded: int = 0,
    ) -> _CheckedResource:
        checked_at_ms = _now_ms()
        await self.metadata.mark_failure(
            site_id,
            resource_id,
            url,
            canonical_fetch_url=final_url or url,
            parent_id=parent_id,
            checked_at_ms=checked_at_ms,
            missing=http_status in {404, 410},
        )
        outcome = self._outcome(
            resource_id=resource_id,
            url=url,
            final_url=final_url,
            result="failed",
            http_status=http_status,
            bytes_downloaded=bytes_downloaded,
            error_code=error_code,
            parent_id=parent_id,
        )
        return _CheckedResource(
            (),
            await self._record(site_id, outcome, started_at_ms, run_namespace),
        )

    async def _load_index_entries(self, state: ResourceState) -> tuple[SitemapEntry, ...]:
        if state.sitemap_kind != "sitemap_index":
            return ()
        payload = await self.objects.get(state.current_state_key)
        if payload is None:
            return ()
        try:
            return decode_state(payload, max_entries=self.limits.max_url_count)
        except ValueError:
            return ()

    async def check_resource(
        self,
        site_id: str,
        url: str,
        *,
        parent_id: str | None = None,
        run_namespace: str | None = None,
    ) -> _CheckedResource:
        started_at_ms = _now_ms()
        normalized_url = normalize_sitemap_url(url, max_length=self.limits.max_url_length)
        existing = await self.metadata.get_resource(site_id, normalized_url)
        resource_id = existing.resource_id if existing else resource_id_for(site_id, normalized_url)
        validators = FetchValidators(
            etag=existing.etag if existing else None,
            last_modified=existing.last_modified if existing else None,
        )
        try:
            fetched = await self.fetcher.fetch(normalized_url, validators)
        except SitemapFetchError as error:
            return await self._failure(
                site_id=site_id,
                resource_id=resource_id,
                url=normalized_url,
                parent_id=parent_id,
                started_at_ms=started_at_ms,
                error_code=error.code,
                run_namespace=run_namespace,
            )

        if fetched.status_code == 304:
            if existing is None:
                return await self._failure(
                    site_id=site_id,
                    resource_id=resource_id,
                    url=normalized_url,
                    parent_id=parent_id,
                    started_at_ms=started_at_ms,
                    error_code="unexpected_304",
                    run_namespace=run_namespace,
                    final_url=fetched.final_url,
                    http_status=304,
                )
            await self.metadata.mark_not_modified(
                site_id,
                resource_id,
                canonical_fetch_url=fetched.final_url,
                checked_at_ms=_now_ms(),
                etag=fetched.etag,
                last_modified=fetched.last_modified,
            )
            outcome = self._outcome(
                resource_id=resource_id,
                url=normalized_url,
                final_url=fetched.final_url,
                result="not_modified",
                http_status=304,
                url_count=existing.url_count,
                sitemap_kind=existing.sitemap_kind,
                state_key=existing.current_state_key,
                parent_id=parent_id,
            )
            return _CheckedResource(
                await self._load_index_entries(existing),
                await self._record(site_id, outcome, started_at_ms, run_namespace),
            )

        if fetched.status_code != 200 or fetched.body is None:
            return await self._failure(
                site_id=site_id,
                resource_id=resource_id,
                url=normalized_url,
                parent_id=parent_id,
                started_at_ms=started_at_ms,
                error_code=f"http_{fetched.status_code}",
                run_namespace=run_namespace,
                final_url=fetched.final_url,
                http_status=fetched.status_code,
                bytes_downloaded=fetched.bytes_downloaded,
            )

        try:
            document = parse_sitemap(
                fetched.body,
                fetched.final_url,
                content_type=fetched.content_type,
                limits=self.limits,
            )
        except SitemapParseError as error:
            return await self._failure(
                site_id=site_id,
                resource_id=resource_id,
                url=normalized_url,
                parent_id=parent_id,
                started_at_ms=started_at_ms,
                error_code=error.code,
                run_namespace=run_namespace,
                final_url=fetched.final_url,
                http_status=200,
                bytes_downloaded=fetched.bytes_downloaded,
            )

        fingerprint = fingerprint_document(document)
        return await self._apply_fingerprint(
            site_id=site_id,
            resource_id=resource_id,
            requested_url=normalized_url,
            parent_id=parent_id,
            existing=existing,
            fingerprint=fingerprint,
            fetched=fetched,
            started_at_ms=started_at_ms,
            run_namespace=run_namespace,
        )

    async def _apply_fingerprint(
        self,
        *,
        site_id: str,
        resource_id: str,
        requested_url: str,
        parent_id: str | None,
        existing: ResourceState | None,
        fingerprint: FingerprintedDocument,
        fetched: FetchResult,
        started_at_ms: int,
        run_namespace: str | None,
    ) -> _CheckedResource:
        # Kept structurally separate so storage commits remain easy to replace
        # with a D1/R2 adapter without changing detection semantics.
        final_url = fetched.final_url
        bytes_downloaded = fetched.bytes_downloaded
        state_key = (
            f"state/{site_id}/{resource_id}/{fingerprint.metadata_hash}.jsonl.gz"
        )
        should_write_state = (
            existing is None
            or existing.urlset_hash != fingerprint.urlset_hash
            or existing.metadata_hash != fingerprint.metadata_hash
        )
        if should_write_state:
            await self.objects.put(
                state_key,
                encode_state(fingerprint.entries),
                "application/gzip",
            )
        elif existing is not None:
            state_key = existing.current_state_key

        state = ResourceState(
            content_hash=fingerprint.content_hash,
            current_state_key=state_key,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            metadata_hash=fingerprint.metadata_hash,
            resource_id=resource_id,
            sitemap_kind=fingerprint.kind,
            url=requested_url,
            url_count=fingerprint.url_count,
            urlset_hash=fingerprint.urlset_hash,
        )

        if existing is None:
            await self.metadata.save_resource(
                site_id,
                state,
                canonical_fetch_url=final_url,
                parent_id=parent_id,
                checked_at_ms=_now_ms(),
                changed=False,
            )
            outcome = self._outcome(
                resource_id=resource_id,
                url=requested_url,
                final_url=final_url,
                result="baseline",
                http_status=200,
                bytes_downloaded=bytes_downloaded,
                url_count=fingerprint.url_count,
                sitemap_kind=fingerprint.kind,
                state_key=state_key,
                parent_id=parent_id,
            )
            return _CheckedResource(
                fingerprint.entries if fingerprint.kind == "sitemap_index" else (),
                await self._record(site_id, outcome, started_at_ms, run_namespace),
            )

        if existing.urlset_hash == fingerprint.urlset_hash:
            await self.metadata.save_resource(
                site_id,
                state,
                canonical_fetch_url=final_url,
                parent_id=parent_id,
                checked_at_ms=_now_ms(),
                changed=False,
            )
            outcome = self._outcome(
                resource_id=resource_id,
                url=requested_url,
                final_url=final_url,
                result="semantic_unchanged",
                http_status=200,
                bytes_downloaded=bytes_downloaded,
                url_count=fingerprint.url_count,
                sitemap_kind=fingerprint.kind,
                state_key=state_key,
                parent_id=parent_id,
            )
            return _CheckedResource(
                fingerprint.entries if fingerprint.kind == "sitemap_index" else (),
                await self._record(site_id, outcome, started_at_ms, run_namespace),
            )

        previous_payload = await self.objects.get(existing.current_state_key)
        if previous_payload is None:
            return await self._failure(
                site_id=site_id,
                resource_id=resource_id,
                url=requested_url,
                parent_id=parent_id,
                started_at_ms=started_at_ms,
                error_code="previous_state_missing",
                run_namespace=run_namespace,
                final_url=final_url,
                http_status=200,
                bytes_downloaded=bytes_downloaded,
            )
        try:
            previous_entries = decode_state(
                previous_payload,
                max_entries=self.limits.max_url_count,
            )
        except ValueError:
            return await self._failure(
                site_id=site_id,
                resource_id=resource_id,
                url=requested_url,
                parent_id=parent_id,
                started_at_ms=started_at_ms,
                error_code="previous_state_invalid",
                run_namespace=run_namespace,
                final_url=final_url,
                http_status=200,
                bytes_downloaded=bytes_downloaded,
            )

        diff = diff_states(previous_entries, fingerprint.entries)
        detected_at_ms = _now_ms()
        diff_identity = (
            stable_id("smdiff", run_namespace, resource_id, fingerprint.urlset_hash)
            if run_namespace
            else f"{detected_at_ms}-{fingerprint.urlset_hash[:12]}"
        )
        diff_key = f"diff/{site_id}/{resource_id}/{diff_identity}.json.gz"
        await self.objects.put(diff_key, encode_diff(diff), "application/gzip")
        await self.metadata.save_resource(
            site_id,
            state,
            canonical_fetch_url=final_url,
            parent_id=parent_id,
            checked_at_ms=detected_at_ms,
            changed=True,
        )
        outcome = self._outcome(
            resource_id=resource_id,
            url=requested_url,
            final_url=final_url,
            result="changed",
            http_status=200,
            bytes_downloaded=bytes_downloaded,
            url_count=fingerprint.url_count,
            sitemap_kind=fingerprint.kind,
            state_key=state_key,
            diff_key=diff_key,
            added_count=len(diff.added),
            removed_count=len(diff.removed),
            modified_count=len(diff.modified),
            parent_id=parent_id,
        )
        return _CheckedResource(
            fingerprint.entries if fingerprint.kind == "sitemap_index" else (),
            await self._record(site_id, outcome, started_at_ms, run_namespace),
        )

    async def scan_site(
        self,
        homepage_url: str,
        *,
        explicit_sitemaps: Iterable[str] = (),
        run_namespace: str | None = None,
    ) -> SiteScanResult:
        scan_started_at_ms = _now_ms()
        normalized_homepage = normalize_sitemap_url(
            homepage_url,
            max_length=self.limits.max_url_length,
        )
        site_id = site_id_for(normalized_homepage)
        scan_namespace = run_namespace or stable_id(
            "smadhoc",
            site_id,
            str(time.time_ns()),
        )
        scan_id = stable_id("smscan", scan_namespace)
        await self.metadata.ensure_site(site_id, normalized_homepage, _now_ms())

        robot_sitemaps: tuple[str, ...] = ()
        try:
            robot_response = await self.fetcher.fetch(
                robots_url(normalized_homepage, self.limits),
                max_download_bytes=self.limits.robots_max_bytes,
            )
            if robot_response.status_code == 200 and robot_response.body is not None:
                robot_sitemaps = parse_robots_sitemaps(
                    robot_response.body,
                    robot_response.final_url,
                    limits=self.limits,
                )
        except SitemapFetchError:
            pass

        explicit = tuple(explicit_sitemaps)
        authoritative = discovery_candidates(
            normalized_homepage,
            explicit_sitemaps=explicit,
            robots_sitemaps=robot_sitemaps,
            include_common_paths=False,
            limits=self.limits,
        )
        roots = authoritative or discovery_candidates(
            normalized_homepage,
            include_common_paths=True,
            limits=self.limits,
        )
        discovery_mode = (
            "explicit"
            if explicit
            else "robots"
            if robot_sitemaps
            else "fallback"
        )

        queue = deque((url, None, 0) for url in roots)
        visited: set[str] = set()
        outcomes: list[CheckOutcome] = []
        traversal_reason_codes: list[str] = []
        fallback_mode = not authoritative
        found_fallback = False
        while queue:
            url, parent_id, depth = queue.popleft()
            if url in visited:
                continue
            if len(visited) >= self.limits.max_resources_per_site:
                traversal_reason_codes.append("resource_limit_reached")
                break
            if depth > self.limits.max_index_depth:
                traversal_reason_codes.append("index_depth_limit_reached")
                continue
            if fallback_mode and parent_id is None and found_fallback:
                continue
            visited.add(url)
            checked = await self.check_resource(
                site_id,
                url,
                parent_id=parent_id,
                run_namespace=scan_namespace,
            )
            outcomes.append(checked.outcome)
            if fallback_mode and parent_id is None and checked.outcome.result != "failed":
                found_fallback = True
            if checked.outcome.sitemap_kind == "sitemap_index":
                for entry in checked.index_entries:
                    # Sitemap protocol requires index children to share the index
                    # host. This also prevents a compromised index from turning
                    # the monitor into an arbitrary public-web crawler.
                    if urlsplit(entry.normalized_url).hostname != urlsplit(
                        checked.outcome.final_url
                    ).hostname:
                        continue
                    queue.append((entry.normalized_url, checked.outcome.resource_id, depth + 1))

        return SiteScanResult(
            discovery_mode=discovery_mode,
            finished_at_ms=_now_ms(),
            homepage_url=normalized_homepage,
            outcomes=tuple(outcomes),
            scan_id=scan_id,
            site_id=site_id,
            started_at_ms=scan_started_at_ms,
            traversal_complete=not traversal_reason_codes,
            traversal_reason_codes=tuple(dict.fromkeys(traversal_reason_codes)),
        )
