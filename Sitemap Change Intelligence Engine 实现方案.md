# Sitemap Change Intelligence Engine 实现方案

## 1. 系统目标

系统不是单纯回答：

> sitemap.xml 有没有变化？

而是回答：

> example.com 最近 24 小时新增了 86 个 URL，其中 73 个属于 `/integrations/{slug}`，疑似正在批量建设 Integration Landing Pages；另外出现 `/compare/xxx` 页面，可能开始进行竞品 SEO。

因此整个系统分成四层：

```text
Detection
发现 sitemap 是否变化

        ↓

Diff
准确找出新增 / 移除 / metadata 变化

        ↓

Understanding
URL 聚类 + 页面抽样 + 分类

        ↓

Intelligence
解释目标网站可能正在做什么
```

最终核心数据不是 `sitemap snapshot`，而是：

```text
ChangeEvent
```

---

# 2. 推荐技术架构

如果采用 Cloudflare，可以使用：

```text
Cron Trigger
      ↓
Scheduler Worker
      ↓
Sitemap Fetch Queue
      ↓
Fetcher / Parser Worker
      ↓
 ┌─────────────┐
 │             │
D1             R2
metadata       sitemap state
events         snapshots
               diff files
      ↓
Enrichment Queue
      ↓
Page Fetcher
      ↓
Intent Analysis
      ↓
D1 ChangeEvent
```

Cron Trigger 很适合周期任务，Queues 用来把抓取、页面分析等工作异步解耦；D1 提供 SQL 状态存储，R2 更适合 sitemap、diff、snapshot 这种对象数据。

这里最重要的架构原则是：

```text
D1 = 状态 + 索引 + Event

R2 = 大文件 + URL State + Snapshot + Diff
```

不要把每次抓到的几万条 URL 全部写入 D1。

---

# 3. Sitemap Resource 模型

不要把一个网站简单理解成一个 `sitemap.xml`。

真实结构可能是：

```text
example.com
│
├── robots.txt
│
└── sitemap.xml                  sitemap_index
      │
      ├── pages-sitemap.xml      urlset
      ├── posts-sitemap.xml      urlset
      ├── product-sitemap.xml    urlset
      └── integrations.xml       urlset
```

Sitemap 协议本身支持 sitemap index，一个标准 sitemap 文件最多包含 50,000 个 URL、未压缩大小最多 50MB，因此应该把每个 sitemap 文件作为独立 Resource 管理，而不是每次把整个网站重新处理一遍。

数据关系：

```text
Site
 ↓
SitemapResource
 ↓
SitemapVersion
 ↓
ChangeEvent
```

---

# 4. D1 数据模型

核心表建议设计成：

```sql
CREATE TABLE sites (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    homepage_url TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'active',

    check_interval_sec INTEGER NOT NULL DEFAULT 3600,
    next_check_at INTEGER NOT NULL,

    last_checked_at INTEGER,
    last_changed_at INTEGER,

    error_streak INTEGER NOT NULL DEFAULT 0,

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_sites_due
ON sites(status, next_check_at);
```

每一个 sitemap 单独保存：

```sql
CREATE TABLE sitemap_resources (
    id TEXT PRIMARY KEY,

    site_id TEXT NOT NULL,
    parent_id TEXT,

    url TEXT NOT NULL,

    type TEXT NOT NULL DEFAULT 'unknown',
    -- unknown
    -- sitemap_index
    -- urlset
    -- text

    etag TEXT,
    http_last_modified TEXT,

    content_hash TEXT,
    urlset_hash TEXT,
    metadata_hash TEXT,

    url_count INTEGER,

    current_state_r2_key TEXT,

    last_checked_at INTEGER,
    last_changed_at INTEGER,

    force_verify_at INTEGER,

    missing_streak INTEGER NOT NULL DEFAULT 0,
    error_streak INTEGER NOT NULL DEFAULT 0,

    created_at INTEGER NOT NULL,

    UNIQUE(site_id, url)
);
```

每次抓取可以有轻量日志：

