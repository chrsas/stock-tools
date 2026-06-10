import { describe, expect, it } from "vitest";

import { avatarUrl } from "./format";

describe("avatarUrl", () => {
  it("mints only known xqimg relative keys", () => {
    expect(avatarUrl("community/avatar.jpg!50x50.png")).toBe(
      "https://xqimg.imedao.com/community/avatar.jpg!50x50.png",
    );
    expect(avatarUrl("/users/avatar.png")).toBe("https://xqimg.imedao.com/users/avatar.png");
    expect(avatarUrl("other-cdn/avatar.jpg")).toBe("");
  });

  it("rejects dangerous schemes and malformed values", () => {
    expect(avatarUrl("javascript:alert(1)")).toBe("");
    expect(avatarUrl("data:image/svg+xml,bad")).toBe("");
    expect(avatarUrl("community/avatar bad.png")).toBe("");
  });

  it("keeps valid absolute and protocol-relative URLs", () => {
    expect(avatarUrl("https://xqimg.imedao.com/avatar/a.png")).toBe(
      "https://xqimg.imedao.com/avatar/a.png",
    );
    expect(avatarUrl("//xqimg.imedao.com/avatar/a.png")).toBe(
      "https://xqimg.imedao.com/avatar/a.png",
    );
  });

  it("prefers the 50x50 candidate", () => {
    expect(avatarUrl("avatar/large.png, community/small.png!50x50.png")).toBe(
      "https://xqimg.imedao.com/community/small.png!50x50.png",
    );
  });
});
