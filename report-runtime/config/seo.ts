export type SeoLocale = 'en' | 'zh-CN';

export type SeoCopy = {
  title: string;
  description: string;
};

export type SeoCopySource = 'config' | 'database' | 'template';

export type ResolvedSeoCopy = SeoCopy & {
  titleSource: SeoCopySource;
  descriptionSource: SeoCopySource;
};

export type StaticSeoPage = {
  path: string;
  index: boolean;
  canonical: boolean;
  sitemap: boolean;
  changeFrequency?: 'daily' | 'weekly' | 'monthly';
  priority?: number;
  imagePath?: string;
  reviewedAt: string;
  copy: {
    en: SeoCopy;
    'zh-CN'?: SeoCopy;
  };
};

export const SEO_SITE = {
  brandName: 'Sigpik',
  defaultLocale: 'en' as const,
  defaultImagePath: '/og.jpg',
  reviewedAt: '2026-09-04',
};

/**
 * The approved metadata registry for static HTML pages.
 *
 * Page routes must resolve metadata by page ID instead of declaring title or
 * description strings locally. Values intentionally omit the `| sigpik`
 * suffix because the shared renderer appends it exactly once.
 */
export const STATIC_SEO_PAGES = {
  sharedPromotionReport: {
    path: '/share/reports/$token', index: false, canonical: false, sitemap: false, reviewedAt: '2026-09-12',
    copy: {
      en: { title: 'Shared promotion history report', description: 'Read a shared Sigpik promotion history report, with a timeline, competitive findings and source links.' },
      'zh-CN': { title: '分享的推广历史报告', description: '阅读 Sigpik 推广历史研究报告，查看推广时间线、竞争优势、切入机会与引用来源。' },
    },
  },
  reports: {
    path: '/reports', index: false, canonical: true, sitemap: false, reviewedAt: '2026-09-14',
    copy: {
      en: { title: 'AI Traffic Reports & Market Research', description: 'Monthly AI traffic research for founders, growth teams and media. Explore comparable samples, growth contributors and charts with citation-ready data.' },
      'zh-CN': { title: 'AI 流量报告与市场研究', description: '面向 AI 创始人、产品增长团队与媒体，按月研究可比样本的 Web 访问趋势、增长贡献和集中度，提供完整样本、统计口径与可引用图表。' },
    },
  },
  videoResearch: {
    path: '/reports/video-generation', index: false, canonical: true, sitemap: false, changeFrequency: 'monthly', reviewedAt: '2026-09-14',
    copy: {
      en: { title: 'AI Video Generator Traffic & Trends', description: 'Research AI video traffic, sample growth and concentration. Read monthly reports, inspect coverage and download charts with their methodology.' },
      'zh-CN': { title: 'AI 视频生成流量数据与趋势研究', description: '持续观察 AI 视频生成相关平台的 Web 访问变化、增长分布与头部集中度，查看固定月报、完整样本和可引用图表。' },
    },
  },
  imageResearch: {
    path: '/reports/image-generation', index: false, canonical: true, sitemap: false, changeFrequency: 'monthly', reviewedAt: '2026-09-14',
    copy: {
      en: { title: 'AI Image Generator Traffic & Trends', description: 'Research AI image traffic, growth contributors and concentration. Explore monthly sample statistics, coverage notes and downloadable charts.' },
      'zh-CN': { title: 'AI 图像生成流量数据与趋势研究', description: '持续观察 AI 图像生成相关平台的 Web 访问趋势、增长贡献和样本集中度，查看历期统计、覆盖范围与媒体图表。' },
    },
  },
  musicResearch: {
    path: '/reports/music-generation', index: false, canonical: true, sitemap: false, changeFrequency: 'monthly', reviewedAt: '2026-09-14',
    copy: {
      en: { title: 'AI Music Generator Traffic & Trends', description: 'Research AI music traffic, growth breadth and concentration. Explore monthly sample statistics, coverage notes, source tables and charts.' },
      'zh-CN': { title: 'AI 音乐生成流量数据与趋势研究', description: '持续观察 AI 音乐生成相关平台的 Web 访问趋势、增长分布和样本集中度。查看固定月报、样本名单与可引用图表。' },
    },
  },
  videoReport202608: {
    path: '/reports/video-generation/2026-08', index: false, canonical: true, sitemap: false,
    reviewedAt: '2026-09-14', imagePath: '/report-assets/previews/video-generation/2026-08/v1/en/social-card.png',
    copy: {
      en: { title: 'AI Video Traffic Report: August 2026', description: '145 comparable video-related domains: estimated Web visits fell 3.8%. Read sample findings, coverage limitations and download charts with source tables.' },
      'zh-CN': { title: '2026年8月 AI 视频生成流量报告', description: '145个视频生成相关平台可比样本的估计 Web 访问量环比下降3.8%。查看增长贡献、覆盖缺口、完整样本与可引用图表。' },
    },
  },
  imageReport202608: {
    path: '/reports/image-generation/2026-08', index: false, canonical: true, sitemap: false,
    reviewedAt: '2026-09-14', imagePath: '/report-assets/previews/image-generation/2026-08/v1/en/social-card.png',
    copy: {
      en: { title: 'AI Image Traffic Report: August 2026', description: '103 comparable image-related domains: estimated Web visits fell 6.1%. Explore growth contributors, coverage limitations and downloadable source tables.' },
      'zh-CN': { title: '2026年8月 AI 图像生成流量报告', description: '103个图像生成相关平台可比样本的估计 Web 访问量环比下降6.1%，64个样本下降。查看头部贡献、覆盖缺口与媒体图表。' },
    },
  },
  musicReport202608: {
    path: '/reports/music-generation/2026-08', index: false, canonical: true, sitemap: false,
    reviewedAt: '2026-09-14', imagePath: '/report-assets/previews/music-generation/2026-08/v1/en/social-card.png',
    copy: {
      en: { title: 'AI Music Traffic Report: August 2026', description: '51 comparable music-related domains, including Suno: estimated Web visits rose 7.2%, while 27 declined. Read coverage notes and download charts and sample data.' },
      'zh-CN': { title: '2026年8月 AI 音乐生成流量报告', description: '包含 Suno 的51个音乐生成相关平台可比样本，估计 Web 访问量环比上涨7.2%，27个样本下降。查看增长贡献、补充样本依据和可引用图表。' },
    },
  },
  home: {
    path: '',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 1,
    imagePath: '/og.jpg',
    reviewedAt: '2026-09-04',
    copy: {
      en: {
        title: 'AI SaaS Market Analysis & Competitive Intelligence',
        description: 'Research AI SaaS growth, traffic trends, and competitor activity. Market intelligence for founders, growth teams, researchers, and media.',
      },
      'zh-CN': {
        title: 'AI SaaS 市场分析与竞品动态情报',
        description: '研究 AI SaaS 增长、流量趋势与竞品动态，为创始人、增长团队、研究者与媒体提供市场分析和竞争情报。',
      },
    },
  },
  signals: {
    path: '/signals', index: true, canonical: true, sitemap: true,
    changeFrequency: 'daily', priority: 0.8, reviewedAt: '2026-09-13',
    copy: {
      en: { title: 'AI Product Signals & Competitor Changes', description: 'Explore confirmed AI product growth and website changes. Review sources, compare before-and-after values, and follow the products that matter.' },
      'zh-CN': { title: 'AI 产品异动与竞品信号', description: '浏览 AI 产品已确认的增长与网站变化，核对来源、前后数据和观察周期，持续关注与你相关的产品。' },
    },
  },
  ranking: {
    path: '/ranking',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Fastest Growing AI Tools by Website Traffic (2026)',
        description: 'Rank fast-growing AI tools by website traffic and month-over-month growth, using a 1K baseline and data refreshed monthly.',
      },
      'zh-CN': {
        title: '增长最快的 AI 工具网站流量榜（2026）',
        description: '按网站流量与月环比增长查看 AI 工具排名；榜单采用 1K 基准访问量，并按月更新数据。',
      },
    },
  },
  categories: {
    path: '/categories',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Browse categories',
        description: 'Start from a category, then narrow the catalog by capability, API support, and MCP support.',
      },
      'zh-CN': {
        title: '浏览分类',
        description: '从分类进入工具目录，再按能力、API 支持和 MCP 支持继续筛选。',
      },
    },
  },
  aiDirectories: {
    path: '/ai-directories',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'AI Directory List: Submission Links, Pricing & Traffic',
        description: 'Find AI directories for your product. Compare verified submission links, free and paid listing options, pricing, and monthly traffic trends.',
      },
      'zh-CN': {
        title: 'AI 工具导航站列表：提交方式、价格与访问量',
        description: '查找适合提交产品的 AI 工具导航站，对比已核验的提交入口、免费与付费收录方式、价格和月访问量趋势。',
      },
    },
  },
  developers: {
    path: '/developers',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-09-01',
    copy: {
      en: {
        title: 'Developer API & Remote MCP',
        description: 'Connect verified AI product data, search demand, and growth rankings to agents and server-side workflows.',
      },
      'zh-CN': {
        title: '开发者 API 与 Remote MCP',
        description: '把已核验的 AI 产品数据、搜索需求与增长排名接入 Agent 和服务端工作流。',
      },
    },
  },
  widgets: {
    path: '/widgets',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Free AI data widgets',
        description: 'Create embeddable AI tool growth rankings and traffic cards for websites, blogs, and newsletters.',
      },
      'zh-CN': {
        title: '免费 AI 数据组件',
        description: '生成可嵌入网站、博客和新闻稿的 AI 工具增长榜与流量卡片。',
      },
    },
  },
  pricing: {
    path: '/pricing',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Free, Starter, and Pro pricing',
        description: 'Compare Sigpik Free, Starter, and Pro subscriptions for AI product rankings, market intelligence, product watchlists, sitemap monitoring, signals, and exports.',
      },
      'zh-CN': {
        title: 'Free、Starter 与 Pro 套餐价格',
        description: '对比 sigpik Free、Starter 与 Pro 套餐在 AI 产品榜单、市场洞察、关注列表、网站地图监测、变化信号和数据导出方面的权益。',
      },
    },
  },
  submit: {
    path: '/submit',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Add an AI product',
        description: "Know an AI product we're missing? Send us the official website — Sigpik will verify, classify, and enrich it automatically.",
      },
      'zh-CN': {
        title: '添加一个 AI 产品',
        description: '发现了 Sigpik 还没覆盖的 AI 产品？把官网发给我们，验证、分类和数据补全由 Sigpik 自动完成。',
      },
    },
  },
  about: {
    path: '/about',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'monthly',
    priority: 0.4,
    reviewedAt: '2026-08-26',
    copy: {
      en: {
        title: 'About',
        description: 'Learn how sigpik tracks AI product rankings, monthly traffic, growth, launches, and market changes—and how to interpret the data responsibly.',
      },
      'zh-CN': {
        title: '关于 sigpik',
        description: '了解 sigpik 如何跟踪 AI 产品排名、月访问量、增长、发布与市场变化，以及应该如何审慎解读这些数据。',
      },
    },
  },
  changelog: {
    path: '/changelog',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.5,
    reviewedAt: '2026-09-02',
    copy: {
      en: {
        title: 'Product changelog',
        description: 'Follow the latest sigpik product updates, new features, experience improvements, and fixes in one chronological release log.',
      },
      'zh-CN': {
        title: '产品更新日志',
        description: '按时间查看 sigpik 的最新产品动态、新功能、体验改进和问题修复。',
      },
    },
  },
  contact: {
    path: '/contact',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'monthly',
    priority: 0.4,
    reviewedAt: '2026-08-26',
    copy: {
      en: {
        title: 'Contact',
        description: 'Use the right sigpik channel for AI product submissions, listing corrections, privacy questions, account access, and product-data requests.',
      },
      'zh-CN': {
        title: '联系 sigpik',
        description: '根据产品提交、收录信息更正、隐私问题、账户访问或产品数据需求，选择合适的 sigpik 处理入口。',
      },
    },
  },
  privacy: {
    path: '/privacy',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Privacy Policy',
        description: 'How sigpik handles account, submission, newsletter, analytics, and product-research data.',
      },
      'zh-CN': {
        title: '隐私政策',
        description: '了解 sigpik 如何处理账户、产品提交、邮件订阅、网站分析与产品研究数据。',
      },
    },
  },
  terms: {
    path: '/terms',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Terms of Use',
        description: 'Terms for using sigpik product discovery, market intelligence, accounts, and submissions.',
      },
      'zh-CN': {
        title: '使用条款',
        description: '使用 sigpik 产品发现、市场洞察、账户与产品提交服务时适用的条款。',
      },
    },
  },
  saasIdeas: {
    path: '/saas-ideas', index: false, canonical: true, sitemap: false, reviewedAt: '2026-09-11',
    copy: {
      en: { title: 'SaaS Ideas — in development', description: 'SEO opportunity research is still in development.' },
      'zh-CN': { title: 'SaaS Ideas：功能开发中', description: 'SEO 切入机会功能还在开发中，敬请期待。' },
    },
  },
  searchDemand: {
    path: '/search-demand',
    index: true,
    canonical: true,
    sitemap: true,
    changeFrequency: 'weekly',
    priority: 0.8,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'AI Search Demand — observed demand across AI products',
        description: 'Discover search terms observed across AI products, the products capturing that demand, and what is starting to move.',
      },
      'zh-CN': {
        title: 'AI 搜索需求：观察产品正在承接哪些需求',
        description: '查看 AI 产品覆盖的搜索词、正在承接这些需求的产品，以及近期开始出现变化的方向。',
      },
    },
  },
  signIn: {
    path: '/sign-in',
    index: false,
    canonical: true,
    sitemap: false,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Sign in',
        description: 'Sign in to sigpik to manage followed tools and submissions.',
      },
      'zh-CN': {
        title: '登录',
        description: '登录 sigpik，管理关注的产品与提交记录。',
      },
    },
  },
  signUp: {
    path: '/sign-up',
    index: false,
    canonical: true,
    sitemap: false,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Create account',
        description: 'Create a sigpik account to submit AI products and save tools.',
      },
      'zh-CN': {
        title: '创建账户',
        description: '创建 sigpik 账户，提交 AI 产品并收藏感兴趣的工具。',
      },
    },
  },
  dashboard: {
    path: '/dashboard',
    index: false,
    canonical: true,
    sitemap: false,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'sigpik',
        description: 'Manage saved products, followed markets, and account access.',
      },
      'zh-CN': {
        title: 'sigpik',
        description: '管理收藏的产品、关注的市场与账户访问。',
      },
    },
  },
  admin: {
    path: '/admin',
    index: false,
    canonical: true,
    sitemap: false,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'sigpik',
        description: 'Private sigpik catalog and market intelligence administration.',
      },
      'zh-CN': {
        title: 'sigpik',
        description: '管理 sigpik 产品目录与市场洞察数据。',
      },
    },
  },
  billingSuccess: {
    path: '/billing/success',
    index: false,
    canonical: true,
    sitemap: false,
    reviewedAt: '2026-08-20',
    copy: {
      en: {
        title: 'Subscription confirmed',
        description: 'Confirmation of the current sigpik subscription and account access.',
      },
      'zh-CN': {
        title: '订阅已确认',
        description: '确认当前 sigpik 订阅与账户权限。',
      },
    },
  },
} as const satisfies Record<string, StaticSeoPage>;

