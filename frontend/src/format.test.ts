import { describe, expect, it } from "vitest";

import { avatarUrl, percent } from "./format";

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
