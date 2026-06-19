import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function GET(req: NextRequest) {
  const auth = await requireAdmin();
  if (auth.user === null) return auth.response;
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "100", 10);
  const action = req.nextUrl.searchParams.get("action");
  const logs = await db.auditLog.findMany({
    where: action ? { action } : undefined,
    orderBy: { createdAt: "desc" },
    take: limit,
  });
  const total = await db.auditLog.count({ where: action ? { action } : undefined });
  return NextResponse.json({ items: logs, total });
}
