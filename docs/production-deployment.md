# Production deployment

The existing Dokploy Compose service is `sigpik数据采集`, app name
`service-sigpik-h4m21a`, compose ID `c5v_jBd3UTUgsbGIsWVrX`.
Its GitHub source is `ningshenlo/tool-data-runner`, Compose path
`./docker-compose.dokploy.yml`, deployment branch `main`.

## Test gate

Develop and release only on `main`. The `Report runtime` workflow validates the
rendered Compose, builds both images, checks imports inside the actual collector
image, runs offline regression tests, and renders synthetic reports with
read-only input volumes and no network. PR runs do not deploy.

Dokploy's GitHub provider watches only `main`, with On Push and Autodeploy
enabled. A push to `main` starts CI and Dokploy in parallel — there is no
separate `deploy/production` promotion branch. Do not retarget Dokploy to
legacy branches (`deploy/production`, `agent/fix-traffic-runner-observability`,
etc.). Revert on `main` and push again to roll back application code. Never
roll back to a D1-era deployment. Never force-push `main` unless the current
task explicitly authorizes it.

### One-time cutover from the old dual-branch setup

1. Fast-forward remote `main` to the commit that is live on
   `deploy/production` (do this **before** changing Dokploy, or the next
   Autodeploy would roll production backward).
2. In Dokploy → General: set Branch to `main`, keep Compose path
   `./docker-compose.dokploy.yml`, Trigger `On Push`, Autodeploy on. Save.
   Do not click Deploy / Rebuild / Fresh Volumes just to flip the branch.
3. Later pushes go only to `main`. Leave old branches untouched.

## PostgreSQL and preserved resources

The database is the existing production Pigsty PostgreSQL service under project
`sigpik-postgresql-production`, environment `database-production`.
Keep `SIGPIK_DATABASE_URL` in Dokploy Environment. It uses the existing jobs role,
database `sigpik`, `sslmode=verify-full`, and `/run/pg-ca/origin-ca.pem`.
The TLS identity is `ainav-pg-test.rugchecker.com`; `hostaddr=172.22.0.2`
routes directly to the existing private PostgreSQL address. This keeps server
identity verification enabled without requiring a public DNS change.
Do not copy its value into source control, build arguments, CI or reports.

The following resource names intentionally retain `test`; they are production:

* External network: `ainav-pg-vpc-test-20261019`.
* CA source volume: `ainav-pigsty-test-dnkttj_pigsty_data`, only the
  `sigpik-public-ca` subdirectory mounted read-only at `/run/pg-ca`.
  `origin-ca.pem` is the public Cloudflare Origin CA used by the database's
  VPC certificate. Never mount its private key or the entire database volume.
* Fixed IPs: assets `.21`, periodic facts `.22`, taxonomy `.24`, sitemap `.25`,
  review API `.20`, all on `172.22.0.0/24`.
* Default network: `service-sigpik-h4m21a_default`.
* Data volumes: `sigpik-market-snapshots`, `sigpik-report-exports`,
  `sigpik-report-drafts`.

Keep service settings in the existing Dokploy Environment. In particular,
production has `SITEMAP_MONITOR_ENABLED=1` and `SITEMAP_MONITOR_REPLICAS=1`;
repository defaults remain safe-off for other environments. The draft renderer
retains `network_mode: none` and read-only code/input mounts.
Never use Fresh Volumes or `down -v` when deploying this service. Do not run a
second copy of the production Compose because it uses fixed IPs and shared state.

After deployment, verify all six expected containers and their health, the
deployment SHA, and worker logs/heartbeats. Process liveness alone does not prove
successful database access. PostgreSQL schema migrations are separate from this
pipeline; this deployment performs no migration or database restart.

On 2026-10-23, diagnosis of the homepage failure found that the database's
2026-10-22 restart had reverted its TLS certificate to the native Pigsty one.
Temporarily aligning only the collector to that certificate did not repair
Hyperdrive: its VPC service requires the Origin CA certificate and matching
hostname. Both consumers must use the same production certificate contract.
Keep the production certificate paths in Patroni configuration and the
persistent Pigsty inventory; a one-off ALTER SYSTEM override alone did not
survive container bootstrap. Never disable TLS verification or switch only
one consumer to the native Pigsty certificate as a permanent repair.
