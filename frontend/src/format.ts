import type { Row } from "./api";

export function authorName(item: Row): string {
  return item.author_display_name || item.author_name || item.author_platform_uid || "未知作者";
}

export function avatarUrl(value: unknown): string {
  const candidates = String(value || "").split(",").map((part) => part.trim()).filter(Boolean);
  if (!candidates.length) return "";
  const raw = candidates.find((part) => part.includes("50x50")) || candidates[0];
  if (/\s/.test(raw)) return "";
  if (/^https?:\/\//.test(raw)) return raw;
  if (raw.startsWith("//")) return `https:${raw}`;
  const key = raw.replace(/^\/+/, "");
  return /^(community|avatar|cube|users)\//.test(key) ? `https://xavatar.imedao.com/${key}` : "";
}

export function postTitle(item: Row): string {
  return item.platform_post_id ? `雪球 ${item.platform_post_id}` : `本地记录 ${item.post_id || item.id}`;
}

export function fmtTime(value: unknown): string {
  if (!value) return "无";
  const date = new Date(String(value));
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

export function percent(value: unknown): string {
  return value == null ? "无" : `${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(2)}%`;
}

export function originalUrl(item: Row): string {
  return /^https?:\/\//.test(String(item.url || "")) ? item.url : "";
}

export function xueqiuUrl(item: Row): string {
  return item.author_platform_uid ? `https://xueqiu.com/u/${encodeURIComponent(item.author_platform_uid)}` : "";
}
