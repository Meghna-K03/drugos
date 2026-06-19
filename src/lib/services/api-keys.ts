/**
 * API key management for the developer platform.
 *
 * Keys are issued in the format `drugos_<32 hex chars>`. We store only the
 * SHA-256 hash of the key, never the raw key. The user sees the raw key
 * exactly once at creation time and is responsible for storing it.
 *
 * Rate limiting: each key inherits the rate limit of the organization's
 * subscription plan. We do not implement rate limiting inside this service
 * — it lives in the API gateway / middleware layer.
 */

import { db } from "@/lib/db";
import { createHash, randomBytes } from "crypto";

export interface CreatedApiKey {
  id: string;
  name: string;
  prefix: string;
  rawKey: string; // shown once
  createdAt: Date;
}

export async function issueApiKey(
  organizationId: string,
  userId: string,
  name: string
): Promise<CreatedApiKey> {
  const rawKey = `drugos_${randomBytes(16).toString("hex")}`;
  const hash = createHash("sha256").update(rawKey).digest("hex");
  const prefix = rawKey.slice(0, 12);
  const record = await db.apiKey.create({
    data: {
      organizationId,
      userId,
      name,
      hashedKey: hash,
      prefix,
    },
  });
  return {
    id: record.id,
    name: record.name,
    prefix,
    rawKey,
    createdAt: record.createdAt,
  };
}

export async function listApiKeys(organizationId: string) {
  return db.apiKey.findMany({
    where: { organizationId, revokedAt: null },
    orderBy: { createdAt: "desc" },
    select: {
      id: true,
      name: true,
      prefix: true,
      lastUsedAt: true,
      createdAt: true,
    },
  });
}

export async function revokeApiKey(organizationId: string, keyId: string): Promise<boolean> {
  const result = await db.apiKey.updateMany({
    where: { id: keyId, organizationId, revokedAt: null },
    data: { revokedAt: new Date() },
  });
  return result.count > 0;
}