export type StaticSeoPageId = keyof typeof STATIC_SEO_PAGES;

export type ResolvedStaticSeoPage = Omit<StaticSeoPage, 'copy'> & {
  copy: SeoCopy;
};

export function getStaticSeoPage(pageId: StaticSeoPageId, locale: SeoLocale): ResolvedStaticSeoPage {
  const page = STATIC_SEO_PAGES[pageId] as StaticSeoPage;
  const localizedCopy = locale === 'zh-CN' ? page.copy['zh-CN'] : undefined;

  return {
    ...page,
    copy: localizedCopy ?? page.copy.en,
  };
}

export function getIndexableStaticSeoPages() {
  return (Object.entries(STATIC_SEO_PAGES) as Array<[StaticSeoPageId, StaticSeoPage]>)
    .filter(([, page]) => page.index && page.sitemap);
}

export function formatDocumentTitle(title: string) {
  const normalized = title.replace(/\s+/g, ' ').trim();
  const brandSuffix = `| ${SEO_SITE.brandName}`;

  if (normalized.toLocaleLowerCase() === SEO_SITE.brandName.toLocaleLowerCase()) return SEO_SITE.brandName;
  if (normalized.toLocaleLowerCase().endsWith(brandSuffix.toLocaleLowerCase())) return normalized;
  return `${normalized} ${brandSuffix}`;
}

