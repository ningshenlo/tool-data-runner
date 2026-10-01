# Sitemap Scheduler 与幂等队列地基（2026-08-14 09:52）

## 决策

将 due-site scheduler、任务幂等、租约、失败退避和 dead-letter 状态提升为
Sitemap Monitor Phase 1 的上线前置条件，而不是 Phase 5 性能优化。

当前运行形态继续使用 Python `tool-data-runner`。D1 的 `sitemap_jobs` 同时作为：

1. 调度器与执行器之间的耐久任务账本；
2. 当前阶段的可轮询队列；
3. 未来接入 Cloudflare Queue 后的幂等真相源。

Cloudflare Queue 是至少一次投递，因此未来 Queue message 只携带 `job_id`。消息到达
后仍然必须先竞争 D1 job lease；不把 Queue message id 当成业务幂等保证。

## 调度状态

站点保存：

- `check_interval_sec`
- `schedule_version`
- `next_check_at`
- `last_attempt_at` / `last_success_at`
- `dispatch_lease_owner` / `dispatch_lease_token` / `dispatch_lease_expires_at`
- `error_streak`

调度器通过单条 `UPDATE ... RETURNING` 原子领取：

```text
status = active
AND next_check_at <= now
AND dispatch lease 不存在或已过期
```

改变 homepage 或检查周期会增加 `schedule_version`，释放旧 dispatch lease，并使新的
调度周期立即到期。旧 schedule version 的任务不能推进新 schedule。

## Job 幂等与状态机

任务幂等键：

```text
SHA256(site_id + scheduled_for + schedule_version)
```

D1 同时设置：

```sql
UNIQUE(idempotency_key)
UNIQUE(site_id, scheduled_for, schedule_version)
```

状态机：

```text
pending
  → running
  → succeeded
  → retry → running
  → dead
```

每次领取 `running` 时增加 `attempts` 并生成新的 `lease_token`。处理期间按租约的
1/3 周期续租。完成 SQL 必须同时匹配 `job_id + running + lease_token`；租约过期后
被重新领取的任务会获得新 token，因此旧 Worker 的迟到完成会被忽略。

已过期的 `running` 即使刚好是最后一次预算也允许恢复领取，避免 Worker 在最终
attempt 崩溃后任务永久卡死；恢复执行完成后才根据结果进入 `succeeded` 或 `dead`。

同一个 job 的每次实际尝试还会生成确定性 run namespace：

```text
SHA256(job_id + attempts)
```

resource run key 与 diff object key 基于该 namespace，避免同一次投递重入产生两份
run/diff 副作用，同时保留真正 retry attempt 的审计记录。

## 时间策略

- 成功：`next_check_at = finished_at + check_interval + deterministic jitter`
- 可重试失败：指数退避，默认从 60 秒开始，最大 1 小时
- 达到 `max_attempts`：job 进入 `dead` 并写 `dead_letter_at`
- dead 后：站点安排未来一次新的 schedule occurrence，不会永久停止监控
- jitter 由 job key 确定，测试可复现，同时避免大量站点整点同时请求

第一次常见路径 discovery 可能先得到若干 404，随后找到有效 sitemap。因此站点级
成功定义为“至少一个 sitemap resource 成功”，而不是“所有尝试都成功”。资源自身
仍保留 missing/error streak，未来 Change Engine 可对部分失败做确认。

## 部署边界

- 更新了未应用的 `0060_sitemap_monitor_phase1.sql`；没有执行远程 migration。
- 没有创建 Cloudflare Queue、D1 表或 R2 bucket。
- `sitemap-monitor-worker` 继续保持默认 `replicas=0` 和
  `SITEMAP_MONITOR_ENABLED=0` 双门禁。
- loop 的 `--interval-seconds` 现在是 scheduler poll 周期；站点周期独立使用
  `--check-interval-seconds`。

## 本地验证范围

专项测试覆盖：

- due-site 原子领取与 dispatch lease；
- 重复建 job 只产生一行；
- job 租约互斥、过期恢复和 attempts 增长；
- stale completion fencing；
- retry available time、max attempts 和 dead-letter；
- 成功推进 `next_check_at`，未到期 tick 不重复扫描；
- SQLite 与 Cloudflare D1 adapter 使用同一状态契约。

下一步仍需在显式批准后完成远程 canary，验证真实 D1 REST 行为、多个 Worker 并发、
网络中断和长任务续租；本次不包含部署或真实网站扫描。

本地结果：Sitemap Monitor 专项 24/24 通过；全仓 216 项中 215 通过。唯一失败仍是
既有的 market entitlement plan matrix 测试：数据已有 `starter`，旧断言仍只期待
`free/pro/enterprise`，与本次 scheduler 改动无关。
