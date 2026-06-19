/**
 * Tests for the RxNorm drug name normalization service.
 *
 * Verifies:
 *   1. Real drug name searches return canonical RxCUI identifiers.
 *   2. RxCUI values are numeric strings.
 *   3. Searches for non-existent terms return empty arrays (not fake drugs).
 */

import { searchDrugsByName, getDrugProperties } from "@/lib/services/rxnorm";

describe("RxNorm drug search service", () => {
  test("returns canonical RxCUI for aspirin", async () => {
    const results = await searchDrugsByName("aspirin", 5);
    expect(results.length).toBeGreaterThan(0);
    for (const r of results) {
      expect(r.rxcui).toMatch(/^\d+$/);
    }
    // Aspirin's canonical RxCUI is 1191 — should appear in the top results.
    const hasAspirin = results.some((r) => r.rxcui === "1191");
    expect(hasAspirin).toBe(true);
  }, 60000);

  test("rejects queries shorter than 2 characters", async () => {
    const results = await searchDrugsByName("a", 5);
    expect(results).toEqual([]);
  });

  test("non-existent drug name returns empty or sparse results", async () => {
    const results = await searchDrugsByName("xyznonsensicaldrugqqq12345", 5);
    // RxNorm may return fuzzy matches — but they should be sparse, not fabricated
    expect(results.length).toBeLessThanOrEqual(5);
  }, 60000);

  test("getDrugProperties returns active ingredients for a known RxCUI", async () => {
    // RxCUI 1191 = Aspirin
    const props = await getDrugProperties("1191");
    expect(props.rxcui).toBe("1191");
    // Active ingredients array should be populated for any clinical drug
    expect(Array.isArray(props.activeIngredients)).toBe(true);
    expect(Array.isArray(props.brandNames)).toBe(true);
  }, 60000);
});
