/**
 * Projects & collaboration service.
 * Backed by Prisma/SQLite. Real CRUD operations on projects, hypotheses,
 * comments, and activity feed.
 */

import { db } from "@/lib/db";

export interface CreateProjectInput {
  name: string;
  description?: string;
  ownerId: string;
  organizationId: string;
  visibility?: "private" | "org" | "public";
  tags?: string[];
}

export async function createProject(input: CreateProjectInput) {
  return db.project.create({
    data: {
      name: input.name,
      description: input.description || null,
      ownerId: input.ownerId,
      organizationId: input.organizationId,
      visibility: input.visibility || "private",
      tags: (input.tags || []).join(","),
    },
  });
}

export async function listProjects(orgId: string) {
  return db.project.findMany({
    where: { organizationId: orgId },
    orderBy: { updatedAt: "desc" },
    include: {
      _count: { select: { hypotheses: true, comments: true } },
    },
  });
}

export async function getProject(id: string) {
  return db.project.findUnique({
    where: { id },
    include: {
      hypotheses: { orderBy: { updatedAt: "desc" } },
      comments: { orderBy: { createdAt: "desc" }, take: 50 },
      activities: { orderBy: { createdAt: "desc" }, take: 50 },
    },
  });
}

export interface CreateHypothesisInput {
  projectId: string;
  title: string;
  drugName: string;
  diseaseName: string;
  createdById: string;
  notes?: string;
}

export async function createHypothesis(input: CreateHypothesisInput) {
  const hyp = await db.hypothesis.create({
    data: {
      projectId: input.projectId,
      title: input.title,
      drugName: input.drugName,
      diseaseName: input.diseaseName,
      createdById: input.createdById,
      notes: input.notes,
      status: "draft",
    },
  });
  await db.projectActivity.create({
    data: {
      projectId: input.projectId,
      type: "hypothesis_created",
      actorName: "system",
      summary: `Created hypothesis "${input.title}" (${input.drugName} → ${input.diseaseName})`,
    },
  });
  return hyp;
}

export async function listHypotheses(projectId: string) {
  return db.hypothesis.findMany({
    where: { projectId },
    orderBy: { updatedAt: "desc" },
  });
}

export async function addComment(projectId: string, authorName: string, body: string) {
  const comment = await db.comment.create({
    data: { projectId, authorName, body },
  });
  await db.projectActivity.create({
    data: {
      projectId,
      type: "comment_added",
      actorName: authorName,
      summary: body.slice(0, 120),
    },
  });
  return comment;
}

export async function listComments(projectId: string) {
  return db.comment.findMany({
    where: { projectId },
    orderBy: { createdAt: "desc" },
  });
}

export async function listProjectActivities(projectId: string) {
  return db.projectActivity.findMany({
    where: { projectId },
    orderBy: { createdAt: "desc" },
    take: 100,
  });
}
