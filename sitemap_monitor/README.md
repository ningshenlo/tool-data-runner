# sitemap-monitor

`sitemap_monitor` 是 Sitemap Change Intelligence Engine 的 Phase 1 检测模块。

当前范围：

- robots.txt、显式 URL 和常见路径 discovery；
- sitemap index、urlset、text sitemap、`.xml.gz` 和 HTTP gzip；
- Conditional GET（ETag / Last-Modified），不先发 HEAD；
- 保守 URL normalize；
- `content_hash`、`urlset_hash`、`metadata_hash` 三层指纹；
- 首次扫描只建立 baseline；
- 后续用排序后的 JSONL gzip state 做 O(N) added/removed/modified diff；
- sitemap index 递归、循环去重、深度/资源数/URL 数/下载大小/解压大小限制；
- index 子 sitemap 必须与 index 最终 URL 同 host，避免被滥用为任意站点爬虫；
- D1/SQLite due-site scheduler，只领取 `next_check_at <= now` 的 active site；
- `sitemap_jobs` 持久任务账本，使用确定性 idempotency key、过期租约和 completion fencing；
- 每个 job attempt 汇总为站点级 `sitemap_site_scans`，resource run 通过 `site_scan_id` 可追溯；
- Comparability Gate 输出 `comparable`、`partial`、`resource_set_changed`、`possible_migration`、`fetch_incomplete` 或 `baseline_invalid`；
- 同时保存 raw/normalized resource set hash，识别常见动态 sitemap 分片 family；
- 抓取状态与 semantic baseline 分离；不完整扫描会留档，但不能替换最后一个有效语义基线；
- 成功站点默认每 6 小时检查；只有 DNS、超时、429、5xx 等瞬态故障在同一 job 内指数退避重试；
- 404、非法 XML 等确定性失败一次即进入 `dead`，站点按连续失败轮次采用 24 小时、72 小时、7 天冷却；
- 每 6 小时归档被 schedule version 替代的旧任务，并分批清理 7 天前的失败/unchanged run、30 天前的非信号扫描与无引用终态 job；
- 进程内每小时才重新确认一次站点配置，D1 upsert 在配置无变化时不写入，避免 30 秒轮询造成写放大；
- 任务执行期间续租，旧 Worker 的迟到完成不能推进站点 schedule；
- 本地 SQLite + 文件对象存储，schema 和对象 key 分别兼容 D1/R2 adapter；
- 可选 Cloudflare backend：D1 只保存 metadata/run，R2 保存 state/diff；默认仍为 local。

当前 D1 job ledger 同时充当耐久队列和幂等真相源。尚未接 Cloudflare Queue
transport；以后即使接入，Queue message 也只携带 `job_id`，消费前仍必须竞争 D1
lease。当前不包含 ChangeEvent 产品化、URL 聚类、页面抽样、LLM 或 Adaptive
Polling，这些属于后续阶段。

`baseline_invalid` 表示当前还没有可比较的旧语义基线。首次完整扫描会以该状态
安全建立 semantic baseline，但不会被当作“全站新增”。后续只有
`is_comparable=true` 的扫描才能进入 P1/P2 解释层。

## 本地运行

```bash
python -m sitemap_monitor --site https://example.com --once
python -m sitemap_monitor --site https://example.com --sitemap https://example.com/custom.xml --json
python -m sitemap_monitor --site https://example.com --loop \
  --interval-seconds 30 --check-interval-seconds 21600
```

`--once` 现在表示“运行一个 scheduler tick”。新站点会立即建立 baseline；尚未到
`next_check_at` 的已有站点不会被重复扫描。`--interval-seconds` 是 scheduler poll
频率，`--check-interval-seconds` 才是成功后的站点检查周期。

维护参数默认值为：每 6 小时维护一次、失败与 unchanged run 保留 7 天、普通扫描
和无引用终态 job 保留 30 天，每轮每类最多处理 500 条。`baseline`、`changed`、
当前 semantic baseline，以及 `resource_set_changed` / `possible_migration` 审计扫描
不会被这套常规清理策略删除。

默认状态目录为 `.sitemap-monitor/`：

```text
.sitemap-monitor/
  metadata.sqlite3
  objects/
    state/{site_id}/{resource_id}/{metadata_hash}.jsonl.gz
    diff/{site_id}/{resource_id}/{timestamp}-{urlset_hash}.json.gz
```

SQLite/D1 中的关键运行状态：

```text
sitemap_sites.next_check_at
  → sitemap_jobs(idempotency_key unique)
  → pending/running/retry
  → succeeded 或 dead
  → sitemap_site_scans(comparability gate)
  → sitemap_sites.semantic_baseline_scan_id
```

幂等键由 `site_id + scheduled_for + schedule_version` 确定。Worker 每次领取都会
获得新的 `lease_token`；完成更新必须匹配该 token。

## 测试

```bash
python -B -m unittest -v test_sitemap_monitor.py
```

测试全部使用 fake HTTP 和临时存储，不访问外网、D1 或 R2。

## Cloudflare backend（默认不启用）

先通过 ainav 的正式 migration 流程应用 sitemap schema，再配置 D1 与 R2
凭据。模块不会自行建表或执行 migration：

```bash
python -m sitemap_monitor \
  --backend cloudflare \
  --site https://example.com \
  --once
```

需要的环境变量：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_D1_DATABASE_ID`
- `CLOUDFLARE_API_TOKEN`
- `FOR_ALL_APP_R2_ACCESS_KEY_ID`
- `FOR_ALL_APP_R2_SECRET_ACCESS_KEY`
- `SITEMAP_MONITOR_R2_BUCKET`（未设置时回退 `CLOUDFLARE_R2_BUCKET`）
- `SITEMAP_MONITOR_SITES`（逗号分隔，可替代重复的 `--site`）
- `SITEMAP_MONITOR_SITE_FILE`（每行一个站点，支持空行和 `#` 注释；首批观察默认使用固定 cohort 文件）
- `SITEMAP_MONITOR_CHECK_INTERVAL_SECONDS`（成功站点默认 `21600`，即 6 小时）
- `SITEMAP_MONITOR_MAINTENANCE_INTERVAL_SECONDS`（默认 `21600`）
- `SITEMAP_MONITOR_RUN_DETAIL_RETENTION_DAYS`（默认 `7`）
- `SITEMAP_MONITOR_SCAN_DETAIL_RETENTION_DAYS` / `SITEMAP_MONITOR_JOB_RETENTION_DAYS`（默认 `30`）
- `SITEMAP_MONITOR_SYNTHETIC_DNS_DOH_FALLBACK`（默认 `0`；仅在本机 DNS 被可信网络代理映射为合成地址时临时设为 `1`，通过固定 Cloudflare DoH 二次验证公网 A/AAAA；生产通常保持关闭）

## Observation cohort

首个真实连续观察名单固定在：

```text
sitemap_monitor/observation-cohort-v1.txt
```

它包含 ainav 旧主分类 `games-entertainment-lifestyle` 中 38 个非 NSFW 站点。
该名单只允许执行 fetch、site scan、comparability、baseline 和后续 family snapshot；
不得直接生成、发布或展示 Signal。
