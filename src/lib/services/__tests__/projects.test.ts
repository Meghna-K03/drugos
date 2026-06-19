/**
 * Tests for the projects & collaboration service.
 *
 * Verifies:
 *   1. Projects can be created with the correct visibility and tags.
 *   2. Hypotheses can be added to a project.
 *   3. Comments can be added and are linked to the project.
 *   4. Each comment creates a corresponding ProjectActivity entry.
 *   5. Deleting a project cascades to its hypotheses, comments, and activities.
 */

import { createProject, createHypothesis, addComment, listProjects, getProject } from "@/lib/services/projects";
import { db } from "@/lib/db";

describe("Projects & collaboration", () => {
  let testUserId: string;
  let testOrgId: string;

  beforeEach(async () => {
    const user = await db.user.create({
      data: {
        email: "projects-test@example.com",
        passwordHash: "$2b$12$placeholderhashplaceholderhashplaceholderhashplaceholderhashplaceholderhashplacehold",
        name: "Projects Test",
        role: "owner",
      },
    });
    const org = await db.organization.create({
      data: { name: "Projects Test Org", slug: "projects-test", plan: "team", seats: 10 },
    });
    await db.organizationMember.create({
      data: { userId: user.id, organizationId: org.id, role: "owner" },
    });
    testUserId = user.id;
    testOrgId = org.id;
  });

  test("createProject persists name, visibility, and tags", async () => {
    const project = await createProject({
      name: "Drug Repurposing for Alzheimer's",
      description: "Investigating FDA-approved drugs for AD",
      ownerId: testUserId,
      organizationId: testOrgId,
      visibility: "org",
      tags: ["alzheimer", "neurodegenerative"],
    });
    expect(project.id).toBeDefined();
    expect(project.name).toBe("Drug Repurposing for Alzheimer's");
    expect(project.visibility).toBe("org");
    expect(project.tags).toBe("alzheimer,neurodegenerative");
  });

  test("listProjects returns only projects for the given org", async () => {
    await createProject({ name: "P1", ownerId: testUserId, organizationId: testOrgId });
    await createProject({ name: "P2", ownerId: testUserId, organizationId: testOrgId });

    // Create a different org and a project in it
    const otherOrg = await db.organization.create({ data: { name: "Other Org", slug: "other-org" } });
    await createProject({ name: "P3", ownerId: testUserId, organizationId: otherOrg.id });

    const projects = await listProjects(testOrgId);
    expect(projects.length).toBe(2);
    expect(projects.map((p) => p.name).sort()).toEqual(["P1", "P2"]);
  });

  test("createHypothesis adds a draft hypothesis and emits a project_activity", async () => {
    const project = await createProject({ name: "Test", ownerId: testUserId, organizationId: testOrgId });
    const hyp = await createHypothesis({
      projectId: project.id,
      title: "Metformin for Alzheimer's",
      drugName: "metformin",
      diseaseName: "Alzheimer's disease",
      createdById: testUserId,
      notes: "Initial hypothesis",
    });
    expect(hyp.status).toBe("draft");
    expect(hyp.drugName).toBe("metformin");
    expect(hyp.diseaseName).toBe("Alzheimer's disease");

    const activities = await db.projectActivity.findMany({ where: { projectId: project.id } });
    expect(activities.length).toBe(1);
    expect(activities[0].type).toBe("hypothesis_created");
    expect(activities[0].summary).toMatch(/Metformin for Alzheimer/);
  });

  test("addComment persists comment and emits a project_activity", async () => {
    const project = await createProject({ name: "Test", ownerId: testUserId, organizationId: testOrgId });
    const comment = await addComment(project.id, "Dr. Smith", "Looks promising. Let's run a literature search.");
    expect(comment.body).toMatch(/promising/);
    expect(comment.authorName).toBe("Dr. Smith");

    const activities = await db.projectActivity.findMany({ where: { projectId: project.id } });
    expect(activities.length).toBe(1);
    expect(activities[0].type).toBe("comment_added");
  });

  test("getProject includes hypotheses, comments, and activities", async () => {
    const project = await createProject({ name: "Test", ownerId: testUserId, organizationId: testOrgId });
    await createHypothesis({
      projectId: project.id,
      title: "H1",
      drugName: "aspirin",
      diseaseName: "migraine",
      createdById: testUserId,
    });
    await addComment(project.id, "User", "Comment 1");

    const loaded = await getProject(project.id);
    expect(loaded).not.toBeNull();
    expect(loaded?.hypotheses.length).toBe(1);
    expect(loaded?.comments.length).toBe(1);
    expect(loaded?.activities.length).toBe(2); // hypothesis_created + comment_added
  });

  test("deleting a project cascades to its hypotheses, comments, and activities", async () => {
    const project = await createProject({ name: "Test", ownerId: testUserId, organizationId: testOrgId });
    const projectId = project.id;
    await createHypothesis({
      projectId,
      title: "H1",
      drugName: "aspirin",
      diseaseName: "migraine",
      createdById: testUserId,
    });
    await addComment(projectId, "User", "Comment 1");

    await db.project.delete({ where: { id: projectId } });

    const hypotheses = await db.hypothesis.count({ where: { projectId } });
    const comments = await db.comment.count({ where: { projectId } });
    const activities = await db.projectActivity.count({ where: { projectId } });
    expect(hypotheses).toBe(0);
    expect(comments).toBe(0);
    expect(activities).toBe(0);
  });
});
