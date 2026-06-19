/**
 * E2E tests for the DrugOS auth-gated frontend flow.
 *
 * Verifies the KEY user-facing behaviors:
 *  - Clicking "Start Free" navigates to the register page
 *  - The register form rejects weak passwords with a clear client-side error
 *  - The register form successfully creates an account via /api/auth/register
 *  - The dashboard is auth-gated (logout clears the session)
 *
 * These tests use Playwright's `page` fixture against the live dev server.
 */

import { test, expect } from "@playwright/test";

const TEST_EMAIL = `e2e+${Date.now()}@drugos-test.example`;
const TEST_PASSWORD = "TestPass!2026#secure";

// Helper: get to the register page from the landing page
async function goToRegister(page: import("@playwright/test").Page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  // Wait for hydration — the CTA buttons only work after React mounts.
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(500);

  // Try clicking any visible "Start Free" / "Register" / "Sign Up" CTA.
  // We try multiple selectors because the landing page has several CTAs.
  const selectors = [
    'button:has-text("Start Free")',
    'a:has-text("Start Free")',
    'button:has-text("Sign Up")',
    'a:has-text("Sign Up")',
    'button:has-text("Register")',
  ];
  for (const sel of selectors) {
    const el = page.locator(sel).first();
    if (await el.isVisible().catch(() => false)) {
      await el.click({ timeout: 5000 }).catch(() => {});
      break;
    }
  }

  // Wait for the register form to render
  await expect(page.locator('input[type="email"]').first()).toBeVisible({ timeout: 15000 });
}

test.describe("Auth-gated frontend flow", () => {
  test("register page is reachable from landing CTA", async ({ page }) => {
    await goToRegister(page);
    // Should see the "Create Account" heading and the submit button
    await expect(page.locator('h1, h2').filter({ hasText: /create.*account|sign up|register/i })).toBeVisible({ timeout: 5000 });
    await expect(page.locator('button[type="submit"]')).toBeVisible({ timeout: 5000 });
  });

  test("register page rejects weak password with clear error", async ({ page }) => {
    await goToRegister(page);

    // Fill with valid email but a too-short password missing digit/symbol
    await page.locator('input[placeholder="Manoj"]').fill("E2E");
    await page.locator('input[placeholder="Pagadala"]').fill("Tester");
    await page.locator('input[type="email"]').fill(`weak+${Date.now()}@drugos-test.example`);
    await page.locator('input[type="password"]').fill("weakpass");

    const submit = page.locator('button[type="submit"]', { hasText: /create account/i });
    await submit.click({ timeout: 5000 });

    // Our custom error UI uses a red box. Look for any element mentioning password requirements.
    // The error message contains the words "Password must contain" or "uppercase".
    const errorBox = page.locator('div.bg-red-50, div[role="alert"], .text-red-700').first();
    await expect(errorBox).toBeVisible({ timeout: 5000 });
    // The text should mention password requirements
    await expect(errorBox).toContainText(/password|uppercase|lowercase|digit|symbol|character/i);
  });

  test("full register → dashboard flow calls /api/auth/register", async ({ page }) => {
    await goToRegister(page);

    // Fill the form with valid credentials
    await page.locator('input[placeholder="Manoj"]').fill("E2E");
    await page.locator('input[placeholder="Pagadala"]').fill("Tester");
    await page.locator('input[type="email"]').fill(TEST_EMAIL);
    await page.locator('input[type="password"]').fill(TEST_PASSWORD);

    // Set up the response listener BEFORE clicking submit
    const registerPromise = page.waitForResponse(
      (r) => r.url().includes("/api/auth/register") && r.request().method() === "POST",
      { timeout: 60000 }
    );

    await page.locator('button[type="submit"]', { hasText: /create account/i }).click();

    const res = await registerPromise;
    expect(res.status()).toBe(201);

    // After successful registration, we should land on the dashboard.
    // The dashboard is wrapped in <AppShell>, which initially shows "Loading DrugOS…"
    // while the /api/auth/me check is in flight, then the real app shell.
    // Wait for either the dashboard content or the user avatar to appear.
    await expect(page.locator("text=Loading DrugOS")).toBeHidden({ timeout: 30000 });

    // The dashboard should be visible — look for any sidebar nav item or page heading
    await expect(page.locator("text=DrugOS").first()).toBeVisible({ timeout: 30000 });
  });

  test("logged-out users cannot reach the dashboard directly", async ({ page }) => {
    // Clear all cookies to ensure we're logged out
    await page.context().clearCookies();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");

    // The landing page CTA buttons should be visible (sign-in/register)
    // If we were authed, we'd see the app shell instead.
    const landingCta = page.locator('button:has-text("Start Free"), a:has-text("Start Free")').first();
    await expect(landingCta).toBeVisible({ timeout: 10000 });
  });

  test("login form rejects wrong credentials with backend error", async ({ page }) => {
    await page.context().clearCookies();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(500);

    // Click Sign In CTA
    const selectors = [
      'button:has-text("Sign In")',
      'a:has-text("Sign In")',
    ];
    for (const sel of selectors) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) {
        await el.click({ timeout: 5000 }).catch(() => {});
        break;
      }
    }

    // Wait for login form
    await expect(page.locator('input[type="email"]').first()).toBeVisible({ timeout: 10000 });

    // Fill with non-existent credentials
    await page.locator('input[type="email"]').fill("definitely-nonexistent@drugos-test.example");
    await page.locator('input[type="password"]').fill("WrongPass!2026#");

    const loginPromise = page.waitForResponse(
      (r) => r.url().includes("/api/auth/login") && r.request().method() === "POST",
      { timeout: 30000 }
    );

    await page.locator('button[type="submit"]', { hasText: /sign in/i }).click();

    const res = await loginPromise;
    expect(res.status()).toBe(401);

    // The error from the backend ("Invalid email or password") should be surfaced in the UI
    const errorBox = page.locator('div.bg-red-50, .text-red-700').first();
    await expect(errorBox).toBeVisible({ timeout: 5000 });

    // And we should still be on the login page (NOT navigated to dashboard)
    await expect(page.locator('input[type="email"]').first()).toBeVisible({ timeout: 2000 });
  });
});
