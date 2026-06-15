import { describe, expect, it } from "vitest";

import { friendlyRequestError } from "./api";

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