```sql
CREATE TABLE sitemap_runs (
    id TEXT PRIMARY KEY,

    site_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,

    started_at INTEGER NOT NULL,
    finished_at INTEGER,

    http_status INTEGER,

    bytes_downloaded INTEGER,
    url_count INTEGER,

    result TEXT,
    -- not_modified
    -- semantic_unchanged
    -- changed
    -- failed

    error_code TEXT
);
```

真正给产品使用的是：

```sql
CREATE TABLE change_events (
    id TEXT PRIMARY KEY,

    site_id TEXT NOT NULL,
    resource_id TEXT,

    detected_at INTEGER NOT NULL,

    event_type TEXT NOT NULL,

    added_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    modified_count INTEGER NOT NULL DEFAULT 0,

    cluster_pattern TEXT,

    category TEXT,
    intent TEXT,

    confidence REAL,
    priority INTEGER,

    summary TEXT,

    diff_r2_key TEXT,
    evidence_r2_key TEXT,

    created_at INTEGER NOT NULL
);

CREATE INDEX idx_events_site_time
ON change_events(site_id, detected_at DESC);
```

---

# 5. Sitemap Discovery

新增网站时首先执行一次 Discovery。

流程：

```text
example.com
    ↓
GET /robots.txt
    ↓
寻找 Sitemap: xxx
    ↓
尝试用户指定 sitemap
    ↓
必要时尝试常见路径
    ↓
解析 sitemap
```

第一次发现：

```text
/sitemap.xml
```

如果里面是：

```xml
<sitemapindex>
```

继续递归。

如果是：

```xml
<urlset>
```

则进入 URL Parser。

需要防止：

```text
index A
 ↓
index B
 ↓
index A
```

因此维护：

```text
visited_sitemap_urls
```

并设置合理递归深度，例如：

```text
max_depth = 5
```

robots.txt 本身也应该低频重新检查，因为目标网站以后可能增加新的 sitemap。

---

# 6. Sitemap Fetch：第一层性能优化

不要：

```text
GET sitemap
↓
下载完整文件
↓
hash
```

优先使用 HTTP Conditional Request。

第一次：

```http
GET /sitemap.xml
```

记录：

```text
ETag
Last-Modified
```

以后：

```http
If-None-Match: xxx
If-Modified-Since: xxx
```

如果服务器返回：

```text
304 Not Modified
```

这次任务直接结束。

HTTP RFC 定义了 `If-None-Match` / ETag 对 GET 条件请求的这种用途；条件不满足时可以返回 304，而无需重新传输完整 representation。

Fetcher 的核心逻辑应该类似：

```ts
async function checkSitemap(resource) {

    const response = await conditionalFetch(resource)

    if (response.status === 304) {
        await markNotModified(resource)
        return
    }

    if (response.status !== 200) {
        await handleFetchError(resource, response)
        return
    }

    const result = await parseAndFingerprint(response)

    if (result.urlsetHash === resource.urlsetHash) {
        await updateTransportMetadata(resource, result)
        return
    }

    await processSemanticChange(resource, result)
}
```

注意：

**不要先发 HEAD，再发 GET。**

直接 Conditional GET 即可，否则反而可能增加请求数量，而且部分网站 HEAD 行为和 GET 并不一致。

---

# 7. 三层 Hash

这是整个检测系统非常重要的一部分。

不要只有：

```text
file_hash
```

建议至少存在：

```text
content_hash
urlset_hash
metadata_hash
```

### content_hash

针对解压后的 sitemap 原始内容计算：

```text
SHA256(XML)
```

作用：

```text
文件内容是否发生过变化
```

它只是内部诊断指标。

---

### urlset_hash

只针对 URL 集合。

首先提取：

```xml
<loc>https://example.com/abc</loc>
```

然后执行：

```text
URL normalize
 ↓
SHA256(url)
 ↓
sort hashes
 ↓
SHA256(concat(all hashes))
```

得到：

```text
urlset_hash
```

因此：

```text
A
B
C
```

变成：

```text
C
A
B
```

最终 hash 不变。

