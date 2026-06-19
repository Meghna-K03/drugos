/**
 * Auth utilities: password hashing (bcrypt), JWT issuing/verification,
 * and a small session helper for Next.js route handlers.
 *
 * Security choices:
 *  - bcrypt with cost factor 12 (OWASP-recommended minimum as of 2024).
 *  - Access tokens are short-lived (15 min) JWTs signed with HS256.
 *  - Refresh tokens are opaque random strings persisted in the DB so they
 *    can be revoked.
 *  - Passwords MUST meet a minimum complexity policy before hashing.
 */

import bcrypt from "bcryptjs";
import jwt from "jsonwebtoken";
import { randomBytes } from "crypto";
import { cookies } from "next/headers";
import { db } from "@/lib/db";

const JWT_SECRET = process.env.JWT_SECRET || "dev-only-insecure-secret-change-me";
const JWT_ISSUER = "drugos";
const ACCESS_TOKEN_TTL_SECONDS = 15 * 60; // 15 minutes
const REFRESH_TOKEN_TTL_DAYS = 30;

export interface AccessTokenPayload {
  sub: string; // user id
  email: string;
  role: string;
  orgId?: string;
  type: "access";
}

export interface AuthenticatedUser {
  userId: string;
  email: string;
  role: string;
  orgId?: string;
}

// ---------------------------------------------------------------------------
// Password policy
// ---------------------------------------------------------------------------

export interface PasswordPolicyResult {
  ok: boolean;
  reason?: string;
}

export function validatePasswordPolicy(password: string): PasswordPolicyResult {
  if (typeof password !== "string" || password.length < 10) {
    return { ok: false, reason: "Password must be at least 10 characters long." };
  }
  if (password.length > 1024) {
    return { ok: false, reason: "Password is too long." };
  }
  if (!/[a-z]/.test(password)) {
    return { ok: false, reason: "Password must contain at least one lowercase letter." };
  }
  if (!/[A-Z]/.test(password)) {
    return { ok: false, reason: "Password must contain at least one uppercase letter." };
  }
  if (!/[0-9]/.test(password)) {
    return { ok: false, reason: "Password must contain at least one digit." };
  }
  if (!/[^A-Za-z0-9]/.test(password)) {
    return { ok: false, reason: "Password must contain at least one symbol." };
  }
  return { ok: true };
}

export function validateEmail(email: string): boolean {
  // Pragmatic RFC-5322-ish check; we do not need to be perfect — we send a
  // verification email for real accounts.
  return typeof email === "string" && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && email.length <= 254;
}

// ---------------------------------------------------------------------------
// Password hashing
// ---------------------------------------------------------------------------

const BCRYPT_COST = 12;

export async function hashPassword(plain: string): Promise<string> {
  const salt = await bcrypt.genSalt(BCRYPT_COST);
  return bcrypt.hash(plain, salt);
}

