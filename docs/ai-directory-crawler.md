# AI 导航站定向爬虫 (AI Directory Batch Crawler)

## 概述

本项目脚本 [`scripts/crawl_ai_directories.py`](file:///e:/桌面/ai_system/tool-data-runner/scripts/crawl_ai_directories.py) 实现了方案 1：在 VPS 环境中定向爬取主流、高纯度的 AI 导航站（Futurepedia、AITopTools、Dang.ai），自动提取真实商业官网并写入 PostgreSQL 数据库中的 `discovery_candidates` 表（使用 `ai_directory_import` 渠道信任旁路）。

## 核心特性

1. **增量发现与高效去重**：
   - 优先通过 Sitemap 索引与时间戳（`<lastmod>` DESC）获取最新发布的工具。
   - 抓取前自动加载数据库已有 `tools`（8,000+）与 `discovery_candidates`（28,000+）中的 `normalized_domain` 及 `source_id`。
   - 已经入库的工具直接跳过，零网络浪费，极速完成增量探测。

2. **多源适配**：
   - **Futurepedia (`futurepedia`)**：解析专用 `sitemap_tools.xml`，提取内页真实官网及标签。
   - **AITopTools (`aitoptools`)**：解析 `sitemap.xml` 中的 `/tool/` 路径，清洗打标并提取落地官网。
   - **Dang.ai (`dang`)**：分页抓取 `/new` 实时更新，自动追踪 302 落地官网。

3. **URL 清洗与信任旁路兼容**：
   - 剔除 `utm_*`、`ref`、`fbclid` 等追踪参数。
   - 过滤平台域名（X/Twitter、GitHub、YouTube、Discord、Apple 等）。
   - 计算与 `discovery-runner` 完全一致的 FNV-1a 32 位 `evidence_fingerprint`。
   - 赋予 `source: 'ai_directory_import'` 与 `ai_eligible: 1`，写入后自动触发 Worker 验证与物化。

## 运行方式

### 1. 试运行（Dry Run，仅提取不写库）
```bash
python scripts/crawl_ai_directories.py --dry-run --limit 10
```

### 2. 正式批量抓取与入库
```bash
python scripts/crawl_ai_directories.py --limit 50 --concurrency 5
```

### 3. 指定单一渠道
```bash
python scripts/crawl_ai_directories.py --sources futurepedia --limit 100
```

### 4. 自动化测试
```bash
python -m pytest tests/test_ai_directory_crawler.py
```
