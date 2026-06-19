import { NextResponse } from "next/server";
import { checkKnowledgeGraphAvailability } from "@/lib/services/ml-stubs";

/**
 * Knowledge Graph query endpoint.
 *
 * The actual Neo4j graph is owned by the standalone Phase 2 service.
 * This endpoint proxies to it when KG_SERVICE_URL is set; otherwise it
 * returns an explicit "service not deployed" response with HTTP 503.
 *
 * We deliberately do NOT return any mock graph data. The user explicitly
 * forbade fabricated output for systems that could affect human safety.
 */
export async function GET() {
  const availability = checkKnowledgeGraphAvailability();
  if (!availability.available) {
    return NextResponse.json(
      {
        error: "service_not_deployed",
        service: availability.service,
        description: availability.description,
        reason: availability.reason,
        documentation: "See Phase 2 of the build plan (Neo4j Knowledge Graph Construction).",
      },
      { status: 503 }
    );
  }
  // Proxy to the real KG service — not implemented yet because the service
  // itself is the user's responsibility. We just forward the request.
  return NextResponse.json({ error: "not_implemented", message: "KG proxy is not yet implemented" }, { status: 501 });
}
