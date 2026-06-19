import { NextRequest, NextResponse } from "next/server";
import { requireAuth, badRequest, internalError } from "@/lib/api-helpers";
import { changePlan, getOrganizationSubscription, PLANS } from "@/lib/services/billing";
import { db } from "@/lib/db";

export async function GET() {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("No active organization");
  const sub = await getOrganizationSubscription(auth.user.orgId);
  return NextResponse.json({ subscription: sub, plans: PLANS });
}

export async function POST(req: NextRequest) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("No active organization");
  let body: { planId: string };
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON");
  }
  if (!body.planId) return badRequest("planId is required");
  try {
    await changePlan(auth.user.orgId, body.planId);
    return NextResponse.json({ ok: true });
  } catch (e: any) {
    return internalError(e.message);
  }
}
