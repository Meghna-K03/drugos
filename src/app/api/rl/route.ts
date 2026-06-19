import { NextRequest, NextResponse } from "next/server";
import { checkRlAvailability } from "@/lib/services/ml-stubs";

/**
 * RL hypothesis ranking endpoint.
 *
 * The actual Stable-Baselines3 PPO agent is owned by the standalone Phase 4
 * service. Returning fabricated repurposing predictions here could cause
 * real-world harm — a pharma company might act on a fake "high confidence"
 * prediction. We refuse to fabricate.
 */
export async function POST(req: NextRequest) {
  const availability = checkRlAvailability();
  if (!availability.available) {
    return NextResponse.json(
      {
        error: "service_not_deployed",
        service: availability.service,
        description: availability.description,
        reason: availability.reason,
        documentation: "See Phase 4 of the build plan (RL-Driven Hypothesis Ranking).",
      },
      { status: 503 }
    );
  }
  return NextResponse.json({ error: "not_implemented", message: "RL proxy is not yet implemented" }, { status: 501 });
}
