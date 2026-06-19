/**
 * Playwright E2E tests for the DrugOS frontend.
 *
 * These tests verify that the major user-facing screens render and that the
 * core navigation flow works. They run against the live dev server.
 *
 * Run with:
 *   npx playwright test
 *
 * Or via the helper script:
 *   node scripts/run-e2e-tests.js
 */

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // Sequential to avoid killing the dev server
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://localhost:3000",
    trace: "on-first-retry",
    headless: true,
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
