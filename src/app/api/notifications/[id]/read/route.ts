import { NextRequest, NextResponse } from "next/server";
import { requireAuth, notFound } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function POST(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const { id } = await params;
  const updated = await db.notification.updateMany({
    where: { id, userId: auth.user.userId, readAt: null },
    data: { readAt: new Date() },
  });
  if (updated.count === 0) return notFound("Notification not found or already read");
  return NextResponse.json({ ok: true });
}