export async function verifyPassword(plain: string, hash: string): Promise<boolean> {
  if (!hash || !plain) return false;
  try {
    return await bcrypt.compare(plain, hash);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// JWT
// ---------------------------------------------------------------------------

export function signAccessToken(payload: AuthenticatedUser): string {
  const jwtPayload: AccessTokenPayload = {
    sub: payload.userId,
    email: payload.email,
    role: payload.role,
    orgId: payload.orgId,
    type: "access",
  };
  return jwt.sign(jwtPayload, JWT_SECRET, {
    issuer: JWT_ISSUER,
    expiresIn: ACCESS_TOKEN_TTL_SECONDS,
    algorithm: "HS256",
  });
}

export function verifyAccessToken(token: string): AuthenticatedUser | null {
  try {
    const decoded = jwt.verify(token, JWT_SECRET, {
      issuer: JWT_ISSUER,
      algorithms: ["HS256"],
    }) as AccessTokenPayload;
    if (!decoded || decoded.type !== "access" || !decoded.sub) return null;
    return {
      userId: decoded.sub,
      email: decoded.email,
      role: decoded.role,
      orgId: decoded.orgId,
    };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Refresh tokens
// ---------------------------------------------------------------------------

export function issueRefreshToken(): { token: string; expiresAt: Date } {
  const token = randomBytes(32).toString("hex");
  const expiresAt = new Date(Date.now() + REFRESH_TOKEN_TTL_DAYS * 24 * 60 * 60 * 1000);
  return { token, expiresAt };
}

export async function rotateRefreshToken(userId: string): Promise<{ refresh: string; access: string }> {
  const { token, expiresAt } = issueRefreshToken();
  await db.refreshToken.create({ data: { userId, token, expiresAt } });
  const user = await db.user.findUnique({ where: { id: userId } });
  if (!user) throw new Error("User not found while rotating refresh token");
  const access = signAccessToken({ userId: user.id, email: user.email, role: user.role });
  return { refresh: token, access };
}

export async function consumeRefreshToken(token: string): Promise<{ refresh: string; access: string } | null> {
  const record = await db.refreshToken.findUnique({ where: { token } });
  if (!record) return null;
  if (record.revokedAt) return null;
  if (record.expiresAt.getTime() < Date.now()) return null;
  await db.refreshToken.update({ where: { id: record.id }, data: { revokedAt: new Date() } });
  return rotateRefreshToken(record.userId);
}

export async function revokeAllRefreshTokensForUser(userId: string): Promise<number> {
  const result = await db.refreshToken.updateMany({
    where: { userId, revokedAt: null },
    data: { revokedAt: new Date() },
  });
  return result.count;
}

// ---------------------------------------------------------------------------
// Cookie helpers (server-side route handlers)
// ---------------------------------------------------------------------------

export const ACCESS_COOKIE = "drugos_access";
export const REFRESH_COOKIE = "drugos_refresh";

export async function setAuthCookies(access: string, refresh: string): Promise<void> {
  const store = await cookies();
  const isProd = process.env.NODE_ENV === "production";
  store.set(ACCESS_COOKIE, access, {
    httpOnly: true,
    secure: isProd,
    sameSite: "lax",
    path: "/",
    maxAge: ACCESS_TOKEN_TTL_SECONDS,
  });
  store.set(REFRESH_COOKIE, refresh, {
    httpOnly: true,
    secure: isProd,
    sameSite: "lax",
    path: "/api/auth/refresh",
    maxAge: REFRESH_TOKEN_TTL_DAYS * 24 * 60 * 60,
  });
}

export async function clearAuthCookies(): Promise<void> {
  const store = await cookies();
  store.delete(ACCESS_COOKIE);
  store.delete(REFRESH_COOKIE);
}

export async function getAuthenticatedUser(): Promise<AuthenticatedUser | null> {
  const store = await cookies();
  const access = store.get(ACCESS_COOKIE)?.value;
  if (!access) return null;
  const user = verifyAccessToken(access);
  if (user) return user;
  // Try refresh
  const refresh = store.get(REFRESH_COOKIE)?.value;
  if (!refresh) return null;
  const refreshed = await consumeRefreshToken(refresh);
  if (!refreshed) return null;
  await setAuthCookies(refreshed.access, refreshed.refresh);
  return verifyAccessToken(refreshed.access);
}

// ---------------------------------------------------------------------------
// API key auth (for the developer platform / programmatic access)
// ---------------------------------------------------------------------------

export async function authenticateApiKey(rawKey: string): Promise<AuthenticatedUser | null> {
  if (!rawKey || typeof rawKey !== "string") return null;
  // Keys are issued with format "drugos_<32 hex chars>". We hash the full key
  // with sha256 before lookup so we never store the raw value.
  const { createHash } = await import("crypto");
  const hash = createHash("sha256").update(rawKey).digest("hex");
  const key = await db.apiKey.findFirst({
    where: { hashedKey: hash, revokedAt: null },
    include: { user: true },
  });
  if (!key) return null;
  await db.apiKey.update({ where: { id: key.id }, data: { lastUsedAt: new Date() } });
  return {
    userId: key.user.id,
    email: key.user.email,
    role: key.user.role,
    orgId: key.organizationId,
  };
}

// ---------------------------------------------------------------------------
// Authorization helpers
// ---------------------------------------------------------------------------

export function requireRole(user: AuthenticatedUser | null, ...roles: string[]): boolean {
  if (!user) return false;
  if (roles.length === 0) return true;
  return roles.includes(user.role);
}