这样就过滤掉：

```text
XML 顺序变化
格式变化
缩进变化
无意义 serialization 变化
```

---

### metadata_hash

再计算：

```text
URL
+
lastmod
```

组成的 fingerprint。

于是可以区分：

```text
content changed
urlset unchanged
metadata unchanged
```

说明：

```text
纯格式变化
```

而：

```text
content changed
urlset unchanged
metadata changed
```

说明：

```text
URL 没变化
lastmod 等 metadata 发生变化
```

只有：

```text
urlset_hash changed
```

才进入真正的 URL Diff。

---

# 8. URL Normalize 必须保守

这里不能过度 normalize。

建议：

```text
hostname lowercase
scheme lowercase
remove fragment
normalize default port
normalize obvious encoding differences
```

但不要默认：

```text
remove query string
remove trailing slash
lowercase path
```

因为：

```text
/product

/product/
```

以及：

```text
/search?q=abc
```

在某些网站上真的可能表示不同资源。

系统同时保存：

```text
raw_url
normalized_url
url_hash
```

---

# 9. 不要每次把 URL 全写进 D1

这是高性能版本和普通版本最大的区别之一。

假设：

```text
1000 websites
×
50000 URLs
```

如果每次检查都往数据库写 URL State，很快会产生大量无意义数据库读写。

推荐把每个 sitemap 当前状态保存成：

```text
R2:

state/
  {site_id}/
    {sitemap_id}/
      current.jsonl.gz
```

内容：

```json
{"h":"001...","u":"https://example.com/a","lm":"2026-08-01"}
{"h":"003...","u":"https://example.com/b","lm":"2026-08-02"}
{"h":"008...","u":"https://example.com/c","lm":"2026-08-02"}
```

按照：

```text
url_hash
```

排序。

D1 只记录：

```text
current_state_r2_key
urlset_hash
url_count
```

R2 本身就是对象存储，适合这种 snapshot/state 文件；D1 则保留可以快速查询的结构化状态和事件。

---

# 10. URL Diff

只有：

```text
new.urlset_hash !== old.urlset_hash
```

的时候，才读取 R2 中旧状态。

然后：

```text
old sorted URLs
new sorted URLs
```

执行 merge diff。

复杂度：

```text
O(N)
```

伪代码：

```ts
while (old || current) {

    if (!old) {
        added(current)
        current++
        continue
    }

    if (!current) {
        removed(old)
        old++
        continue
    }

    if (old.hash === current.hash) {

        if (old.metadata !== current.metadata) {
            modified(current)
        }

        old++
        current++
        continue
    }

    if (old.hash < current.hash) {
        removed(old)
        old++
    } else {
        added(current)
        current++
    }
}
```

最终：

```text
DiffResult

added[]
removed[]
metadataChanged[]
```

---

# 11. Diff 也存 R2

例如：

```text
diff/
  site_id/
    2026-08-11T08-00.json.gz
```

内容：

```json
{
  "added": [],
  "removed": [],
  "modified": []
}
```

D1：

```text
added_count = 183
removed_count = 3
diff_r2_key = xxx
```

这样一个网站突然批量增加 20,000 个 URL，也不需要往 `change_events` 塞几十 MB JSON。

---

# 12. 第一次扫描不能产生“新增 50,000 URL”事件

首次接入网站：

```text
baseline
```

只能：

```text
保存 sitemap state
记录 url_count
记录 fingerprint
```

不能报警：

> 网站新增 50,000 个 URL。

因为系统根本不知道这些 URL 是什么时候出现的。

从第二次扫描开始才产生 ChangeEvent。

---

# 13. URL Cluster：这是情报系统的核心

假设一次新增：

```text
/integrations/slack
/integrations/github
/integrations/notion
/integrations/linear
/integrations/asana
/integrations/jira
```

不要生成六条事件。

应该生成：

```text
/integrations/{slug}

added_count = 6
```

再比如：

```text
/compare/chatgpt-vs-claude
/compare/chatgpt-vs-gemini
/compare/claude-vs-gemini
```