function clampMetaDescription(value: string, maxLength = 160) {
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) return normalized;

  const candidate = normalized.slice(0, maxLength - 1);
  const lastSpace = candidate.lastIndexOf(' ');
  const cutoff = lastSpace >= Math.floor(maxLength * 0.65) ? lastSpace : maxLength - 1;

  return `${candidate.slice(0, cutoff).replace(/[\s,.;:!?。，“”、；：！？—-]+$/u, '')}…`;
}

type ToolSeoInput = {
  name: string;
  tagline?: string | null;
  short_description?: string | null;
  long_description?: string | null;
  seo_title?: string | null;
  seo_description?: string | null;
  has_api?: boolean;
  key_features?: Array<{ name: string; description?: string | null }>;
  feature_highlights?: string[];
};

function hasApiCapability(detail: ToolSeoInput) {
  if (detail.has_api) return true;

  return (detail.key_features ?? []).some((feature) =>
    /\bapi\b/i.test(`${feature.name} ${feature.description ?? ''}`),
  ) || (detail.feature_highlights ?? []).some((feature) => /\bapi\b/i.test(feature));
}

function buildToolSeoTitle(detail: ToolSeoInput, locale: SeoLocale) {
  const supportsApi = hasApiCapability(detail);
  const fullTitle = locale === 'zh-CN'
    ? `${detail.name}：功能、${supportsApi ? 'API 与' : ''}流量数据`
    : `${detail.name}: Features, ${supportsApi ? 'API & ' : ''}Traffic`;
  const compactTitle = locale === 'zh-CN'
    ? `${detail.name}：功能与流量`
    : `${detail.name}: Features & Traffic`;

  if (fullTitle.length <= 56) return fullTitle;
  if (compactTitle.length <= 56) return compactTitle;
  return detail.name;
}

