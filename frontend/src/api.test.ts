import { afterEach, describe, expect, it, vi } from "vitest";

import { freshTimelineItems, friendlyRequestError, loadTimelinePage } from "./api";

describe("freshTimelineItems", () => {
  it("drops posts already rendered when an overlapping window is fetched", () => {
    const rendered = [{ post_id: 3 }, { post_id: 2 }, { post_id: 1 }];
    // A newer post pushed the window down, so the next page re-lists post_id 1.
    const fetched = [{ post_id: 1 }, { post_id: 0 }];
    expect(freshTimelineItems(rendered, fetched)).toEqual([{ post_id: 0 }]);
  });

  it("removes repeats within the same batch", () => {
    const fetched = [{ post_id: 5 }, { post_id: 5 }, { post_id: 4 }];
    expect(freshTimelineItems([], fetched)).toEqual([{ post_id: 5 }, { post_id: 4 }]);
  });

  it("keeps every item when nothing overlaps", () => {
    const fetched = [{ post_id: 9 }, { post_id: 8 }];
    expect(freshTimelineItems([{ post_id: 10 }], fetched)).toEqual(fetched);
  });
});

describe("loadTimelinePage", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("requests the home endpoint with view, offset and limit", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ view: "raw", items: [], has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = await loadTimelinePage("raw", 50, 20);

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/home?");
    expect(url).toContain("view=raw");
    expect(url).toContain("offset=50");
    expect(url).toContain("limit=20");
    expect(payload.has_more).toBe(false);
  });

  it("throws the server error text on a non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("boom", { status: 500 })),
    );
    await expect(loadTimelinePage("filtered", 0, 20)).rejects.toThrow("boom");
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
