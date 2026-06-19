import { NextRequest, NextResponse } from "next/server";
import { requireAuth, notFound } from "@/lib/api-helpers";
import { getProject, createHypothesis } from "@/lib/services/projects";
import { db } from "@/lib/db";

export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const { id } = await params;
  const project = await getProject(id);
  if (!project) return notFound("Project not found");
  if (project.organizationId !== auth.user.orgId) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }
  return NextResponse.json(project);
}

export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const { id } = await params;
  const project = await db.project.findUnique({ where: { id } });
  if (!project) return notFound("Project not found");
  if (project.organizationId !== auth.user.orgId) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }
  let body: { title: string; drugName: string; diseaseName: string; notes?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "bad_request", message: "Invalid JSON" }, { status: 400 });
  }
  if (!body.title || !body.drugName || !body.diseaseName) {
    return NextResponse.json({ error: "bad_request", message: "title, drugName, diseaseName required" }, { status: 400 });
  }
  const hyp = await createHypothesis({
    projectId: id,
    title: body.title,
    drugName: body.drugName,
    diseaseName: body.diseaseName,
    createdById: auth.user.userId,
    notes: body.notes,
  });
  return NextResponse.json(hyp, { status: 201 });
}
