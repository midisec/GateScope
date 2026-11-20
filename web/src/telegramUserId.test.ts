import { describe, expect, it } from "vitest";
import { appendTelegramUserId, parseTelegramUserId } from "./telegramUserId";

describe("Telegram user ID helpers", () => {
  it("recognizes plain and labeled user IDs without guessing arbitrary numbers", () => {
    expect(parseTelegramUserId("123456789")).toBe("123456789");
    expect(parseTelegramUserId("Id: 123456789\nFirst: GateScope")).toBe("123456789");
    expect(parseTelegramUserId("您的用户 ID：987654321")).toBe("987654321");
    expect(parseTelegramUserId("joined at 2026-09-09")).toBeNull();
    expect(parseTelegramUserId("-1001234567890")).toBeNull();
  });

  it("adds a unique ID while preserving the existing allowlist", () => {
    expect(appendTelegramUserId("123, 456", "789")).toBe("123, 456, 789");
    expect(appendTelegramUserId("123, 456", "456")).toBe("123, 456");
  });
});
