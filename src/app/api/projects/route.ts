import { NextRequest, NextResponse } from "next/server";
import { requireAuth, badRequest, notFound } from "@/lib/api-helpers";
import { createProject, listProjects } from "@/lib/services/projects";

export async function GET() {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return NextResponse.json({ items: [] });
  const projects = await listProjects(auth.user.orgId);
  return NextResponse.json({ items: projects });
}

export async function POST(req: NextRequest) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  if (!auth.user.orgId) return badRequest("User has no active organization");
  let body: { name: string; description?: string; visibility?: "private" | "org" | "public"; tags?: string[] };
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON body");
  }
  if (!body.name || body.name.trim().length < 1) {
    return badRequest("Project name is required");
  }
  const project = await createProject({
    name: body.name.trim(),
    description: body.description,
    ownerId: auth.user.userId,
    organizationId: auth.user.orgId,
    visibility: body.visibility,
    tags: body.tags,
  });
  return NextResponse.json(project, { status: 201 });
}
