# tool-data-runner 代理工作约定

适用于本项目及其子目录。先遵守工作区根 AGENTS.md，再阅读本项目 README.md。
部署维护细节见 [docs/production-deployment.md](docs/production-deployment.md)。
源码布局与 worker 边界见 [contracts/README.md](contracts/README.md)：`workloads/` 按 Dokploy 服务拆分，`config/` 放静态常量，`libs/` 放可复用工具函数，`shared/` 放 DB 与就绪门槛，`runner.py` 仍是 CLI 入口。

## 统一开发与发布分支

- 只使用 `main`：日常提交、推送、Dokploy 跟踪分支都是它。不要再往 `deploy/production` 或 `agent/fix-traffic-runner-observability` 发发布。
- 历史分支可留作只读参考，不得再作为生产入口，也不得强推改写。
- 不得擅自把 Dokploy 改到非 `main` 的分支。

## 唯一常规部署流程

`推送 main → Report runtime CI 跑检查；Dokploy（Branch=main, On Push + Autodeploy）拉取同一推送并构建部署`

- CI 定义在 `.github/workflows/report-runtime.yml`，包括 Compose 配置契约、两个镜像构建、镜像内导入检查、离线回归和报表生成检查。
- CI 与 Dokploy 会并行触发：CI 失败时立刻在源分支修并再推，必要时用 revert 回滚；不得点击 Deploy、Rebuild、Stop、Fresh Volumes，也不得通过 API、Webhook、SSH 或生产 `docker compose up` 另行发布。
- PR 检查通过本身不触发生产发布；只有合并/推送到 `main` 才会让 Dokploy 部署。
- 不再上传本地镜像、切回 Raw Compose 或在生产容器内直接修改源码。维护源是本项目的 `docker-compose.dokploy.yml` 和 Dockerfile。
- 推送后核对 CI 与 Dokploy 部署记录、提交 SHA、容器健康及任务日志。CI 成功不等于生产已完成，未验证状态必须说清楚。
- 常规回滚在 `main` 上 revert 再推送。禁止对 `main` 强推来“修”发布分叉，除非用户在当前任务中明确授权。

## 生产配置与例外操作

- 生产使用现有 VPS PostgreSQL `sigpik` 数据库；保留 `DATABASE_BACKEND=postgres`、严格 TLS 校验、私有网络、固定 IP、只读公开 CA 挂载、现有数据卷和服务开关。
- 密钥和数据库连接串由 Dokploy Environment 管理，不提交 Git、不写入 CI 或报告。Sitemap 等功能以现有生产开关为准，不用仓库默认值覆盖。
- 环境变量、密钥、数据库迁移和基础设施调整不属于普通代码自动发布。只有当前任务明确授权这些操作或手动部署例外时才执行，并保留相关资源、验证结果；不能仅因 CI 失败而自行扩大操作范围。
- 禁止使用 Fresh Volumes、`down -v` 或重建数据库来处理发布问题；禁止回退到旧 D1。底层资源名称包含 `test` 不代表可以删除。
