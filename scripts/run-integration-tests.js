#!/usr/bin/env node
/**
 * Integration test runner — starts a fresh Next.js dev server, runs the
 * integration test suite, and tears down the server.
 *
 * This avoids the issue of the dev server dying when run as a sibling of jest.
 *
 * Usage: node scripts/run-integration-tests.js
 */

const { spawn, execSync } = require("child_process");
const http = require("http");

const PORT = 3010; // Use a different port to avoid conflicts
// BASE_URL is mutable: if a dev server is already running on :3000, main()
// reassigns this to point at it instead of starting a second server.
let BASE_URL = `http://localhost:${PORT}`;

async function waitForServer(maxAttempts = 60) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await fetch(`${BASE_URL}/api/system/status`, {
        signal: AbortSignal.timeout(2000),
      });
      if (res.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

async function runTests() {
  const results = { passed: 0, failed: 0, tests: [] };

  async function test(name, fn) {
    try {
      await fn();
      results.passed++;
      results.tests.push({ name, status: "pass" });
      console.log(`  ✓ ${name}`);
    } catch (e) {
      results.failed++;
      results.tests.push({ name, status: "fail", error: e.message });
      console.log(`  ✕ ${name}`);
      console.log(`    ${e.message}`);
    }
  }

  function assert(cond, msg) {
    if (!cond) throw new Error(msg || "Assertion failed");
  }

  // === Tests ===

  await test("GET /api/system/status returns 200 with all services", async () => {
    const res = await fetch(`${BASE_URL}/api/system/status`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.services.auth.available === true, "auth should be available");
    assert(body.services.pubmed.available === true, "pubmed should be available");
    assert(body.services.clinicalTrials.available === true, "clinicalTrials should be available");
    assert(body.services.openfda.available === true, "openfda should be available");
    assert(body.services.knowledgeGraph.available === false, "knowledgeGraph should NOT be available");
    assert(body.services.dataset.available === false, "dataset should NOT be available");
    assert(body.services.rl.available === false, "rl should NOT be available");
  });

  await test("GET /api/billing/plans returns canonical plan list", async () => {
    const res = await fetch(`${BASE_URL}/api/billing/plans`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    const ids = body.plans.map((p) => p.id);
    assert(ids.includes("free"), "Missing 'free' plan");
    assert(ids.includes("researcher"), "Missing 'researcher' plan");
    assert(ids.includes("team"), "Missing 'team' plan");
    assert(ids.includes("enterprise"), "Missing 'enterprise' plan");
  });

  await test("GET /api/projects without auth returns 401", async () => {
    const res = await fetch(`${BASE_URL}/api/projects`);
    assert(res.status === 401, `Expected 401, got ${res.status}`);
  });

  await test("GET /api/api-keys without auth returns 401", async () => {
    const res = await fetch(`${BASE_URL}/api/api-keys`);
    assert(res.status === 401, `Expected 401, got ${res.status}`);
  });

  await test("GET /api/notifications without auth returns 401", async () => {
    const res = await fetch(`${BASE_URL}/api/notifications`);
    assert(res.status === 401, `Expected 401, got ${res.status}`);
  });

  await test("GET /api/admin/users without admin returns 401 or 403", async () => {
    const res = await fetch(`${BASE_URL}/api/admin/users`);
    assert([401, 403].includes(res.status), `Expected 401 or 403, got ${res.status}`);
  });

  const testEmail = `itest-${Date.now()}@example.com`;
  let authCookies = "";

  await test("POST /api/auth/register creates a user and sets auth cookies", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: testEmail,
        password: "TestPassword123!",
        name: "ITest User",
        organizationName: "ITest Org",
      }),
    });
    assert(res.status === 201, `Expected 201, got ${res.status}`);
    const body = await res.json();
    assert(body.user.email === testEmail, "Email mismatch");
    assert(body.organizationId, "Missing organizationId");
    const setCookie = res.headers.get("set-cookie") || "";
    assert(setCookie.includes("drugos_access="), "Missing drugos_access cookie");
    authCookies = setCookie.split(",").map((c) => c.split(";")[0]).join("; ");
  });

  await test("POST /api/auth/register rejects duplicate email with 409", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: testEmail, password: "DifferentPass456!", name: "Dup" }),
    });
    assert(res.status === 409, `Expected 409, got ${res.status}`);
  });

  await test("POST /api/auth/register rejects weak password with 400", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: `weak-${Date.now()}@example.com`, password: "abc", name: "Weak" }),
    });
    assert(res.status === 400, `Expected 400, got ${res.status}`);
  });

  await test("POST /api/auth/login accepts correct password and returns 200", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: testEmail, password: "TestPassword123!" }),
    });
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.user.email === testEmail, "Email mismatch");
  });

  await test("POST /api/auth/login rejects wrong password with 401", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: testEmail, password: "WrongPassword999!" }),
    });
    assert(res.status === 401, `Expected 401, got ${res.status}`);
  });

  await test("GET /api/auth/me with cookie returns user profile", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/me`, {
      headers: { Cookie: authCookies },
    });
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.user.email === testEmail, "Email mismatch");
    assert(body.organizations.length > 0, "Should have at least one org");
  });

  await test("POST /api/auth/logout clears the session", async () => {
    const res = await fetch(`${BASE_URL}/api/auth/logout`, {
      method: "POST",
      headers: { Cookie: authCookies },
    });
    assert(res.status === 200, `Expected 200, got ${res.status}`);
  });

  await test("GET /api/knowledge-graph returns 503 when not deployed", async () => {
    const res = await fetch(`${BASE_URL}/api/knowledge-graph`);
    assert(res.status === 503, `Expected 503, got ${res.status}`);
    const body = await res.json();
    assert(body.error === "service_not_deployed", "Wrong error code");
    assert(body.reason.match(/fabricat/i), "Reason should mention refusing to fabricate");
  });

  await test("GET /api/dataset returns 503 when not deployed", async () => {
    const res = await fetch(`${BASE_URL}/api/dataset`);
    assert(res.status === 503, `Expected 503, got ${res.status}`);
    const body = await res.json();
    assert(body.error === "service_not_deployed", "Wrong error code");
    assert(body.reason.match(/fabricat/i), "Reason should mention refusing to fabricate");
  });

  await test("POST /api/rl returns 503 when not deployed", async () => {
    const res = await fetch(`${BASE_URL}/api/rl`, { method: "POST" });
    assert(res.status === 503, `Expected 503, got ${res.status}`);
    const body = await res.json();
    assert(body.error === "service_not_deployed", "Wrong error code");
    assert(body.reason.match(/fabricat/i), "Reason should mention refusing to fabricate");
  });

  await test("GET /api/literature/search returns real PubMed articles", async () => {
    const res = await fetch(`${BASE_URL}/api/literature/search?q=aspirin+cardiovascular&limit=2`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.total > 0, "Should return >0 total articles");
    assert(body.articles.length > 0, "Should return >0 articles");
    for (const a of body.articles) {
      assert(/^\d+$/.test(a.pmid), `PMID should be numeric: ${a.pmid}`);
      assert(/^https:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/\d+\/$/.test(a.url), `Bad URL: ${a.url}`);
    }
  });

  await test("GET /api/clinical-trials/search returns real trials", async () => {
    const res = await fetch(`${BASE_URL}/api/clinical-trials/search?condition=diabetes&limit=2`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.total > 0, "Should return >0 total trials");
    for (const t of body.trials) {
      assert(/^NCT\d{8}$/.test(t.nctId), `Bad NCT ID: ${t.nctId}`);
    }
  });

  await test("GET /api/safety/metformin returns real FDA data with disclaimer", async () => {
    const res = await fetch(`${BASE_URL}/api/safety/metformin`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.totalReports > 0, "Should return >0 reports for metformin");
    assert(/spontaneous/i.test(body.disclaimer), "Disclaimer should mention 'spontaneous'");
    assert(/not prove causation/i.test(body.disclaimer), "Disclaimer should say 'does not prove causation'");
  });

  await test("GET /api/drugs/search returns RxNorm results", async () => {
    const res = await fetch(`${BASE_URL}/api/drugs/search?q=aspirin&limit=3`);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    const body = await res.json();
    assert(body.results.length > 0, "Should return >0 results");
    for (const r of body.results) {
      assert(/^\d+$/.test(r.rxcui), `RxCUI should be numeric: ${r.rxcui}`);
    }
  });

  await test("GET /api/evidence-package without auth returns 401", async () => {
    const res = await fetch(`${BASE_URL}/api/evidence-package`);
    assert(res.status === 401, `Expected 401, got ${res.status}`);
  });

  return results;
}

async function main() {
  // If a dev server is already running (e.g., on port 3000), reuse it
  // instead of starting a new one. This avoids Next.js lock conflicts.
  let server = null;
  let baseUrl = BASE_URL;

  const existingUrl = process.env.E2E_BASE_URL || "http://localhost:3000";
  let reuseExisting = false;
  try {
    const res = await fetch(`${existingUrl}/api/system/status`, {
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) {
      reuseExisting = true;
      baseUrl = existingUrl;
    }
  } catch {}

  if (reuseExisting) {
    console.log(`Reusing existing dev server at ${baseUrl}`);
    BASE_URL = baseUrl; // reassign the module-level constant
  } else {
    console.log("Starting Next.js dev server on port", PORT);
    server = spawn("node", ["node_modules/next/dist/bin/next", "dev", "-p", String(PORT)], {
      cwd: process.cwd(),
      detached: false,
      stdio: "pipe",
    });

    server.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
    server.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

    console.log("Waiting for server to be ready...");
    const ready = await waitForServer();
    if (!ready) {
      console.error("Server did not become ready in time");
      process.exit(1);
    }
  }

  // BASE_URL has been reassigned above if we are reusing an existing server.
  // The test functions close over the module-level binding, so they pick up
  // the new value automatically.

  try {
    console.log("Server ready. Running tests...\n");
    const results = await runTests();
    console.log(`\n=== Results: ${results.passed} passed, ${results.failed} failed ===`);
    process.exit(results.failed === 0 ? 0 : 1);
  } finally {
    if (server) {
      console.log("Shutting down server...");
      try { process.kill(-server.pid, "SIGTERM"); } catch {}
      server.kill("SIGTERM");
      server.kill("SIGKILL");
    }
  }
}

main().catch((e) => {
  console.error("Fatal error:", e);
  process.exit(1);
});
