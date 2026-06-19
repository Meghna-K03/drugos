#!/usr/bin/env node
/**
 * E2E test runner — reuses an existing Next.js dev server if one is already
 * running (e.g., on port 3000), otherwise starts a fresh one. Then runs the
 * Playwright E2E suite and tears down the server it started (if any).
 *
 * Usage: node scripts/run-e2e-tests.js
 */

const { spawn } = require("child_process");

const PORT = 3020;

async function probe(url) {
  try {
    const res = await fetch(`${url}/api/system/status`, {
      signal: AbortSignal.timeout(2000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function waitForServer(url, maxAttempts = 60) {
  for (let i = 0; i < maxAttempts; i++) {
    if (await probe(url)) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

async function main() {
  // First, try the existing dev server on :3000.
  const existingUrl = process.env.E2E_BASE_URL || "http://localhost:3000";
  let baseUrl = existingUrl;
  let server = null;

  if (await probe(existingUrl)) {
    console.log(`Reusing existing dev server at ${existingUrl}`);
  } else {
    // Start a fresh server on PORT.
    baseUrl = `http://localhost:${PORT}`;
    process.env.E2E_BASE_URL = baseUrl;
    console.log("Starting Next.js dev server on port", PORT);
    server = spawn("node", ["node_modules/next/dist/bin/next", "dev", "-p", String(PORT)], {
      cwd: process.cwd(),
      detached: false,
      stdio: "pipe",
    });

    server.stdout.on("data", (d) => process.stderr.write(`[server] ${d}`));
    server.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));

    console.log("Waiting for server to be ready...");
    const ready = await waitForServer(baseUrl);
    if (!ready) {
      console.error("Server did not become ready in time");
      process.exit(1);
    }
  }

  try {
    console.log(`Server ready at ${baseUrl}. Running Playwright tests...\n`);

    const playwright = spawn("node", ["node_modules/.bin/playwright", "test", "--reporter=list"], {
      cwd: process.cwd(),
      stdio: "inherit",
      env: { ...process.env, E2E_BASE_URL: baseUrl },
    });

    await new Promise((resolve) => playwright.on("close", resolve));
    const exitCode = playwright.exitCode ?? 1;
    console.log(`\nPlaywright exited with code ${exitCode}`);
    process.exit(exitCode);
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
