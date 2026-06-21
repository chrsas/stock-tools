export type Row = Record<string, any>;

async function fetchJson(endpoint: string): Promise<Row> {
  const response = await fetch(endpoint);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

export async function loadPage(): Promise<Row> {
  const path = window.location.pathname;
  const endpoint = path.startsWith("/authors/")
    ? `/api${path}`
    : path.startsWith("/posts/")
      ? `/api${path}`
      : `/api/home${window.location.search}`;
  return fetchJson(endpoint);
}

export async function loadTimelinePage(
  view: string,
  cursor: string | null,
  limit: number,
): Promise<Row> {
  const params = new URLSearchParams({ view, limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return fetchJson(`/api/home?${params}`);
}

export async function loadCollectionStatus(): Promise<Row> {
  return fetchJson("/api/collect/status");
}

export async function loadEnrichmentStatus(): Promise<Row> {
  return fetchJson("/api/enrich/status");
}

export async function loadAutomationSettings(): Promise<Row> {
  return fetchJson("/api/automation/settings");
}

export async function loadOperationsStatus(): Promise<Row> {
  return fetchJson("/api/operations/status");
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
