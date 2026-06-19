import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { hashPassword, validateEmail, validatePasswordPolicy, signAccessToken, rotateRefreshToken, setAuthCookies } from "@/lib/auth/server";
import { badRequest, internalError, writeAuditLog } from "@/lib/api-helpers";

interface RegisterBody {
  email: string;
  password: string;
  name?: string;
  organizationName?: string;
}

export async function POST(req: NextRequest) {
  let body: RegisterBody;
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON body");
  }
  const email = (body.email || "").trim().toLowerCase();
  const password = body.password || "";
  const name = (body.name || "").trim();
  const organizationName = (body.organizationName || "").trim();

  if (!validateEmail(email)) return badRequest("A valid email is required");
  const passwordCheck = validatePasswordPolicy(password);
  if (!passwordCheck.ok) return badRequest(passwordCheck.reason || "Password does not meet policy");
  if (!name) return badRequest("Name is required");

  const existing = await db.user.findUnique({ where: { email } });
  if (existing) {
    return NextResponse.json({ error: "email_taken", message: "An account with this email already exists" }, { status: 409 });
  }

  const passwordHash = await hashPassword(password);

  // Create the user, an organization, and link them as owner.
  // We use a transaction so partial-failures don't leave dangling rows.
  const user = await db.$transaction(async (tx) => {
    const u = await tx.user.create({
      data: {
        email,
        passwordHash,
        name,
        role: "owner",
        emailVerified: false,
      },
    });
    const slugBase = (organizationName || name).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 30);
    // Append a short random suffix to keep the slug unique — otherwise two
    // users registering with the same org name (e.g., "John's Workspace")
    // would collide on the unique slug constraint.
    const slug = `${slugBase}-${Math.random().toString(36).slice(2, 8)}`;
    const org = await tx.organization.create({
      data: {
        name: organizationName || `${name}'s Workspace`,
        slug,
        plan: "free",
        seats: 1,
      },
    });
    await tx.organizationMember.create({
      data: {
        userId: u.id,
        organizationId: org.id,
        role: "owner",
      },
    });
    await tx.subscription.create({
      data: {
        organizationId: org.id,
        plan: "free",
        status: "active",
        seats: 1,
        currentPeriodStart: new Date(),
        currentPeriodEnd: new Date(Date.now() + 365 * 24 * 60 * 60 * 1000),
      },
    });
    return { user: u, orgId: org.id };
  });

  const tokens = await rotateRefreshToken(user.user.id);
  const access = signAccessToken({
    userId: user.user.id,
    email: user.user.email,
    role: user.user.role,
    orgId: user.orgId,
  });
  await setAuthCookies(access, tokens.refresh);
  await writeAuditLog({
    user: { userId: user.user.id, email: user.user.email, role: user.user.role, orgId: user.orgId },
    action: "register",
    resource: `user:${user.user.id}`,
  });

  return NextResponse.json({
    user: {
      id: user.user.id,
      email: user.user.email,
      name: user.user.name,
      role: user.user.role,
    },
    organizationId: user.orgId,
  }, { status: 201 });
}
