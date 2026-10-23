# Production deployment

The existing Dokploy Compose service is `sigpik数据采集`, app name
`service-sigpik-h4m21a`, compose ID `c5v_jBd3UTUgsbGIsWVrX`.
Its GitHub source is `ningshenlo/tool-data-runner`, Compose path
`./docker-compose.dokploy.yml`, deployment branch `deploy/production`.

## Test gate

Develop on `agent/fix-traffic-runner-observability`. The `Report runtime` workflow
validates the rendered Compose, builds both images, checks imports inside the
actual collector image, runs offline regression tests, and renders synthetic
reports with read-only input volumes and no network. Only a successful run on
that source branch can advance `deploy/production` to its exact tested SHA.
PR runs and the historical reports branch cannot promote production.

Dokploy's GitHub provider watches only `deploy/production`, with On Push and
Autodeploy enabled. Do not point it back at the source branch or manually push
untested commits to the deployment branch. No Dokploy administrator API key or
database password is stored in GitHub Actions. The promotion job uses the
repository-scoped GitHub Actions token with contents write permission.

The promotion step rejects stale source runs and uses a normal fast-forward
push. A divergent release branch fails closed; do not solve that with a force
push. Revert through the source branch and run CI to roll back application code.
Never roll back to a D1-era deployment.

## PostgreSQL and preserved resources

The database is the existing production Pigsty PostgreSQL service under project
`sigpik-postgresql-production`, environment `database-production`.
Keep `SIGPIK_DATABASE_URL` in Dokploy Environment. It uses the existing jobs role,
database `sigpik`, `sslmode=verify-full`, and `/run/pg-ca/origin-ca.pem`.
Do not copy its value into source control, build arguments, CI or reports.

The following resource names intentionally retain `test`; they are production:

* External network: `ainav-pg-vpc-test-20261019`.
* CA source volume: `ainav-pigsty-test-dnkttj_pigsty_data`, only the
  `sigpik-public-ca` subdirectory mounted read-only at `/run/pg-ca`.
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
