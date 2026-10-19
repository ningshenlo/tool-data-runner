import video from './video-generation-2026-08.v1.json' with { type: 'json' };
import image from './image-generation-2026-08.v1.json' with { type: 'json' };
import music from './music-generation-2026-08.v1.json' with { type: 'json' };
import type { ReportData } from '../../lib/reports/model.ts';
import { edition } from './presentation.ts';
export * from './presentation.ts';
export const REPORTS = [
  edition({ data: video as ReportData, seoPageId: 'videoReport202608', name: { en: 'Video generation', 'zh-CN': '视频生成' }, scopeName: { en: 'Video-related platforms', 'zh-CN': '视频生成相关平台' }, headline: { en: 'Most samples declined. Some still grew.', 'zh-CN': '多数样本回落，增长仍有亮点' }, featuredDomain: 'davinci.ai' }),
  edition({ data: image as ReportData, seoPageId: 'imageReport202608', name: { en: 'Image generation', 'zh-CN': '图像生成' }, scopeName: { en: 'Image-related platforms', 'zh-CN': '图像生成相关平台' }, headline: { en: 'Visits fell, led by the largest sample.', 'zh-CN': '样本访问量回落，头部平台贡献主要降幅' }, featuredDomain: 'magnific.com' }),
  edition({ data: music as ReportData, seoPageId: 'musicReport202608', name: { en: 'Music generation', 'zh-CN': '音乐生成' }, scopeName: { en: 'Music-related platforms', 'zh-CN': '音乐生成相关平台' }, headline: { en: 'Visit gains, losses and the net change', 'zh-CN': '样本访问量的增量、减量与净变化' }, featuredDomain: 'flowmusic.app' }),
];
export type ReportEdition = (typeof REPORTS)[number];
export const VIDEO_REPORT = REPORTS[0]!.data;
export const REPORT_SUMMARY = REPORTS[0]!.summary;
export const REPORT_MONTHS = [...new Set(REPORTS.map(r => r.data.month))].sort().reverse();
export function findReport(sector: string, month: string) { return REPORTS.find(r => r.data.sector === sector && r.data.month === month); }