得到：

```text
/compare/{slug}
```

V1 不需要非常复杂的机器学习。

可以按照：

```text
hostname
path_depth
first_segment
second_segment
parameter_shape
```

聚类。

后续升级成 Path Trie：

```text
/
└── integrations
      ├── slack
      ├── notion
      ├── github
      ├── jira
      └── linear
```

当某个节点：

```text
children cardinality 很高
+
child structure 一致
```

就转换成：

```text
/integrations/{slug}
```

同时识别：

```text
{id}
{uuid}
{date}
{locale}
{slug}
```

---

# 14. 第一层 Intent：规则引擎

不要一发现 URL 就调用 LLM。

先做 deterministic classification。

例如：

```text
/integrations/*
→ INTEGRATION

/compare/*
/vs/*
/alternatives/*
→ COMPETITOR_SEO

/templates/*
→ TEMPLATE / PROGRAMMATIC_SEO

/api/*
/developers/*
/webhooks/*
→ API_DEVELOPER

/enterprise/*
/security/*
/sso/*
→ ENTERPRISE

/pricing*
→ PRICING_MONETIZATION

/de/*
/fr/*
/ja/*
→ LOCALIZATION

/use-cases/*
/solutions/*
→ USE_CASE

/docs/*
→ DOCS

/blog/*
/news/*
→ CONTENT_MARKETING
```

推荐 taxonomy：

```text
PRODUCT_FEATURE
INTEGRATION
PROGRAMMATIC_SEO
COMPETITOR_SEO
CONTENT_MARKETING
LOCALIZATION
API_DEVELOPER
PRICING_MONETIZATION
ENTERPRISE
MARKETPLACE
USE_CASE
TEMPLATE
DOCS
HIRING
LEGAL
UNKNOWN
```

这样大部分变化根本不需要模型。

---

# 15. Programmatic SEO Detection

这是非常值得单独检测的 Signal。

例如过去一个小时：

```text
+ 328 URLs

pattern:
/templates/{slug}
```

而且页面：

```text
title structure 类似
H1 structure 类似
layout 类似
```

可以产生：

```text
PROGRAMMATIC_SEO_EXPANSION
```

情报：

> 目标网站疑似开始规模化建设 Template SEO 页面，本次新增 328 个 URL。

类似：

```text
/cities/{city}
/tools/{tool}
/use-cases/{industry}
/compare/{competitor}
```

都很有价值。

---

# 16. Page Enrichment：只抓必要页面

发生：

```text
83 new URLs
```

绝对不要：

```text
83 URLs
→
83 页面
→
83 LLM calls
```

应该：

```text
83 URLs
 ↓
cluster
 ↓
/integrations/{slug}
 ↓
抽样 3~5 页面
```

例如抓：

```text
/integrations/slack
/integrations/notion
/integrations/github
```

提取：

```text
HTTP status
title
meta description
H1
canonical
robots
JSON-LD
breadcrumb
正文前若干结构化文本
```

然后才进入 Intent Analysis。

对于：

```text
/pricing
/enterprise
/api
/new-product
```

这种单 URL 高价值路径，则直接抓取。

---

# 17. LLM 不应该读取完整 HTML

输入应该是已经压缩后的 Evidence。

例如：

```json
{
  "site": "example.com",

  "change": {
    "added_count": 83,
    "removed_count": 0,
    "pattern": "/integrations/{slug}"
  },

  "examples": [
    "/integrations/slack",
    "/integrations/notion",
    "/integrations/github"
  ],

  "pages": [
    {
      "url": "...",
      "title": "...",
      "h1": "...",
      "description": "..."
    }
  ]
}
```

要求模型严格输出：

```json
{
  "category": "INTEGRATION",

  "summary":
    "The company appears to be expanding its integration ecosystem.",

  "business_intent":
    "Increase platform connectivity and product stickiness.",

  "seo_intent":
    "Capture long-tail searches for third-party integrations.",

  "confidence": 0.91,

  "evidence": [
    "83 URLs were added under /integrations/",
    "Pages target individual SaaS products"
  ],

  "should_alert": true
}
```

