# tool-data-runner 代理工作约定

适用于本项目及其子目录。先遵守工作区根 AGENTS.md，再阅读本项目 README.md。
部署维护细节见 [docs/production-deployment.md](docs/production-deployment.md)。

## 统一开发与发布分支

- 后续修改统一提交并推送到 `agent/fix-traffic-runner-observability`。若在临时分支或工作树开发，先将改动整合到该分支再发布；不要覆盖他人的未提交改动。
- `deploy/production` 是 CI 管理的发布分支，只由成功的 CI 推进到通过测试的准确提交。代理不得手动提交、合并、推送或强推该分支。
- 不把 `main`、历史开发分支或临时分支当作生产发布入口；不得擅自更改 Dokploy 跟踪的分支。

## 唯一常规部署流程

`推送 agent/fix-traffic-runner-observability → Report runtime CI 全部通过 → CI 推进 deploy/production → Dokploy 自动构建并部署`

- CI 定义在 `.github/workflows/report-runtime.yml`，包括 Compose 配置契约、两个镜像构建、镜像内导入检查、离线回归和报表生成检查。
- CI 失败时修复问题并重新推送源分支，不得跳过检查或绕过 CI 发布。PR 检查通过本身不触发生产发布；过期源提交也不会被推进。
- 正常代码和文档更新不需要手动操作 Dokploy。不得点击 Deploy、Rebuild、Stop、Fresh Volumes，也不得通过 API、Webhook、SSH 或生产 `docker compose up` 另行发布，以免重复部署或绕过测试门槛。
- 不再上传本地镜像、切回 Raw Compose 或在生产容器内直接修改源码。维护源是本项目的 `docker-compose.dokploy.yml` 和 Dockerfile。
- 推送后检查该提交的 CI 结果；需要确认上线时，可只读查看 Dokploy 部署记录、提交 SHA、容器健康及任务日志。CI 成功不等于生产部署已经成功，未验证的状态必须明确说明。
- 常规回滚也应在源分支提交 revert，再经过相同 CI 流程。遇到发布分支分叉时先排查原因，不得强推解决。

## 生产配置与例外操作

- 生产使用现有 VPS PostgreSQL `sigpik` 数据库；保留 `DATABASE_BACKEND=postgres`、严格 TLS 校验、私有网络、固定 IP、只读公开 CA 挂载、现有数据卷和服务开关。
- 密钥和数据库连接串由 Dokploy Environment 管理，不提交 Git、不写入 CI 或报告。Sitemap 等功能以现有生产开关为准，不用仓库默认值覆盖。
- 环境变量、密钥、数据库迁移和基础设施调整不属于普通代码自动发布。只有当前任务明确授权这些操作或手动部署例外时才执行，并保留相关资源、验证结果；不能仅因 CI 失败而自行扩大操作范围。
- 禁止使用 Fresh Volumes、`down -v` 或重建数据库来处理发布问题；禁止回退到旧 D1。底层资源名称包含 `test` 不代表可以删除。
