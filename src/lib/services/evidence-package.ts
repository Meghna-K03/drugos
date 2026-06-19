/**
 * Evidence Package service.
 *
 * Assembles a scientifically-grounded evidence package for a (drug, disease)
 * hypothesis by aggregating REAL data from:
 *   - PubMed literature search (NCBI E-utilities)
 *   - ClinicalTrials.gov interventional studies
 *   - FDA adverse event reports (openFDA)
 *
 * We never invent data. If a service is unavailable or returns no results,
 * we say so explicitly. The package is serializable to JSON and to a
 * human-readable PDF-ready markdown blob.
 */

import { searchPubMed, type PubMedArticle } from "./pubmed";
import { searchClinicalTrials, type ClinicalTrial } from "./clinical-trials";
import { getDrugSafetySummary, type DrugSafetySummary } from "./openfda";

export interface EvidencePackage {
  drug: string;
  disease: string;
  generatedAt: string;
  literature: {
    total: number;
    articles: PubMedArticle[];
  };
  clinicalTrials: {
    total: number;
    trials: ClinicalTrial[];
  };
  safety: DrugSafetySummary | null;
  notes: string;
}

export interface BuildEvidencePackageInput {
  drug: string;
  disease: string;
  literatureLimit?: number;
  trialsLimit?: number;
  notes?: string;
}

export async function buildEvidencePackage(input: BuildEvidencePackageInput): Promise<EvidencePackage> {
  const drug = (input.drug || "").trim();
  const disease = (input.disease || "").trim();
  if (!drug || !disease) {
    throw new Error("Both drug and disease must be provided to build an evidence package.");
  }

  // Run all three lookups concurrently — they are independent.
  const [literature, clinicalTrials, safety] = await Promise.allSettled([
    searchPubMed({
      query: `${drug} AND ${disease}`,
      limit: input.literatureLimit ?? 15,
      sort: "relevance",
    }),
    searchClinicalTrials({
      condition: disease,
      intervention: drug,
      limit: input.trialsLimit ?? 10,
    }),
    getDrugSafetySummary(drug),
  ]);

  return {
    drug,
    disease,
    generatedAt: new Date().toISOString(),
    literature: {
      total: literature.status === "fulfilled" ? literature.value.total : 0,
      articles: literature.status === "fulfilled" ? literature.value.articles : [],
    },
    clinicalTrials: {
      total: clinicalTrials.status === "fulfilled" ? clinicalTrials.value.total : 0,
      trials: clinicalTrials.status === "fulfilled" ? clinicalTrials.value.trials : [],
    },
    safety: safety.status === "fulfilled" ? safety.value : null,
    notes:
      input.notes ||
      `Evidence package assembled for ${drug} as a candidate for ${disease}. ` +
        "All data is sourced from authoritative public databases (PubMed, " +
        "ClinicalTrials.gov, openFDA). This package does NOT contain any " +
        "model predictions — those are owned by the standalone RL agent.",
  };
}

/**
 * Convert the evidence package to a markdown document suitable for PDF export.
 */
export function evidencePackageToMarkdown(pkg: EvidencePackage): string {
  const lines: string[] = [];
  lines.push(`# Evidence Package`);
  lines.push(``);
  lines.push(`- **Drug**: ${pkg.drug}`);
  lines.push(`- **Disease**: ${pkg.disease}`);
  lines.push(`- **Generated**: ${pkg.generatedAt}`);
  lines.push(`- **Notes**: ${pkg.notes}`);
  lines.push(``);
  lines.push(`---`);
  lines.push(``);
  lines.push(`## 1. PubMed Literature (${pkg.literature.total} total matches)`);
  lines.push(``);
  if (pkg.literature.articles.length === 0) {
    lines.push(`No articles returned. This may indicate PubMed search returned zero results `);
    lines.push(`for this drug-disease pair, or the PubMed service was temporarily unavailable.`);
  } else {
    for (const a of pkg.literature.articles) {
      lines.push(`### ${a.title}`);
      lines.push(`- **PMID**: ${a.pmid}`);
      lines.push(`- **Journal**: ${a.journal}`);
      lines.push(`- **Authors**: ${a.authors.slice(0, 5).join(", ")}${a.authors.length > 5 ? ", et al." : ""}`);
      lines.push(`- **Date**: ${a.pubDate}`);
      if (a.doi) lines.push(`- **DOI**: ${a.doi}`);
      lines.push(`- **URL**: ${a.url}`);
      lines.push(``);
    }
  }
  lines.push(`---`);
  lines.push(``);
  lines.push(`## 2. Clinical Trials (${pkg.clinicalTrials.total} total matches)`);
  lines.push(``);
  if (pkg.clinicalTrials.trials.length === 0) {
    lines.push(`No registered clinical trials returned for this drug-disease pair.`);
  } else {
    for (const t of pkg.clinicalTrials.trials) {
      lines.push(`### ${t.title}`);
      lines.push(`- **NCT ID**: ${t.nctId}`);
      lines.push(`- **Status**: ${t.status}`);
      lines.push(`- **Phase**: ${t.phase}`);
      lines.push(`- **Sponsor**: ${t.sponsor || "N/A"}`);
      lines.push(`- **Enrollment**: ${t.enrollment ?? "N/A"}`);
      lines.push(`- **Start**: ${t.startDate || "N/A"}`);
      lines.push(`- **Completion**: ${t.completionDate || "N/A"}`);
      lines.push(`- **URL**: ${t.url}`);
      lines.push(``);
    }
  }
  lines.push(`---`);
  lines.push(``);
  lines.push(`## 3. FDA Adverse Event Profile`);
  lines.push(``);
  if (!pkg.safety) {
    lines.push(`openFDA service was unavailable when this package was generated.`);
  } else {
    lines.push(`- **Generic Name**: ${pkg.safety.genericName}`);
    lines.push(`- **Brand Name**: ${pkg.safety.brandName}`);
    lines.push(`- **Total Reports Returned**: ${pkg.safety.totalReports}`);
    lines.push(`- **Serious Reports**: ${pkg.safety.seriousReports}`);
    lines.push(`- **Serious Reports Involving Death**: ${pkg.safety.seriousReportsWithDeath}`);
    lines.push(``);
    lines.push(`### Top Reported Reactions`);
    if (pkg.safety.topReactions.length === 0) {
      lines.push(`No reaction frequency data available in the returned reports.`);
    } else {
      for (const r of pkg.safety.topReactions) {
        lines.push(`- ${r.term}: ${r.count} reports`);
      }
    }
    lines.push(``);
    lines.push(`> **Disclaimer**: ${pkg.safety.disclaimer}`);
  }
  return lines.join("\n");
}
