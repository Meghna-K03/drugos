import { NextRequest, NextResponse } from "next/server";
import { requireAuth, notFound } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const { id } = await params;
  const project = await db.project.findUnique({ where: { id } });
  if (!project) return notFound("Project not found");
  if (project.organizationId !== auth.user.orgId) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }
  let body: { authorName: string; body: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  if (!body.body || body.body.trim().length < 1) {
    return NextResponse.json({ error: "bad_request", message: "Comment body required" }, { status: 400 });
  }
  const comment = await db.comment.create({
    data: {
      projectId: id,
      userId: auth.user.userId,
      authorName: body.authorName || auth.user.email,
      body: body.body,
    },
  });
  await db.projectActivity.create({
    data: {
      projectId: id,
      type: "comment_added",
      actorName: body.authorName || auth.user.email,
      summary: body.body.slice(0, 120),
    },
  });
  return NextResponse.json(comment, { status: 201 });
}
