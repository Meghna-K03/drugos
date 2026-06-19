import { NextResponse } from "next/server";
import { getAuthenticatedUser } from "@/lib/auth/server";
import { db } from "@/lib/db";

export async function GET() {
  const authUser = await getAuthenticatedUser();
  if (!authUser) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const user = await db.user.findUnique({
    where: { id: authUser.userId },
    select: {
      id: true,
      email: true,
      name: true,
      role: true,
      status: true,
      emailVerified: true,
      academicVerified: true,
      mfaEnabled: true,
      lastLoginAt: true,
      createdAt: true,
    },
  });
  if (!user) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }
  const memberships = await db.organizationMember.findMany({
    where: { userId: user.id },
    include: { organization: true },
  });
  return NextResponse.json({
    user,
    organizations: memberships.map((m) => ({
      id: m.organization.id,
      name: m.organization.name,
      slug: m.organization.slug,
      plan: m.organization.plan,
      role: m.role,
    })),
    activeOrganizationId: authUser.orgId || memberships[0]?.organization.id || null,
  });
}
