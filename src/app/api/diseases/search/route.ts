import { NextRequest, NextResponse } from "next/server";
import { searchDiseasesByName } from "@/lib/services/mesh";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") || "";
  if (!q || q.trim().length < 2) {
    return badRequest("Query parameter 'q' (min 2 chars) is required");
  }
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "10", 10);
  try {
    const results = await searchDiseasesByName(q, limit);
    return NextResponse.json({ query: q, results });
  } catch (e: any) {
    return internalError(`MeSH search failed: ${e.message}`);
  }
}
