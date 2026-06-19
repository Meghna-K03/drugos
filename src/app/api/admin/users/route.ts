import { NextRequest, NextResponse } from "next/server";
import { requireAdmin, badRequest, writeAuditLog } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function GET(req: NextRequest) {
  const auth = await requireAdmin();
  if (auth.user === null) return auth.response;
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "50", 10);
  const offset = parseInt(req.nextUrl.searchParams.get("offset") || "0", 10);
  const [users, total] = await Promise.all([
    db.user.findMany({
      select: {
        id: true,
        email: true,
        name: true,
        role: true,
        status: true,
        emailVerified: true,
        createdAt: true,
        lastLoginAt: true,
      },
      orderBy: { createdAt: "desc" },
      take: limit,
      skip: offset,
    }),
    db.user.count(),
  ]);
  return NextResponse.json({ items: users, total });
}

export async function PATCH(req: NextRequest) {
  const auth = await requireAdmin();
  if (auth.user === null) return auth.response;
  let body: { userId: string; role?: string; status?: string };
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON");
  }
  if (!body.userId) return badRequest("userId is required");
  const data: { role?: string; status?: string } = {};
  if (body.role) data.role = body.role;
  if (body.status) data.status = body.status;
  const updated = await db.user.update({
    where: { id: body.userId },
    data,
    select: { id: true, email: true, name: true, role: true, status: true },
  });
  await writeAuditLog({
    user: auth.user,
    action: "admin_user_update",
    resource: `user:${updated.id}`,
    metadata: data,
  });
  return NextResponse.json(updated);
}
