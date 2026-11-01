# Scripts

| Path | Role |
|---|---|
| `check-deployment-contract.py` | CI: validate rendered Dokploy Compose |
| `check-market-publication.py` | Ops: inspect market snapshot readiness |
| `check-search-demand-publication.py` | Ops: dry-run search-demand publish |
| `sync-report-runtime.py` | CI/deploy: verify report-runtime bundle |
| `crawl_ai_directories.py` | Maintained crawler (see `docs/ai-directory-crawler.md`) |
| `build_revision.py` | Fingerprint shipped source for image audits |
| `sql/cutover-preflight.sql` | Historical cutover checklist SQL |
| `once/*_once.py` | **One-shot** sitemap/Jev seed experiments — not used by Dokploy workers |

Run one-shot scripts from `scripts/once/` so sibling imports resolve:

```bash
cd scripts/once
python filter_sitemap_ai_relevance_once.py ...
```
