import { NextResponse } from "next/server";
import {
  checkKnowledgeGraphAvailability,
  checkDatasetAvailability,
  checkRlAvailability,
} from "@/lib/services/ml-stubs";

export async function GET() {
  return NextResponse.json({
    services: {
      auth: { available: true, service: "Authentication" },
      rxnorm: { available: true, service: "RxNorm Drug Search" },
      mesh: { available: true, service: "MeSH Disease Search" },
      clinicalTrials: { available: true, service: "ClinicalTrials.gov Search" },
      pubmed: { available: true, service: "PubMed Literature Search" },
      openfda: { available: true, service: "openFDA Adverse Events" },
      patentsview: {
        available: !!process.env.PATENTSVIEW_API_KEY,
        service: "USPTO Patent Search",
        reason: process.env.PATENTSVIEW_API_KEY
          ? undefined
          : "PATENTSVIEW_API_KEY not configured",
      },
      projects: { available: true, service: "Projects & Collaboration" },
      billing: { available: true, service: "Billing & Subscriptions" },
      admin: { available: true, service: "Admin Console" },
      apiKeys: { available: true, service: "Developer API Keys" },
      evidence: { available: true, service: "Evidence Packages" },
      knowledgeGraph: checkKnowledgeGraphAvailability(),
      dataset: checkDatasetAvailability(),
      rl: checkRlAvailability(),
    },
    generatedAt: new Date().toISOString(),
  });
}
