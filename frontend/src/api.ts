export type Row = Record<string, any>;

export async function loadPage(): Promise<Row> {
  const path = window.location.pathname;
  const endpoint = path.startsWith("/authors/")
    ? `/api${path}`
    : path.startsWith("/posts/")
      ? `/api${path}`
      : `/api/home${window.location.search}`;
  const response = await fetch(endpoint);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function freshTimelineItems(rendered: Row[], fetched: Row[]): Row[] {
  // Offset paging can re-list a post when the archive grows above the current
  // window mid-scroll: a newer post shifts every rank down one, so the next
  // window's first row is one already shown. Drop ids we have rendered (and any
  // repeat inside this batch) so nothing appears twice.
  const seen = new Set(rendered.map((item) => item.post_id));
  return fetched.filter((item) => {
    if (seen.has(item.post_id)) return false;
    seen.add(item.post_id);
    return true;
  });
}

export async function loadTimelinePage(
  view: string,
  offset: number,
  limit: number,
): Promise<Row> {
  const params = new URLSearchParams({ view, offset: String(offset), limit: String(limit) });
  const response = await fetch(`/api/home?${params}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function loadCollectionStatus(): Promise<Row> {
  const response = await fetch("/api/collect/status");
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function loadEnrichmentStatus(): Promise<Row> {
  const response = await fetch("/api/enrich/status");
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function loadAutomationSettings(): Promise<Row> {
  const response = await fetch("/api/automation/settings");
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function loadOperationsStatus(): Promise<Row> {
  const response = await fetch("/api/operations/status");
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export function friendlyRequestError(reason: unknown): string {
  if (reason instanceof TypeError && /failed to fetch/i.test(reason.message)) {
    return "无法连接本地网页服务。当前任务可能仍在后台运行，请确认服务未退出，然后刷新页面查看状态。";
  }
  if (reason instanceof Error) return reason.message;
  return String(reason);
}

export async function mutate(
  path: string,
  csrfToken: string,
  values: Row | URLSearchParams = {},
): Promise<Row> {
  // URLSearchParams preserves repeated keys (e.g. multiple recall `group` fields)
  // that a plain object would collapse; a Row is the common single-value case.
  const body = values instanceof URLSearchParams ? values : new URLSearchParams(values);
  body.set("csrf_token", csrfToken);
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });
  if (!response.ok) throw new Error(await response.text());
  const text = await response.text();
  return text ? (JSON.parse(text) as Row) : {};
}
