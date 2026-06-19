import { NextRequest, NextResponse } from "next/server";
import { searchPubMed } from "@/lib/services/pubmed";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest) {
  const query = req.nextUrl.searchParams.get("q") || "";
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "15", 10);
  const offset = parseInt(req.nextUrl.searchParams.get("offset") || "0", 10);
  const sort = (req.nextUrl.searchParams.get("sort") || "relevance") as any;
  const yearFrom = req.nextUrl.searchParams.get("yearFrom")
    ? parseInt(req.nextUrl.searchParams.get("yearFrom")!, 10)
    : undefined;
  const yearTo = req.nextUrl.searchParams.get("yearTo")
    ? parseInt(req.nextUrl.searchParams.get("yearTo")!, 10)
    : undefined;

  if (!query || query.trim().length < 2) {
    return badRequest("Query parameter 'q' (min 2 chars) is required");
  }
  try {
    const result = await searchPubMed({ query, limit, offset, sort, yearFrom, yearTo });
    return NextResponse.json(result);
  } catch (e: any) {
    return internalError(`PubMed search failed: ${e.message}`);
  }
}
