# Tool Data Runner：Dokploy Worker 拆分实施记录

日期：2026-08-13 14:55（Asia/Shanghai）

## 目标

将原来一个 `python runner.py --all --loop` 容器拆成四个独立运行单元，解决日志混杂、故障域过大、所有 workload 共用并发参数以及无法独立调优的问题。采集任务表、D1 租约、generation fencing 和业务写入逻辑保持不变。

## 部署拓扑

```text
Dokploy Compose
├── periodic-facts-worker
│   ├── Similarweb Traffic
│   └── Ahrefs DR + RDAP
├── assets-worker
├── pricing-monitor-worker
└── taxonomy-worker
```

四个 service 使用同一个 Docker image，通过不同命令和环境变量选择 workload：

```text
python runner.py --periodic-facts --loop --interval-seconds 300
python runner.py --assets --loop --interval-seconds 150
python runner.py --pricing --loop --interval-seconds 900
python runner.py --taxonomy --loop
```

部署定义见 `docker-compose.dokploy.yml`。

## 行为边界

- Traffic 与 Domain Facts 保留在同一 `periodic-facts-worker`，但内部仍是两条独立 coroutine、独立并发参数、独立 D1 workload run。
- DR 继续按 `RUNNER_DOMAIN_STATE_MAX_AGE_DAYS=30` 刷新。
- RDAP 生命周期逻辑此次没有修改；当前代码仍会持久化 `done`、`no_data` 和 `failed` 检查结果，后续 DR 刷新不会重复 RDAP。
- Pricing、Assets、Taxonomy 的采集和写入算法均未因部署拆分而变化。
- `--all --loop` 暂时保留作为回滚入口，但会输出 deprecated 日志；不再作为生产默认拓扑扩展。

## 配置隔离

新增 workload 级并发配置：

```text
RUNNER_TRAFFIC_CONCURRENCY
RUNNER_DOMAIN_CONCURRENCY
RUNNER_ASSET_CONCURRENCY
RUNNER_PRICING_CONCURRENCY
```

未设置时仍回退到旧的 `RUNNER_CONCURRENCY`，便于渐进迁移。

每个 service 使用独立的：

```text
RUNNER_SERVICE_NAME
RUNNER_INSTANCE_ID
```

Telemetry 的 `runner_instances.service` 和 `workloads_json` 现在反映真实部署边界；运行期间还会把 workload heartbeat 写入 `metadata_json.workload_heartbeats`。

## 日志策略

默认 `RUNNER_LOG_LEVEL=info`：

- 保留 loop 启动、batch summary、重试/异常和非成功 task outcome。
- 成功 task 的 start/done 与正常 D1 明细降到 `debug`。
- 每条结构化日志自动包含 timestamp、service、instance_id、workloads；telemetered batch 内同时包含 workload。
- batch summary 增加 `duration_ms`。

临时排查单个任务时，可以只对目标 service 设置：

```text
RUNNER_LOG_LEVEL=debug
```

## 上线顺序

1. 在 Dokploy 创建 Compose 项目并使用 `docker-compose.dokploy.yml`。
2. 配置公共 Cloudflare D1 环境变量及各 service 所需 Provider Secret。
3. 为四个 service 设置稳定且互不相同的 instance ID。
4. 启动四个新 service；允许它们与旧 `--all` 容器短暂重叠。
5. 在 D1 `runner_instances` / `runner_runs` 和 Dokploy 日志中确认四个 service 均产生健康心跳与 batch summary。
6. 停止旧 `--all` 容器。
7. 保留旧部署配置一个发布周期用于回滚，但不要同时长期运行。

短暂重叠不会导致同一任务被重复提交：现有 D1 lease、lease token 和 generation fencing 仍是唯一执行协调机制。

## 回滚

如果任一拆分 service 无法稳定运行：

1. 停止四个拆分 service。
2. 重新启动旧的 `python runner.py --all --loop --interval-seconds 300`。
3. 等待已领取任务的 lease 过期后，它们会被旧实例重新领取。

不需要修改或回滚 D1 schema。
