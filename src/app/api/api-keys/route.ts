import { NextRequest, NextResponse } from "next/server";
import { requireAuth, badRequest } from "@/lib/api-helpers";
import { issueApiKey, listApiKeys } from "@/lib/services/api-keys";

export async function GET() {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("No active organization");
  const keys = await listApiKeys(auth.user.orgId);
  return NextResponse.json({ items: keys });
}

export async function POST(req: NextRequest) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("No active organization");
  let body: { name: string };
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON");
  }
  if (!body.name || body.name.trim().length < 1) {
    return badRequest("Key name is required");
  }
  const created = await issueApiKey(auth.user.orgId, auth.user.userId, body.name.trim());
  return NextResponse.json(created, { status: 201 });
}