特别要求模型：

```text
insufficient evidence
→ UNKNOWN
```

不能强迫它每次都给战略解释。

---

# 18. 将多个 URL Change 聚合成 Site Change

真实情况可能是：

```text
products-sitemap
+ 3

integration-sitemap
+ 80

blog-sitemap
+ 4

docs-sitemap
+ 10
```

不要给用户四条碎片通知。

可以在一次 Site Scan 完成后生成：

```text
SiteChangeBundle
```

例如：

> Example 在此次扫描中新增加 97 个 URL。主要变化集中在 Integration：新增 80 个第三方集成页面；同时新增 3 个产品页面和 10 个 Docs 页面。综合来看，该公司可能正在进行一次产品生态扩张。

这比单纯 Sitemap Event 更有价值。

---

# 19. removed URL 的语义必须正确

发现：

```text
old sitemap:
/abc

new sitemap:
不存在 /abc
```

事件只能叫：

```text
REMOVED_FROM_SITEMAP
```

不能直接说：

```text
PAGE_DELETED
```

因为 sitemap 本身只是 URL discovery 机制，URL 从 sitemap 消失不等于页面一定消失。

对于重要 removed URL，再执行：

```text
GET /abc
```

判断：

```text
200
301
302
404
410
noindex
canonical changed
```

才能升级成：

```text
PAGE_DELETED
PAGE_REDIRECTED
PAGE_NOINDEXED
SITEMAP_ONLY_REMOVAL
```

---

# 20. lastmod 必须建立 Reliability Score

很多网站会生成这种 sitemap：

```text
昨天：

所有 URL
lastmod = 2026-08-10

今天：

所有 URL
lastmod = 2026-08-11
```

实际页面根本没变。

如果每次都相信 `lastmod`，会产生大量噪音。

可以维护：

```text
lastmod_reliability_score
```

如果长期发现：

```text
lastmod 大量变化
但
URL / 页面实际不变化
```

逐渐降低这个 sitemap 的 metadata 权重。

最终：

```text
lastmod_reliability = LOW
```

之后只把它作为内部信号，不产生用户 Event。

---

# 21. Sitemap Index 需要特殊处理

如果：

```text
sitemap_index.xml
```

原来：

```text
blog.xml
pages.xml
products.xml
```

现在：

```text
blog.xml
pages.xml
products.xml
integrations.xml
```

这是一个非常重要的事件：

```text
SITEMAP_RESOURCE_ADDED
```

甚至还没打开：

```text
integrations.xml
```

你就已经知道：

> 网站新增了一整个 Integration 内容体系。

Sitemap index 本身就是为了组织多个 sitemap，且 sitemap 条目可以包含 lastmod，因此 index 层应该作为第一等资源处理，而不是只当一个跳转文件。

---

# 22. Adaptive Polling

不要：

```text
所有网站
每小时一次
```

Scheduler 每隔固定短周期运行，只查询：

```sql
SELECT *
FROM sites
WHERE status = 'active'
AND next_check_at <= ?
ORDER BY next_check_at
LIMIT 500;
```

然后：

```text
enqueue(site_id)
```

真正的检查频率存在数据库：

```text
check_interval_sec
next_check_at
```

V1 可以使用简单策略：

```text
最近24h多次变化
→ 15~30 min

最近7天发生变化
→ 1h

30天基本不变化
→ 6h

长期完全不变化
→ 12~24h
```

发现变化：

```text
interval ↓
```

长期不变：

```text
interval ↑
```

同时加入：

```text
random jitter
```

避免整点突然向大量网站同时发送请求。

---

# 23. Sitemap Index 的 lastmod 只能作为 Hint

例如 index 写着：

```xml
<sitemap>
    <loc>products.xml</loc>
    <lastmod>2026-08-11</lastmod>
</sitemap>
```

可以优先检查：

```text
products.xml
```

但不能永远因为：

```text
lastmod 没变
```

就永远不抓。