function buildToolSeoDescription(detail: ToolSeoInput, locale: SeoLocale) {
  const summary = detail.short_description ?? detail.tagline ?? detail.long_description;
  const lead = summary
    ? (summary.toLocaleLowerCase().includes(detail.name.toLocaleLowerCase())
        ? summary
        : `${detail.name} — ${summary}`)
    : (locale === 'zh-CN'
        ? `${detail.name} 的 AI 产品资料页。`
        : `${detail.name} is listed in the sigpik AI product directory.`);
  const intentCopy = locale === 'zh-CN'
    ? `了解 ${detail.name} 的主要功能、API/MCP 支持、网站流量数据与替代产品。`
    : `Explore ${detail.name} features, API/MCP access, website traffic data, and alternatives.`;

  return clampMetaDescription(`${lead.replace(/[\s.。]+$/u, '')}. ${intentCopy}`);
}

export function resolveToolSeo(detail: ToolSeoInput, locale: SeoLocale): ResolvedSeoCopy {
  const legacyBrandPattern = /\bainav\b/i;
  const storedTitle = detail.seo_title?.trim();
  const storedDescription = detail.seo_description?.trim();
  const databaseTitle = storedTitle && !legacyBrandPattern.test(storedTitle) ? storedTitle : undefined;
  const databaseDescription = storedDescription && !legacyBrandPattern.test(storedDescription)
    ? storedDescription
    : undefined;

  return {
    title: databaseTitle || buildToolSeoTitle(detail, locale),
    description: databaseDescription || buildToolSeoDescription(detail, locale),
    titleSource: databaseTitle ? 'database' : 'template',
    descriptionSource: databaseDescription ? 'database' : 'template',
  };
}

