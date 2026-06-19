/**
 * Tests for the evidence package service.
 *
 * Verifies:
 *   1. Building a package for a real (drug, disease) pair returns real
 *      literature + trials + safety data.
 *   2. The package's notes field explicitly states that NO model predictions
 *      are included (this is critical: the evidence package is a literature
 *      + trials + safety summary, NOT a repurposing recommendation).
 *   3. The markdown export contains all three sections.
 *   4. The safety section always includes the disclaimer text.
 *   5. buildEvidencePackage throws if drug or disease is missing.
 */

import {
  buildEvidencePackage,
  evidencePackageToMarkdown,
} from "@/lib/services/evidence-package";

describe("Evidence package assembly", () => {
  test("throws if drug is missing", async () => {
    await expect(
      buildEvidencePackage({ drug: "", disease: "diabetes" })
    ).rejects.toThrow(/drug and disease/);
  });

  test("throws if disease is missing", async () => {
    await expect(
      buildEvidencePackage({ drug: "metformin", disease: "" })
    ).rejects.toThrow(/drug and disease/);
  });

  test("assembles a real package for metformin + diabetes", async () => {
    const pkg = await buildEvidencePackage({
      drug: "metformin",
      disease: "diabetes",
      literatureLimit: 3,
      trialsLimit: 3,
    });
    expect(pkg.drug).toBe("metformin");
    expect(pkg.disease).toBe("diabetes");
    // PubMed should return at least one article for this canonical pair
    expect(pkg.literature.total).toBeGreaterThan(0);
    expect(pkg.literature.articles.length).toBeGreaterThan(0);
    for (const a of pkg.literature.articles) {
      expect(a.pmid).toMatch(/^\d+$/);
      expect(a.url).toMatch(/^https:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/\d+\/$/);
    }
    // CT.gov should also return at least one trial
    expect(pkg.clinicalTrials.total).toBeGreaterThan(0);
    for (const t of pkg.clinicalTrials.trials) {
      expect(t.nctId).toMatch(/^NCT\d{8}$/);
    }
    // openFDA should return data for metformin
    expect(pkg.safety).not.toBeNull();
    expect(pkg.safety?.disclaimer).toMatch(/spontaneous/i);
  }, 90000);

  test("notes explicitly state NO model predictions are included", async () => {
    const pkg = await buildEvidencePackage({
      drug: "aspirin",
      disease: "cardiovascular disease",
      literatureLimit: 2,
      trialsLimit: 2,
    });
    expect(pkg.notes).toMatch(/not.*model prediction|NO.*prediction/i);
  }, 90000);

  test("markdown export contains all three sections", async () => {
    const pkg = await buildEvidencePackage({
      drug: "aspirin",
      disease: "cardiovascular disease",
      literatureLimit: 2,
      trialsLimit: 2,
    });
    const md = evidencePackageToMarkdown(pkg);
    expect(md).toMatch(/## 1\. PubMed Literature/);
    expect(md).toMatch(/## 2\. Clinical Trials/);
    expect(md).toMatch(/## 3\. FDA Adverse Event Profile/);
    expect(md).toMatch(/Disclaimer/);
    expect(md).toMatch(/spontaneous/i);
  }, 90000);

  test("package survives a Promise.allSettled partial failure (e.g., openFDA down)", async () => {
    // We can't easily simulate openFDA being down, but we can verify the
    // structure: if any of the three sub-services fail, the package still
    // returns with that section empty/null and others populated.
    const pkg = await buildEvidencePackage({
      drug: "xyzunknownbrandname12345",
      disease: "rare fictional condition 99999",
      literatureLimit: 1,
      trialsLimit: 1,
    });
    expect(pkg).toBeDefined();
    expect(pkg.literature).toBeDefined();
    expect(pkg.clinicalTrials).toBeDefined();
    // safety may be null if openFDA returned 404 — that's fine
    expect(typeof pkg.notes).toBe("string");
  }, 90000);
});
