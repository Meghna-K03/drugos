import { NextResponse } from "next/server";
import { requireAuth } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function GET() {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const notifications = await db.notification.findMany({
    where: { userId: auth.user.userId },
    orderBy: { createdAt: "desc" },
    take: 50,
  });
  const unread = await db.notification.count({
    where: { userId: auth.user.userId, readAt: null },
  });
  return NextResponse.json({ items: notifications, unread });
}
