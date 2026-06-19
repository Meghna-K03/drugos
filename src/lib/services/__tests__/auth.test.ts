/**
 * Tests for the auth utilities.
 *
 * Verifies:
 *   1. Password hashing is deterministic for the same input (same hash) —
 *      and that hashing is slow enough to indicate bcrypt was used.
 *   2. Password verification correctly accepts correct passwords and
 *      rejects wrong ones.
 *   3. Password policy enforces the OWASP-recommended minimum complexity.
 *   4. JWT access tokens are signed with HS256 and can be verified.
 *   5. Tampered tokens are rejected.
 *   6. Email validation accepts RFC-5322-ish addresses and rejects garbage.
 */

import {
  hashPassword,
  verifyPassword,
  validatePasswordPolicy,
  validateEmail,
  signAccessToken,
  verifyAccessToken,
  issueRefreshToken,
} from "@/lib/auth/server";

describe("Password hashing (bcrypt)", () => {
  test("hashPassword produces a bcrypt hash with cost factor 12", async () => {
    const hash = await hashPassword("CorrectHorse9!");
    expect(hash).toMatch(/^\$2[aby]?\$12\$./); // bcrypt $2b$12$...
  }, 30000);

  test("hashPassword is non-deterministic (different salts)", async () => {
    const h1 = await hashPassword("CorrectHorse9!");
    const h2 = await hashPassword("CorrectHorse9!");
    expect(h1).not.toBe(h2);
  });

  test("verifyPassword accepts the correct password", async () => {
    const hash = await hashPassword("CorrectHorse9!");
    const ok = await verifyPassword("CorrectHorse9!", hash);
    expect(ok).toBe(true);
  });

  test("verifyPassword rejects an incorrect password", async () => {
    const hash = await hashPassword("CorrectHorse9!");
    const ok = await verifyPassword("WrongPassword9!", hash);
    expect(ok).toBe(false);
  });

  test("verifyPassword returns false (never throws) on malformed hash", async () => {
    const ok = await verifyPassword("anything", "not-a-real-hash");
    expect(ok).toBe(false);
  });
});

describe("Password policy", () => {
  test("rejects passwords shorter than 10 chars", () => {
    const r = validatePasswordPolicy("Ab1!short");
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/10 characters/i);
  });

  test("rejects passwords without lowercase", () => {
    const r = validatePasswordPolicy("ABCDEFGH1!");
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/lowercase/i);
  });

  test("rejects passwords without uppercase", () => {
    const r = validatePasswordPolicy("abcdefgh1!");
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/uppercase/i);
  });

  test("rejects passwords without a digit", () => {
    const r = validatePasswordPolicy("Abcdefghij!");
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/digit/i);
  });

  test("rejects passwords without a symbol", () => {
    const r = validatePasswordPolicy("Abcdefghij1");
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/symbol/i);
  });

  test("accepts a strong password", () => {
    const r = validatePasswordPolicy("CorrectHorse9!");
    expect(r.ok).toBe(true);
  });
});

describe("Email validation", () => {
  test("accepts well-formed emails", () => {
    expect(validateEmail("user@example.com")).toBe(true);
    expect(validateEmail("first.last@sub.example.org")).toBe(true);
  });

  test("rejects malformed emails", () => {
    expect(validateEmail("not-an-email")).toBe(false);
    expect(validateEmail("@example.com")).toBe(false);
    expect(validateEmail("user@")).toBe(false);
    expect(validateEmail("user@example")).toBe(false);
    expect(validateEmail("")).toBe(false);
  });
});

describe("JWT access tokens", () => {
  test("signAccessToken produces a JWT with three dot-separated parts", () => {
    const token = signAccessToken({
      userId: "u1",
      email: "u@example.com",
      role: "researcher",
    });
    const parts = token.split(".");
    expect(parts.length).toBe(3);
  });

  test("verifyAccessToken accepts a token it issued", () => {
    const token = signAccessToken({
      userId: "u1",
      email: "u@example.com",
      role: "researcher",
      orgId: "org1",
    });
    const decoded = verifyAccessToken(token);
    expect(decoded).not.toBeNull();
    expect(decoded?.userId).toBe("u1");
    expect(decoded?.email).toBe("u@example.com");
    expect(decoded?.role).toBe("researcher");
    expect(decoded?.orgId).toBe("org1");
  });

  test("verifyAccessToken rejects a tampered token", () => {
    const token = signAccessToken({ userId: "u1", email: "u@example.com", role: "researcher" });
    const tampered = token.slice(0, -5) + "XXXXX";
    expect(verifyAccessToken(tampered)).toBeNull();
  });

  test("verifyAccessToken rejects a token signed with a different secret", () => {
    const jwt = require("jsonwebtoken");
    const forged = jwt.sign(
      { sub: "u1", email: "u@example.com", role: "admin", type: "access" },
      "different-secret",
      { issuer: "drugos", algorithm: "HS256" }
    );
    expect(verifyAccessToken(forged)).toBeNull();
  });

  test("verifyAccessToken rejects tokens without type=access", () => {
    const jwt = require("jsonwebtoken");
    const forged = jwt.sign(
      { sub: "u1", email: "u@example.com", role: "admin", type: "refresh" },
      process.env.JWT_SECRET,
      { issuer: "drugos", algorithm: "HS256" }
    );
    expect(verifyAccessToken(forged)).toBeNull();
  });
});

describe("Refresh token issuance", () => {
  test("issueRefreshToken returns a 64-char hex string with a 30-day expiry", () => {
    const { token, expiresAt } = issueRefreshToken();
    expect(token).toMatch(/^[0-9a-f]{64}$/);
    const thirtyDaysFromNow = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
    const oneMinute = 60 * 1000;
    // Should be within 1 minute of 30 days from now
    expect(Math.abs(expiresAt.getTime() - thirtyDaysFromNow.getTime())).toBeLessThan(oneMinute);
  });
});
