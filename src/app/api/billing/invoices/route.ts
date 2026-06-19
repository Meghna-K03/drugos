import { NextResponse } from "next/server";
import { requireAuth, badRequest } from "@/lib/api-helpers";
import { listOrganizationInvoices } from "@/lib/services/billing";

export async function GET() {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("No active organization");
  const invoices = await listOrganizationInvoices(auth.user.orgId);
  return NextResponse.json({ items: invoices });
}
