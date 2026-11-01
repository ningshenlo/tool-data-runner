# tool-data-runner Dokploy CI deployment repair

Date: 2026-10-23 (Asia/Shanghai). Status: complete; automatically deployed and runtime verified.

Final commit: `f92bc1cedab1cdfc11068b3aa212594e1615db73`.
Both the source branch and `deploy/production` point to this commit.
[Final successful CI run](https://github.com/ningshenlo/tool-data-runner/actions/runs/39900991466).
Dokploy automatically deployed this exact commit successfully in 4 seconds.

## Original configuration

The existing service `c5v_jBd3UTUgsbGIsWVrX` / `service-sigpik-h4m21a`
used Raw Compose and local `pg-6d04c5f` images with `pull_policy: never`.
Autodeploy was enabled, but the service was not connected to its source repository.
The repository Compose lacked the production PostgreSQL environment, network,
fixed addresses and CA mount. Six production containers existed; Sitemap was
enabled with one replica. No fresh volumes, service deletion or database restart
was performed in this task.

## Changes

* Preserved every named volume, both network definitions and all service IPs.
* Restored GitHub source using `./docker-compose.dokploy.yml` and
  `deploy/production`, On Push, Autodeploy enabled.
* CI builds both images, checks actual packaged imports, runs 47 Python tests,
  Node regression tests and an offline report render/restart smoke test.
* CI validates the PostgreSQL mounts/networks and both Sitemap switch states.
* Successful source-branch CI fast-forwards only the exact tested commit to
  `deploy/production`; stale runs and PRs cannot promote production.
* Removed retired costguard test references; isolated market SQL activation
  fixtures from the sibling Sigpik repository.
* No administrator token or production database credential added to GitHub.

## TLS correction discovered during live acceptance

The first automatically deployed commit was `32028d3c5af4ea1a89c1152e71bd70694400d854`.
Its CI passed and Dokploy deployed it automatically in 33 seconds. Live logs
revealed a pre-existing connection configuration mismatch: PostgreSQL had
restarted at `2026-10-22 12:33:23.663539+08` and presented its native Pigsty
certificate; the collector still expected the Origin CA and old DNS identity.

Verified the database's active `/pg/cert/server.crt` against `/pg/cert/ca.crt`
and hostname `ainav-test-1`. Copied only the public CA to the existing shared
public-CA directory as `pigsty-ca.crt`. CA SHA-256 fingerprint:
`7B:91:C9:96:D4:93:8A:F9:B7:D5:CC:4E:77:8F:4B:71:73:D5:F5:43:93:9B:FC:AB:04:B1:19:9C:30:18:89:E6`.

Changed only the TLS/routing parameters of Dokploy's `SIGPIK_DATABASE_URL`:
host `ainav-test-1`, `hostaddr=172.22.0.2`, CA `/run/pg-ca/pigsty-ca.crt`.
Retained `sslmode=verify-full`, database, username, password and connection timeout.
All other environment content was compared and remained unchanged.
A read-only query from the collector returned `sigpik`, `sigpik_jobs`, SSL true,
TLSv1.3. No private key was copied or exposed.

## Final runtime acceptance

All six expected containers reported running/healthy after deployment. The
report draft container was retained by Compose because its configuration/image
did not change. The updated collector processes used the corrected connection.

Observed at 12:34–12:36 Beijing time:

* Domain facts: queued/claimed/completed one task, failed zero; subsequent idle
  poll also completed normally.
* Traffic: release available, batch failed zero, eight complete report exports;
  two report exports were business-data blocked, not deployment failures.
* Taxonomy: completed a batch poll and entered the normal idle interval, with
  no database/provider errors in the observed pass.
* Sitemap: continuously processed resources and reported semantic comparisons
  and changed URLs (including akool.com and loadsmart.com).
* Report review API and offline draft worker: healthy.

This is a bounded deployment smoke check, not exhaustive validation of every
external provider or every data-dependent task path.

## Evidence and retention

`compose-before.yml` is a credential-free rollback reference for resource names
and former images; keep until the new pipeline has completed its normal
operational cycle. It does not contain the obsolete database URL, and that URL
must not be restored. `baseline.json` contains only selected non-secret settings.
`ci-result.json` records GitHub test/promotion results; `containers.txt` and
`containers.png` record the six healthy containers. Long-term operating
instructions are in `docs/production-deployment.md`.

Temporary files under `work/20261023-dokploy-ci` were removed after final
verification; existing unrelated `logs/` remains untouched.
