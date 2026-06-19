import { NextRequest, NextResponse } from "next/server";
import { consumeRefreshToken, setAuthCookies, signAccessToken } from "@/lib/auth/server";

export async function POST(_req: NextRequest) {
  // The refresh cookie is HttpOnly, so we read it via the cookies() helper.
  // We import dynamically to avoid next/headers SSR warnings outside of route
  // handlers.
  const { cookies } = await import("next/headers");
  const store = await cookies();
  const refresh = store.get("drugos_refresh")?.value;
  if (!refresh) {
    return NextResponse.json({ error: "no_refresh_token" }, { status: 401 });
  }
  const result = await consumeRefreshToken(refresh);
  if (!result) {
    return NextResponse.json({ error: "invalid_refresh_token" }, { status: 401 });
  }
  await setAuthCookies(result.access, result.refresh);
  return NextResponse.json({ ok: true });
}
