import { NextRequest, NextResponse } from "next/server";
import { requireAuth, notFound } from "@/lib/api-helpers";
import { revokeApiKey } from "@/lib/services/api-keys";

export async function POST(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return NextResponse.json({ error: "bad_request", message: "No active organization" }, { status: 400 });
  const { id } = await params;
  const ok = await revokeApiKey(auth.user.orgId, id);
  if (!ok) return notFound("API key not found or already revoked");
  return NextResponse.json({ ok: true });
}
