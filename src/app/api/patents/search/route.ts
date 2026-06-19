import { NextRequest, NextResponse } from "next/server";
import { searchPatents } from "@/lib/services/patentsview";
import { badRequest, internalError } from "@/lib/api-helpers";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") || "";
  const limit = parseInt(req.nextUrl.searchParams.get("limit") || "20", 10);
  if (!q || q.trim().length < 2) {
    return badRequest("Query parameter 'q' (min 2 chars) is required");
  }
  try {
    const result = await searchPatents({ query: q, limit });
    return NextResponse.json(result);
  } catch (e: any) {
    return internalError(`Patent search failed: ${e.message}`);
  }
}