因此增加：

```text
force_verify_at
```

例如：

```text
正常：
根据 parent lastmod 判断

但到 force_verify_at：
强制 conditional GET
```

这样避免目标网站 lastmod 错误导致永久漏报。

---

# 24. Queue 必须实现幂等

Cloudflare Queues 的消费语义要求应用考虑消息可能再次投递，因此官方也建议在重复处理会产生副作用时使用唯一 ID / idempotency key。

例如：

```text
job_key =
resource_id
+
scheduled_time_bucket
```

D1：

```sql
CREATE UNIQUE INDEX idx_job_key
ON sitemap_runs(resource_id, started_at);
```

或者：

```text
job_id = SHA256(
    sitemap_resource_id +
    scheduled_bucket
)
```

Worker 收到重复任务：

```text
job already completed
→ ACK
```

绝对不能让重复 Queue Message 产生两份 ChangeEvent。

---

# 25. 错误策略

HTTP：

```text
304
→ success / unchanged

200
→ parse

301 / 302
→ follow + 记录 canonical fetch URL

404
→ missing_streak++

429
→ Retry-After / backoff

5xx
→ retry + exponential backoff

403
→ 标记 blocked，不进行激进绕过
```

只有连续：

```text
missing_streak >= N
```

才能把 SitemapResource 判定为 Missing。

否则一次 CDN/WAF 故障就会制造：

> 整个 sitemap 被删除

这种灾难级假情报。

Queues 本身支持重试、延迟以及 Dead Letter Queue，因此失败任务应该依赖队列重试机制，而不是 Worker 内部长时间循环。

---

# 26. Sitemap Parser 防御

Parser 必须处理：

```text
.xml
.xml.gz
sitemap_index
urlset
text sitemap
HTTP gzip
嵌套 sitemap
malformed XML
```

并设置：

```text
max_download_bytes
max_decompressed_bytes
max_url_count
max_index_depth
request_timeout
```

标准 sitemap 本身存在 50,000 URL 和 50MB 未压缩大小限制，因此可以把这个标准作为正常流量的安全边界，超出后进入特殊处理，而不是无限读取。

---

# 27. Priority / Noise Filter

不是所有 Sitemap Change 都值得告诉用户。

例如：

```text
/tag/*
/author/*
/page/23
/archive/2026/08/*
```

通常：

```text
LOW
```

而：

```text
/pricing
/enterprise
/api
/developers
/integrations/*
/compare/*
/templates/*
/solutions/*
```

可以提高优先级。

最终：

```text
priority_score
```

综合：

```text
path importance

change volume

change burst

cluster consistency

new sitemap type

page evidence

LLM confidence

historical novelty

noise penalty
```

例如：

```text
priority >= 80
→ Important

50~79
→ Normal

<50
→ Low / hidden
```

---

# 28. 最终用户看到的 ChangeEvent

系统不应该展示：

```text
sitemap hash changed
```

而应该展示：

```text
Example.com

2026-08-11

Integration Expansion

+83 URLs
-0 URLs

Pattern:
/integrations/{slug}

Examples:
Slack
Notion
GitHub
Linear

Likely intent:
The company appears to be expanding its third-party
integration ecosystem.

SEO implication:
These pages may also target
"[product] + integration" long-tail searches.

Confidence:
91%
```

点进去以后再查看：

```text
全部新增 URL
页面证据
首次发现时间
来源 sitemap
历史 sitemap
原始 Diff
```

---

# 29. 推荐项目目录

```text
src/
  monitor/

    scheduler/
      schedule-sites.ts
      adaptive-interval.ts

    discovery/
      robots.ts
      discover-sitemaps.ts

    sitemap/
      fetch.ts
      conditional-request.ts
      decompress.ts
      parser.ts
      normalize-url.ts
      fingerprint.ts

    diff/
      merge-diff.ts
      snapshot.ts

    intelligence/
      path-cluster.ts
      rules.ts
      priority.ts
      enrichment.ts
      intent-analysis.ts
      aggregate-site-change.ts

    storage/
      d1.ts
      r2.ts

    queues/
      sitemap-consumer.ts
      enrichment-consumer.ts
      intent-consumer.ts

    types/
      sitemap.ts
      event.ts
```

