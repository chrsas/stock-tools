import type { Row } from "./api";

export function authorName(item: Row): string {
  return item.author_display_name || item.author_name || item.author_platform_uid || "未知作者";
}

export function avatarUrl(value: unknown): string {
  const candidates = String(value || "").split(",").map((part) => part.trim()).filter(Boolean);
  if (!candidates.length) return "";
  const raw = candidates.find((part) => part.includes("50x50")) || candidates[0];
  if (/\s/.test(raw)) return "";
  // Avatars live on xavatar.imedao.com; the xqimg.imedao.com post-image CDN 404s on
  // avatar paths, so rewrite that host wherever it appears (relative, absolute, or
  // protocol-relative). Other hosts pass through untouched.
  const fixHost = (url: string) => url.replace(/^(https?:\/\/)xqimg\.imedao\.com\//, "$1xavatar.imedao.com/");
  if (/^https?:\/\//.test(raw)) return fixHost(raw);
  if (raw.startsWith("//")) return fixHost(`https:${raw}`);
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

// 相对时间，精确到秒：未来渲染成“X 后”、过去渲染成“X 前”。第二个参数显式传入
// 当前毫秒数，既方便测试，也让调用方用一个每秒自增的 now 触发 Vue 重新计算、让秒数真正走动。
export function fmtRelative(value: unknown, nowMs: number = Date.now()): string {
  if (!value) return "无";
  const then = new Date(String(value)).valueOf();
  if (Number.isNaN(then)) return String(value);
  const diffMs = then - nowMs;
  const future = diffMs >= 0;
  let total = Math.floor(Math.abs(diffMs) / 1000);
  if (total < 1) return future ? "即将" : "刚刚";
  const days = Math.floor(total / 86400);
  total -= days * 86400;
  const hours = Math.floor(total / 3600);
  total -= hours * 3600;
  const minutes = Math.floor(total / 60);
  const seconds = total - minutes * 60;
  const parts: string[] = [];
  if (days) parts.push(`${days} 天`);
  if (hours) parts.push(`${hours} 小时`);
  if (minutes) parts.push(`${minutes} 分`);
  // 天级跨度再标到秒太吵；其余一律带上秒，保证“精确到秒”。
  if (!days) parts.push(`${seconds} 秒`);
  return `${parts.join(" ")}${future ? "后" : "前"}`;
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

// 证据卡片的观察版本列表：后端按 version_id 升序返回，当前版本通常排在最后。
// 这里把 current_version_id 对应的版本提到首位，其余保持原有时间顺序，
// 让证据卡片的“发言内容”一眼能看到当前生效的那条。
export function orderedVersions(versions: Row[] | undefined, currentVersionId: unknown): Row[] {
  const list = Array.isArray(versions) ? versions.slice() : [];
  if (currentVersionId == null) return list;
  const currentIndex = list.findIndex((version) => version.version_id === currentVersionId);
  if (currentIndex < 0) return list;
  const [current] = list.splice(currentIndex, 1);
  return [current, ...list];
}
