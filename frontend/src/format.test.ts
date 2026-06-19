import { describe, expect, it } from "vitest";

import { avatarUrl, orderedVersions, percent } from "./format";

describe("avatarUrl", () => {
  it("mints only known xavatar relative keys", () => {
    expect(avatarUrl("community/avatar.jpg!50x50.png")).toBe(
      "https://xavatar.imedao.com/community/avatar.jpg!50x50.png",
    );
    expect(avatarUrl("/users/avatar.png")).toBe("https://xavatar.imedao.com/users/avatar.png");
    expect(avatarUrl("other-cdn/avatar.jpg")).toBe("");
  });

  it("rejects dangerous schemes and malformed values", () => {
    expect(avatarUrl("javascript:alert(1)")).toBe("");
    expect(avatarUrl("data:image/svg+xml,bad")).toBe("");
    expect(avatarUrl("community/avatar bad.png")).toBe("");
  });

  it("rewrites the xqimg host on absolute and protocol-relative URLs", () => {
    expect(avatarUrl("https://xqimg.imedao.com/avatar/a.png")).toBe(
      "https://xavatar.imedao.com/avatar/a.png",
    );
    expect(avatarUrl("//xqimg.imedao.com/avatar/a.png")).toBe(
      "https://xavatar.imedao.com/avatar/a.png",
    );
  });

  it("passes through absolute URLs on other hosts unchanged", () => {
    expect(avatarUrl("https://xavatar.imedao.com/avatar/a.png")).toBe(
      "https://xavatar.imedao.com/avatar/a.png",
    );
    expect(avatarUrl("https://cdn.example.com/avatar/a.png")).toBe(
      "https://cdn.example.com/avatar/a.png",
    );
  });

  it("prefers the 50x50 candidate", () => {
    expect(avatarUrl("avatar/large.png, community/small.png!50x50.png")).toBe(
      "https://xavatar.imedao.com/community/small.png!50x50.png",
    );
  });
});

describe("percent", () => {
  it("formats descriptive returns", () => {
    expect(percent(0.0123)).toBe("+1.23%");
    expect(percent(null)).toBe("无");
  });
});

describe("orderedVersions", () => {
  const versions = [
    { version_id: 1, content_text: "v1" },
    { version_id: 2, content_text: "v2" },
    { version_id: 3, content_text: "v3" },
  ];

  it("pins the current version first and keeps the rest in original order", () => {
    expect(orderedVersions(versions, 2).map((v) => v.version_id)).toEqual([2, 1, 3]);
    // 当前版本本就在末位（多版本帖子的常见情形）也应被提到首位。
    expect(orderedVersions(versions, 3).map((v) => v.version_id)).toEqual([3, 1, 2]);
  });

  it("leaves the list untouched when current id is unknown or missing", () => {
    expect(orderedVersions(versions, 99).map((v) => v.version_id)).toEqual([1, 2, 3]);
    expect(orderedVersions(versions, null).map((v) => v.version_id)).toEqual([1, 2, 3]);
    expect(orderedVersions(undefined, 1)).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const input = versions.slice();
    orderedVersions(input, 3);
    expect(input.map((v) => v.version_id)).toEqual([1, 2, 3]);
  });
});
