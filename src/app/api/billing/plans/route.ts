import { NextResponse } from "next/server";
import { PLANS } from "@/lib/services/billing";

export async function GET() {
  return NextResponse.json({ plans: PLANS });
}
