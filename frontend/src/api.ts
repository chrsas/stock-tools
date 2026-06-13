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

export async function mutate(path: string, csrfToken: string, values: Row = {}): Promise<Row> {
  const body = new URLSearchParams({ csrf_token: csrfToken, ...values });
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