type CategorySeoInput = {
  name: string;
  tool_count?: number | null;
  short_description?: string | null;
  seo_title?: string | null;
  seo_description?: string | null;
  seo_intro?: string | null;
};

export function resolveCategorySeo(category: CategorySeoInput, locale: SeoLocale): ResolvedSeoCopy {
  const databaseTitle = category.seo_title?.trim();
  const databaseDescription = category.seo_description?.trim();
  const fallbackTitle = locale === 'zh-CN'
    ? `${category.name} AI 工具流量与增长排名（2026）`
    : `${category.name} AI Tools Ranked by Traffic & Growth (2026)`;
  const count = Number.isFinite(category.tool_count) && Number(category.tool_count) >= 0
    ? Number(category.tool_count)
    : null;
  const fallbackDescription = category.seo_intro?.trim()
    || category.short_description?.trim()
    || (locale === 'zh-CN'
      ? `比较${count === null ? '' : ` ${count} 款`}${category.name} AI 工具的月访问量、近 1 个月增长与最新网站变化。Sigpik 每月更新。`
      : `Compare${count === null ? '' : ` ${count}`} ${category.name} AI tools by monthly traffic, 1-month growth, and latest site changes. Updated monthly on Sigpik.`);

  return {
    title: databaseTitle || fallbackTitle,
    description: clampMetaDescription(databaseDescription || fallbackDescription),
    titleSource: databaseTitle ? 'database' : 'template',
    descriptionSource: databaseDescription ? 'database' : 'template',
  };
}

export function resolveSearchSeo(query: string, locale: SeoLocale): ResolvedSeoCopy {
  const normalizedQuery = query.replace(/\s+/g, ' ').trim();
  const title = locale === 'zh-CN'
    ? `${normalizedQuery} — 搜索工具库`
    : `${normalizedQuery} — Search the catalog`;
  const description = locale === 'zh-CN'
    ? '按关键词、分类、API 支持或 MCP 支持查找 AI 工具。'
    : 'Find AI tools by keyword, category, API support, or MCP support.';

  return {
    title,
    description,
    titleSource: 'template',
    descriptionSource: 'template',
  };
}
