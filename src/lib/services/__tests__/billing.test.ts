/**
 * Tests for the billing service.
 *
 * Verifies:
 *   1. Plan definitions are well-formed (non-negative price, positive seats).
 *   2. changePlan correctly transitions the subscription state.
 *   3. Invoices are generated for non-free plans.
 *   4. Free plans do NOT generate invoices.
 */

import { PLANS, getPlan, changePlan } from "@/lib/services/billing";
import { db } from "@/lib/db";

describe("Billing plans", () => {
  test("all plans have non-negative price and at least one seat", () => {
    for (const plan of PLANS) {
      expect(plan.priceCents).toBeGreaterThanOrEqual(0);
      expect(plan.seats).toBeGreaterThan(0);
      expect(plan.features.length).toBeGreaterThan(0);
    }
  });

  test("free plan has price 0", () => {
    const free = getPlan("free");
    expect(free).toBeDefined();
    expect(free?.priceCents).toBe(0);
  });

  test("getPlan returns undefined for unknown plan IDs", () => {
    expect(getPlan("nonexistent-plan-id")).toBeUndefined();
  });
});

describe("Subscription state machine", () => {
  let testOrgId: string;

  beforeEach(async () => {
    const user = await db.user.create({
      data: {
        email: "billing-test@example.com",
        passwordHash: "$2b$12$placeholderhashplaceholderhashplaceholderhashplaceholderhashplaceholderhashplacehold",
        name: "Billing Test",
        role: "owner",
      },
    });
    const org = await db.organization.create({
      data: {
        name: "Billing Test Org",
        slug: "billing-test",
        plan: "free",
        seats: 1,
      },
    });
    await db.organizationMember.create({
      data: { userId: user.id, organizationId: org.id, role: "owner" },
    });
    testOrgId = org.id;
  });

  test("changePlan(free -> researcher) updates plan and creates invoice", async () => {
    await changePlan(testOrgId, "researcher");
    const sub = await db.subscription.findUnique({ where: { organizationId: testOrgId } });
    expect(sub).not.toBeNull();
    expect(sub?.plan).toBe("researcher");
    expect(sub?.seats).toBe(1);

    const invoices = await db.billingInvoice.findMany({ where: { organizationId: testOrgId } });
    expect(invoices.length).toBe(1);
    expect(invoices[0].amountCents).toBe(4900);
    expect(invoices[0].status).toBe("open");
    expect(invoices[0].number).toMatch(/^INV-\d{4}-\d{2}-[A-Z0-9]+$/);
  });

  test("changePlan to free plan does NOT generate invoice", async () => {
    // First upgrade to researcher (generates invoice)
    await changePlan(testOrgId, "researcher");
    const invoicesAfterUpgrade = await db.billingInvoice.count({ where: { organizationId: testOrgId } });
    expect(invoicesAfterUpgrade).toBe(1);

    // Downgrade to free — should NOT generate a new invoice
    await changePlan(testOrgId, "free");
    const invoicesAfterDowngrade = await db.billingInvoice.count({ where: { organizationId: testOrgId } });
    expect(invoicesAfterDowngrade).toBe(1);
  });

  test("changePlan throws on unknown plan ID", async () => {
    await expect(changePlan(testOrgId, "unknown-plan")).rejects.toThrow(/Unknown plan/);
  });

  test("changePlan(team) sets seats to 10", async () => {
    await changePlan(testOrgId, "team");
    const sub = await db.subscription.findUnique({ where: { organizationId: testOrgId } });
    expect(sub?.seats).toBe(10);
  });
});
