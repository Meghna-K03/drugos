import { NextRequest, NextResponse } from "next/server";
import { searchClinicalTrials } from "@/lib/services/clinical-trials";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest) {
  const condition = req.nextUrl.searchParams.get("condition") || "";
  const intervention = req.nextUrl.searchParams.get("intervention") || "";
  const status = (req.nextUrl.searchParams.get("status") || "ALL") as any;
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "20", 10);
  const offset = parseInt(req.nextUrl.searchParams.get("offset") || "0", 10);

  if (!condition && !intervention) {
    return badRequest("At least one of 'condition' or 'intervention' is required");
  }
  try {
    const result = await searchClinicalTrials({
      condition: condition || undefined,
      intervention: intervention || undefined,
      status,
      limit,
      offset,
    });
    return NextResponse.json(result);
  } catch (e: any) {
    return internalError(`ClinicalTrials.gov search failed: ${e.message}`);
  }
}
