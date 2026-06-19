import { NextRequest, NextResponse } from "next/server";
import { searchDrugsByName, getDrugProperties } from "@/lib/services/rxnorm";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") || "";
  const rxcui = req.nextUrl.searchParams.get("rxcui");
  try {
    if (rxcui) {
      const props = await getDrugProperties(rxcui);
      return NextResponse.json(props);
    }
    if (!q || q.trim().length < 2) {
      return badRequest("Query parameter 'q' (min 2 chars) or 'rxcui' is required");
    }
    const limit = parseInt(req.nextUrl.searchParams.get("limit") || "10", 10);
    const results = await searchDrugsByName(q, limit);
    return NextResponse.json({ query: q, results });
  } catch (e: any) {
    return internalError(`RxNorm search failed: ${e.message}`);
  }
}
