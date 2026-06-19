import { NextRequest, NextResponse } from "next/server";
import { clearAuthCookies, getAuthenticatedUser } from "@/lib/auth/server";
import { writeAuditLog } from "@/lib/api-helpers";

export async function POST(_req: NextRequest) {
  const user = await getAuthenticatedUser();
  if (user) {
    await writeAuditLog({ user, action: "logout", resource: `user:${user.userId}` });
  }
  await clearAuthCookies();
  return NextResponse.json({ ok: true });
}
