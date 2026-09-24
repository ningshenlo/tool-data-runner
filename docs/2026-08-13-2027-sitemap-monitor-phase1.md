# Sitemap Monitor Phase 1 实现记录（2026-08-13 20:27）

> 后续更新：2026-08-14 已将 due-site scheduler 与 D1 job 幂等账本加入 Phase 1
> 上线门禁，设计与验证见 `docs/2026-08-14-sitemap-scheduler-foundation.md`。

## 目标与边界

依据《Sitemap Change Intelligence Engine 实现方案》先完成 Phase 1，确保 URL
新增/移除检测准确。当前不接 ChangeEvent 产品层、URL 聚类、页面抽样、LLM、
Adaptive Polling 或 Queue；这些仍属于后续阶段。

本次只创建本地代码、测试、正式 migration 文件和默认关闭的服务配置。没有执行
D1 migration，没有创建/写入 R2，也没有部署或扫描真实网站。

## 模块结构

`sitemap_monitor/` 是独立 Python package，不继续扩大已有的 `runner.py`：

- `normalize.py`：保守 URL normalize，保留 path 大小写、query 和 trailing slash。
- `parser.py`：增量 XML 解析 urlset/sitemap index，并支持 text sitemap。
- `fetch.py`：Conditional GET、手动安全 redirect、gzip 解压与下载/解压限额。
- `fingerprint.py`：`content_hash`、`urlset_hash`、`metadata_hash`。
- `diff.py`：对 url-hash 排序 state 做 O(N) merge diff。
- `codec.py`：确定性 JSONL gzip state 与 JSON gzip diff。
- `engine.py`：robots discovery、递归 index、baseline/304/semantic/change 状态机。
- `storage.py`：本地 SQLite metadata + 文件 object store。
- `cloudflare.py`：参数化 D1 REST metadata store + 私有 R2 S3 SigV4 object store。
- `cli.py`：独立 `python -m sitemap_monitor` 入口。

## 关键正确性决策

1. 第一次成功扫描只写 baseline，added/removed/modified 全部为 0。
2. XML 序列化或排序变化只改变 `content_hash`；`urlset_hash` 不变时不生成 diff。
3. metadata-only 变化保存新 state，供未来真实 diff 使用，但本阶段只记
   `semantic_unchanged`。
4. index 返回 304 时从已有 object state 恢复子 sitemap，继续递归检查，避免漏扫。
5. index 子 sitemap 必须与 index 最终 URL 同 host。
6. 禁止 DOCTYPE/ENTITY、非 HTTP(S)、凭据 URL、private/reserved IP 和 redirect 到私网。
7. 不发 HEAD；直接发带 ETag/Last-Modified 的 Conditional GET。
8. D1 只保存 site/resource/run，R2 只保存 state/diff，不把 URL 明细写入 D1。
9. R2 state 使用 content-addressed/metadata-addressed immutable key，避免 metadata
   提交失败时覆盖唯一旧 baseline。

## 生产门禁

- migration：`ainav/d1/migrations/0060_sitemap_monitor_phase1.sql`，仅生成，未执行。
- Dokploy：`sitemap-monitor-worker` 默认 `replicas=0`。
- 进程门禁：服务还要求 `SITEMAP_MONITOR_ENABLED=1`。
- 默认 backend：`local`；Cloudflare backend 必须显式指定。

## 验证结果

- Sitemap Monitor 专项：17/17 通过，全离线。
- 覆盖 baseline、semantic unchanged、真实 diff、304 index 递归、跨 host 阻断、
  gzip/限额、XML 防御、redirect、D1 adapter、R2 签名/安全 key 和部署门禁。
- 全仓 unittest：206 项中 205 通过；唯一失败是既有
  `test_market_entitlement_seed_has_stable_plan_matrix`，当前数据含 `starter`，旧测试仍
  期待 `free/pro/enterprise`，与 Sitemap Monitor 改动无关。

## 后续顺序

1. 审核 0060 migration、私有 R2 bucket 和首批小规模站点；获得明确批准后再迁移。
2. 先以 local 或 Cloudflare dry cohort 建 baseline，观察 304 rate、semantic unchanged
   rate、bytes/check 与错误率。
3. Phase 2 再实现 ChangeEvent、missing confirmation、diff timeline 和 Admin 查询。
4. Phase 3 再实现 path clustering、deterministic classification 与 priority/noise filter。
