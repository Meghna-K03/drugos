import { NextResponse } from "next/server";
import { checkDatasetAvailability } from "@/lib/services/ml-stubs";

/**
 * Dataset statistics endpoint.
 *
 * The actual Airflow ETL pipeline is owned by the standalone Phase 1 service.
 * Returning fake dataset statistics here would be a serious integrity violation.
 */
export async function GET() {
  const availability = checkDatasetAvailability();
  if (!availability.available) {
    return NextResponse.json(
      {
        error: "service_not_deployed",
        service: availability.service,
        description: availability.description,
        reason: availability.reason,
        documentation: "See Phase 1 of the build plan (Data Ingestion & Pipeline Setup).",
      },
      { status: 503 }
    );
  }
  return NextResponse.json({ error: "not_implemented", message: "Dataset proxy is not yet implemented" }, { status: 501 });
}
