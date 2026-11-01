# 分类安全修复 — 2026-10-28

撤销 slug 关键词计数豁免，保留小差距争议。上游兜底仅使用带原始 pageUrl 的引用，保留来源字段和证据类型，不再以本地化或候选描述伪造官网证据、独立产品资格、主卖工作流。当前及旧 upstream-fallback 画像必须人工审核。策略版本升级为 primary-evidence-v4-metadata-review-2026-10-28。

sigpik 审核计数移除异常转零与合法零值回退。该文件混有其他任务修改，不整文件提交。

验证：Python 分类53项、项目发布离线回归合计101项、前端队列20项全部通过。直接调用已安装 TypeScript 编译器 tsc --noEmit 通过；pnpm 包装器依赖检查失败，未安装依赖。git diff --check 通过。分类测试已加入 Report runtime CI。

本次没有修改生产数据库。原报告的912项任务、353个工具和生产发布结论未独立复核。新门禁不会自动撤销历史已接受分类；应按历史画像标记关联分类运行审计。小差距仍需人工确认，本修复不宣称完成新的语义裁决算法，自动覆盖率可能下降。

源码、测试和 CI 配置保留在正式位置；本报告为验收入口。临时任务 README 已清理，其他任务资料未改动。

## 发布结果

修复提交 33bb48a；CI fixture 修复提交 65f62a5。首轮 CI 发现测试依赖相邻 sigpik 仓库，现已将三份固定迁移文件纳入 tests/fixtures/taxonomy-migrations，并记录来源；未跳过测试。

[最终 CI](https://github.com/ningshenlo/tool-data-runner/actions/runs/40677853616) 全部通过，deploy/production 已由 CI 推进到 65f62a5b75dfb7ce171a490f8d90aab048608138。Dokploy 自动部署由现有配置承接，但本次未独立核实生产容器运行版本和健康，不宣称运行态已验收。

sigpik 后台计数修复已本地验证，尚未发布。原 execution-summary.md 已追加复核更正，保留历史正文。此发布结果补记为本地报告更新，避免仅因记录 CI 结果再次触发部署。
