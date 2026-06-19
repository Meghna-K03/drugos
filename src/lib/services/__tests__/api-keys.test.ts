/**
 * Tests for the API key management service.
 *
 * Verifies:
 *   1. Issued keys are in the format drugos_<hex>.
 *   2. The raw key is never stored — only the SHA-256 hash is.
 *   3. The prefix stored is the first 12 chars of the raw key (for UI display).
 *   4. Revocation marks the key with a revokedAt timestamp.
 *   5. Revoked keys cannot be revoked again.
 */

import { issueApiKey, listApiKeys, revokeApiKey } from "@/lib/services/api-keys";
import { db } from "@/lib/db";
import { createHash } from "crypto";

describe("API key management", () => {
  let testUserId: string;
  let testOrgId: string;

  beforeEach(async () => {
    const user = await db.user.create({
      data: {
        email: "apikey-test@example.com",
        passwordHash: "$2b$12$placeholderhashplaceholderhashplaceholderhashplaceholderhashplaceholderhashplacehold",
        name: "API Key Test",
        role: "owner",
      },
    });
    const org = await db.organization.create({ data: { name: "Org", slug: "apikey-test" } });
    await db.organizationMember.create({
      data: { userId: user.id, organizationId: org.id, role: "owner" },
    });
    testUserId = user.id;
    testOrgId = org.id;
  });

  test("issueApiKey returns a key in drugos_<hex> format", async () => {
    const created = await issueApiKey(testOrgId, testUserId, "Production key");
    expect(created.rawKey).toMatch(/^drugos_[0-9a-f]{32}$/);
    expect(created.prefix).toBe(created.rawKey.slice(0, 12));
    expect(created.name).toBe("Production key");
  });

  test("only the SHA-256 hash is stored — raw key is not in DB", async () => {
    const created = await issueApiKey(testOrgId, testUserId, "Test key");
    const stored = await db.apiKey.findFirst({ where: { prefix: created.prefix } });
    expect(stored).not.toBeNull();
    expect(stored?.hashedKey).not.toContain(created.rawKey);
    expect(stored?.hashedKey).toBe(createHash("sha256").update(created.rawKey).digest("hex"));
    expect(stored?.hashedKey).toMatch(/^[0-9a-f]{64}$/);
  });

  test("listApiKeys returns only non-revoked keys, without hashes", async () => {
    const k1 = await issueApiKey(testOrgId, testUserId, "Key 1");
    const k2 = await issueApiKey(testOrgId, testUserId, "Key 2");
    await revokeApiKey(testOrgId, k1.id);

    const keys = await listApiKeys(testOrgId);
    expect(keys.length).toBe(1);
    expect(keys[0].name).toBe("Key 2");
    expect((keys[0] as any).hashedKey).toBeUndefined();
  });

  test("revokeApiKey marks the key with revokedAt", async () => {
    const created = await issueApiKey(testOrgId, testUserId, "To revoke");
    const ok = await revokeApiKey(testOrgId, created.id);
    expect(ok).toBe(true);
    const stored = await db.apiKey.findUnique({ where: { id: created.id } });
    expect(stored?.revokedAt).not.toBeNull();
    expect(stored?.revokedAt).toBeInstanceOf(Date);
  });

  test("revokeApiKey on already-revoked key returns false", async () => {
    const created = await issueApiKey(testOrgId, testUserId, "To revoke twice");
    const first = await revokeApiKey(testOrgId, created.id);
    expect(first).toBe(true);
    const second = await revokeApiKey(testOrgId, created.id);
    expect(second).toBe(false);
  });

  test("revokeApiKey on a key from a DIFFERENT org returns false (no cross-org revoke)", async () => {
    const created = await issueApiKey(testOrgId, testUserId, "Test");
    const otherOrg = await db.organization.create({ data: { name: "Other Org", slug: "other" } });
    const ok = await revokeApiKey(otherOrg.id, created.id);
    expect(ok).toBe(false);
  });
});
