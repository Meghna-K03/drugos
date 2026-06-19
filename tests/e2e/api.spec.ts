/**
 * E2E tests for the DrugOS backend APIs.
 *
 * These tests use Playwright's APIRequestContext to hit the API endpoints
 * directly. They are simpler than full browser E2E tests and verify that
 * the backend is correctly wired up.
 */

import { test, expect, type APIRequestContext } from "@playwright/test";

test.describe("Backend API health", () => {
  test("GET /api/system/status returns 200", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/system/status");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.services).toBeDefined();
    expect(body.services.auth.available).toBe(true);
    expect(body.services.pubmed.available).toBe(true);
    expect(body.services.knowledgeGraph.available).toBe(false);
    expect(body.services.dataset.available).toBe(false);
    expect(body.services.rl.available).toBe(false);
  });

  test("GET /api/billing/plans returns canonical plans", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/billing/plans");
    expect(res.status()).toBe(200);
    const body = await res.json();
    const ids = body.plans.map((p: any) => p.id);
    expect(ids).toEqual(expect.arrayContaining(["free", "researcher", "team", "enterprise"]));
  });
});

test.describe("Backend auth flow", () => {
  const testEmail = `e2e-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  let authCookie: string | null = null;

  test("POST /api/auth/register creates a user", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.post("/api/auth/register", {
      data: {
        email: testEmail,
        password: "TestPassword123!",
        name: "E2E User",
        organizationName: "E2E Org",
      },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body.user.email).toBe(testEmail);
    // Capture cookie for subsequent tests
    const setCookie = res.headers()["set-cookie"];
    if (setCookie) {
      authCookie = setCookie.split(";")[0];
    }
  });

  test("POST /api/auth/login accepts the new user", async ({ request }: { request: APIRequestContext }) => {
    // Note: this test depends on the previous one having created the user.
    // Playwright runs tests in declaration order within a describe block.
    const res = await request.post("/api/auth/login", {
      data: { email: testEmail, password: "TestPassword123!" },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.user.email).toBe(testEmail);
  });

  test("POST /api/auth/login rejects wrong password", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.post("/api/auth/login", {
      data: { email: testEmail, password: "WrongPassword999!" },
    });
    expect(res.status()).toBe(401);
  });
});

test.describe("Backend biomedical APIs (live)", () => {
  test("GET /api/literature/search returns real PubMed articles", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/literature/search?q=metformin+diabetes&limit=3");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.total).toBeGreaterThan(0);
    expect(body.articles.length).toBeGreaterThan(0);
    for (const a of body.articles) {
      expect(a.pmid).toMatch(/^\d+$/);
      expect(a.url).toMatch(/^https:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/\d+\/$/);
    }
  });

  test("GET /api/clinical-trials/search returns real trials", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/clinical-trials/search?condition=asthma&limit=3");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.total).toBeGreaterThan(0);
    for (const t of body.trials) {
      expect(t.nctId).toMatch(/^NCT\d{8}$/);
    }
  });

  test("GET /api/safety/aspirin returns real FDA data with disclaimer", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/safety/aspirin");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.totalReports).toBeGreaterThan(0);
    expect(body.disclaimer).toMatch(/spontaneous/i);
    expect(body.disclaimer).toMatch(/not prove causation/i);
  });

  test("GET /api/drugs/search returns RxNorm results", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/drugs/search?q=metformin&limit=3");
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.results.length).toBeGreaterThan(0);
    for (const r of body.results) {
      expect(r.rxcui).toMatch(/^\d+$/);
    }
  });
});

test.describe("Backend ML stubs (scientific integrity contract)", () => {
  test("GET /api/knowledge-graph returns 503 with refusal to fabricate", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/knowledge-graph");
    expect(res.status()).toBe(503);
    const body = await res.json();
    expect(body.error).toBe("service_not_deployed");
    expect(body.reason).toMatch(/fabricat/i);
  });

  test("GET /api/dataset returns 503 with refusal to fabricate", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/dataset");
    expect(res.status()).toBe(503);
    const body = await res.json();
    expect(body.error).toBe("service_not_deployed");
    expect(body.reason).toMatch(/fabricat/i);
  });

  test("POST /api/rl returns 503 with refusal to fabricate", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.post("/api/rl");
    expect(res.status()).toBe(503);
    const body = await res.json();
    expect(body.error).toBe("service_not_deployed");
    expect(body.reason).toMatch(/fabricat/i);
  });
});

test.describe("Backend auth-protected endpoints", () => {
  test("GET /api/projects without auth returns 401", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/projects");
    expect(res.status()).toBe(401);
  });

  test("GET /api/api-keys without auth returns 401", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/api-keys");
    expect(res.status()).toBe(401);
  });

  test("GET /api/notifications without auth returns 401", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/notifications");
    expect(res.status()).toBe(401);
  });

  test("GET /api/admin/users without auth returns 401 or 403", async ({ request }: { request: APIRequestContext }) => {
    const res = await request.get("/api/admin/users");
    expect([401, 403]).toContain(res.status());
  });
});
