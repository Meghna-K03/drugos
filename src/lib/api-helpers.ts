/**
 * Shared API helpers for Next.js route handlers.
 */

import { NextResponse } from "next/server";
import { getAuthenticatedUser, type AuthenticatedUser } from "@/lib/auth/server";
import { db } from "@/lib/db";

export async function requireAuth(): Promise<{ user: AuthenticatedUser; response: null } | { user: null; response: Response }> {
  const user = await getAuthenticatedUser();
  if (!user) {
    return {
      user: null,
      response: NextResponse.json({ error: "unauthorized", message: "Authentication required" }, { status: 401 }),
    };
  }
  return { user, response: null };
}

export async function requireAdmin(): Promise<{ user: AuthenticatedUser; response: null } | { user: null; response: Response }> {
  const auth = await requireAuth();
  if (auth.user === null) return auth;
  if (auth.user.role !== "admin" && auth.user.role !== "owner") {
    return {
      user: null,
      response: NextResponse.json({ error: "forbidden", message: "Admin access required" }, { status: 403 }),
    };
  }
  return auth;
}

export async function writeAuditLog(params: {
  user: AuthenticatedUser | null;
  action: string;
  resource?: string;
  metadata?: Record<string, unknown>;
}) {
  try {
    await db.auditLog.create({
      data: {
        userId: params.user?.userId || null,
        actorName: params.user?.email || "anonymous",
        action: params.action,
        resource: params.resource || null,
        metadata: JSON.stringify(params.metadata || {}),
      },
    });
  } catch (e) {
    // Audit log failures must NEVER break the main request — but we log them.
    console.error("Failed to write audit log:", e);
  }
}

export function badRequest(message: string) {
  return NextResponse.json({ error: "bad_request", message }, { status: 400 });
}

export function notFound(message: string) {
  return NextResponse.json({ error: "not_found", message }, { status: 404 });
}

export function internalError(message: string) {
  return NextResponse.json({ error: "internal_error", message }, { status: 500 });
}