Queue 可以拆成：

```text
sitemap-fetch

page-enrichment

intent-analysis
```

这样以后：

```text
1000 个 sitemap fetch
```

不会因为：

```text
10 个慢 LLM request
```

阻塞整个抓取系统。

---

# 30. 推荐开发顺序

1. **Phase 1 — Sitemap Monitor MVP**：实现 Site、SitemapResource、robots discovery、sitemap index、urlset、Conditional GET、content/urlset hash、R2 baseline、基本 added/removed diff。此阶段不要接 LLM，先确保“新增 URL 检测绝对准确”。

2. **Phase 2 — Change Engine**：加入 ChangeEvent、Diff R2 文件、sitemap added/removed、metadata change、missing confirmation、后台 Timeline，让系统真正可以查看“网站最近发生过什么变化”。

3. **Phase 3 — URL Intelligence**：加入 path clustering、taxonomy rules、noise filter、priority score。做到 `/integrations/{slug}`、`/compare/{slug}`、`/templates/{slug}` 这种变化无需 AI 就可以解释。

4. **Phase 4 — Page Enrichment + LLM**：每个 Cluster 只抽样少量页面，提取 title/H1/meta/canonical，再让模型生成 business intent、SEO intent、confidence 和 evidence。

5. **Phase 5 — Performance**：加入 Adaptive Polling、ETag 命中率分析、lastmod reliability、queue retry、DLQ、domain throttling、Jitter 和强制周期校验。

6. **Phase 6 — Website Intelligence**：在 Sitemap Signal 稳定以后，再扩展 Pricing、Homepage、Docs、Changelog、Navigation、robots.txt、RSS 等信号。此时 Sitemap Monitor 就自然升级成完整的 `Website Change Intelligence Engine`。

---

# 31. MVP 最重要的验收指标

第一版不要追求 AI 有多聪明。

先盯住：

```text
Sitemap discovery success rate

304 hit rate

semantic unchanged rate

URL diff accuracy

false added rate

false removed rate

average bytes / check

average fetch duration

change detection latency

error rate

blocked sitemap count
```

尤其关注两个指标：

```text
HTTP 304 rate
```

它决定多少任务连文件都不用下载。

以及：

```text
content changed
BUT
urlset unchanged
```

的比例。

如果这个比例很高，就说明 semantic fingerprint 帮你过滤掉了大量无意义 Sitemap Change。

---

# 32. 最核心的数据处理路径

整个系统最终可以压缩成：

```text
Scheduler
    ↓
Due Site
    ↓
Sitemap Resource
    ↓
Conditional GET
    ↓
304?
 ├─ YES → END
 └─ NO
      ↓
Streaming Parse
      ↓
content_hash
urlset_hash
metadata_hash
      ↓
urlset changed?
 ├─ NO → update metadata → END
 └─ YES
      ↓
load previous R2 state
      ↓
merge diff
      ↓
save new R2 state
      ↓
added / removed
      ↓
URL clustering
      ↓
rules classification
      ↓
important?
 ├─ NO → save low-priority event
 └─ YES
      ↓
representative page fetch
      ↓
structured evidence
      ↓
LLM intent inference
      ↓
SiteChangeBundle
      ↓
Change Intelligence
```

这就是我认为比较合理的第一版完整架构。

其中最重要的三个设计决策是：

```text
① HTTP validator 是第一道过滤
② semantic URL fingerprint 才决定有没有真实变化
③ LLM 分析 Cluster，而不是分析每一个 URL
```

这样系统规模从：

```text
50 websites
```

增长到：

```text
500
5,000
甚至更多
```

时，请求数、数据库写入和 LLM 成本都不会简单地跟 URL 总数一起线性爆炸。

最终你保存的也不再是大量没有意义的 XML，而是一条条真正有研究价值的：

Site → Change → Evidence → Intent。