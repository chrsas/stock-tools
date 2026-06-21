import { afterEach, describe, expect, it, vi } from "vitest";

import { friendlyRequestError, loadTimelinePage } from "./api";

describe("loadTimelinePage", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("requests the home endpoint with view, cursor and limit", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ view: "raw", items: [], has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = await loadTimelinePage("raw", "cursor-1", 20);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/home?");
    expect(url).toContain("view=raw");
    expect(url).toContain("cursor=cursor-1");
    expect(url).toContain("limit=20");
    expect(url).not.toContain("offset=");
    expect(payload.has_more).toBe(false);
  });

  it("omits cursor on the first timeline request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ view: "filtered", items: [], has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await loadTimelinePage("filtered", null, 20);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("view=filtered");
    expect(url).toContain("limit=20");
    expect(url).not.toContain("cursor=");
  });

  it("throws the server error text on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("boom", { status: 500 })),
    );
    await expect(loadTimelinePage("filtered", null, 20)).rejects.toThrow("boom");
  });
});

describe("friendlyRequestError", () => {
  it("explains browser fetch connection failures", () => {
    const message = friendlyRequestError(new TypeError("Failed to fetch"));
    expect(message).toContain("无法连接本地网页服务");
    expect(message).toContain("当前任务可能仍在后台运行");
    expect(message).not.toContain("采集可能");
  });

  it("keeps useful server error messages", () => {
    expect(friendlyRequestError(new Error("采集正在进行中，请稍候。"))).toBe(
      "采集正在进行中，请稍候。",
    );
  });
});
