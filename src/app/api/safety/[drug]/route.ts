import { NextRequest, NextResponse } from "next/server";
import { getDrugSafetySummary } from "@/lib/services/openfda";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest, { params }: { params: Promise<{ drug: string }> }) {
  const { drug } = await params;
  if (!drug || drug.length < 2) {
    return badRequest("Drug name parameter (min 2 chars) is required");
  }
  try {
    const summary = await getDrugSafetySummary(decodeURIComponent(drug));
    if (!summary) {
      return NextResponse.json({ error: "not_found", message: "No data returned for this drug" }, { status: 404 });
    }
    return NextResponse.json(summary);
  } catch (e: any) {
    return internalError(`openFDA lookup failed: ${e.message}`);
  }
}
