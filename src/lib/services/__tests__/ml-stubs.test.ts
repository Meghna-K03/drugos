/**
 * Tests for the ML service stubs (Knowledge Graph, Dataset, RL).
 *
 * These tests verify the SCIENTIFIC INTEGRITY CONTRACT:
 *   1. When the underlying ML service is not deployed, the stub MUST return
 *      `available: false` with a clear reason — never fabricated data.
 *   2. The reason text must explicitly mention that no fabricated data is
 *      returned.
 *   3. When env vars are set, the stub MUST report availability = true.
 *
 * Returning fabricated predictions here could literally endanger lives —
 * a pharma company might act on a fake "high confidence" repurposing
 * prediction. These tests guard against that.
 */

import {
  checkKnowledgeGraphAvailability,
  checkDatasetAvailability,
  checkRlAvailability,
} from "@/lib/services/ml-stubs";

describe("ML service stubs — scientific integrity contract", () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    // Restore env after each test
    process.env = { ...originalEnv };
  });

  test("knowledge graph stub refuses to fabricate when not deployed", () => {
    delete process.env.KG_SERVICE_URL;
    const avail = checkKnowledgeGraphAvailability();
    expect(avail.available).toBe(false);
    expect(avail.reason).toMatch(/not been deployed|not set|refuses/i);
    expect(avail.reason).toMatch(/fabricat/i);
  });

  test("dataset stub refuses to fabricate when not deployed", () => {
    delete process.env.DATASET_SERVICE_URL;
    const avail = checkDatasetAvailability();
    expect(avail.available).toBe(false);
    expect(avail.reason).toMatch(/not been deployed|not set|refuses/i);
    expect(avail.reason).toMatch(/fabricat/i);
  });

  test("RL stub refuses to fabricate when not deployed", () => {
    delete process.env.RL_SERVICE_URL;
    const avail = checkRlAvailability();
    expect(avail.available).toBe(false);
    expect(avail.reason).toMatch(/not been deployed|not set|refuses/i);
    expect(avail.reason).toMatch(/fabricat/i);
  });

  test("knowledge graph stub reports available when env var is set", () => {
    process.env.KG_SERVICE_URL = "http://kg-service.internal:7474";
    const avail = checkKnowledgeGraphAvailability();
    expect(avail.available).toBe(true);
    expect(avail.reason).toMatch(/kg-service\.internal/);
  });

  test("dataset stub reports available when env var is set", () => {
    process.env.DATASET_SERVICE_URL = "http://airflow.internal:8080";
    const avail = checkDatasetAvailability();
    expect(avail.available).toBe(true);
  });

  test("RL stub reports available when env var is set", () => {
    process.env.RL_SERVICE_URL = "http://rl-agent.internal:8000";
    const avail = checkRlAvailability();
    expect(avail.available).toBe(true);
  });
});
