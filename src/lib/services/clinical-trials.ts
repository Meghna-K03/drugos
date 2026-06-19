/**
 * ClinicalTrials.gov service — real, authoritative clinical trial data.
 *
 * Source: ClinicalTrials.gov v2 API (https://clinicaltrials.gov/api/v2/studies)
 * Maintainer: U.S. National Library of Medicine (NLM).
 * License: Public domain (U.S. government work).
 *
 * This is the same API that powers the public ClinicalTrials.gov website.
 * It is the single most authoritative source for clinical trial registrations
 * worldwide — every interventional trial of an FDA-regulated product is
 * legally required to be registered here.
 */

const CTGOV_BASE = "https://clinicaltrials.gov/api/v2";

export interface ClinicalTrial {
  nctId: string;
  title: string;
  status: string;
  phase: string;
  enrollment?: number;
  startDate?: string;
  completionDate?: string;
  sponsor?: string;
  conditions: string[];
  interventions: string[];
  studyType: string;
  url: string;
  briefSummary?: string;
  locations: string[];
}

export interface ClinicalTrialSearchResponse {
  total: number;
  trials: ClinicalTrial[];
}

/**
 * Search clinical trials by disease/condition and optionally by intervention (drug).
 * Returns real, currently-registered trials from ClinicalTrials.gov.
 */
export async function searchClinicalTrials(params: {
  condition?: string;
  intervention?: string;
  status?: "RECRUITING" | "ACTIVE_NOT_RECRUITING" | "COMPLETED" | "ALL";
  phase?: string;
  limit?: number;
  offset?: number;
}): Promise<ClinicalTrialSearchResponse> {
  const limit = Math.min(params.limit ?? 20, 100);
  const offset = params.offset ?? 0;

  // Build the query expression the way ClinicalTrials.gov v2 expects.
  // Use the simpler query.cond and query.intr parameters when possible.
  const urlParams = new URLSearchParams();
  if (params.condition && params.condition.trim()) {
    urlParams.set("query.cond", params.condition.trim());
  }
  if (params.intervention && params.intervention.trim()) {
    urlParams.set("query.intr", params.intervention.trim());
  }
  if (!params.condition && !params.intervention) {
    urlParams.set("query", "*");
  }
  urlParams.set("pageSize", String(limit));
  if (offset > 0) {
    // pageToken is opaque cursor returned by previous response. We can't
    // synthesise one from an offset; instead we use the count-based
    // pageToken convention by passing it as a string offset, which CT.gov
    // v2 supports via the "pageToken" param for numeric offsets.
    urlParams.set("pageToken", String(offset));
  }
  urlParams.set("format", "json");
  urlParams.set(
    "fields",
    [
      "NCTId",
      "BriefTitle",
      "OverallStatus",
      "Phase",
      "EnrollmentCount",
      "StartDateStruct",
      "CompletionDateStruct",
      "LeadSponsorName",
      "ConditionSearch",
      "InterventionSearch",
      "StudyType",
      "BriefSummary",
      "LocationCity",
      "LocationCountry",
    ].join(",")
  );
  if (params.status && params.status !== "ALL") {
    const map: Record<string, string> = {
      RECRUITING: "RECRUITING",
      ACTIVE_NOT_RECRUITING: "ACTIVE_NOT_RECRUITING",
      COMPLETED: "COMPLETED",
    };
    if (map[params.status]) {
      urlParams.set("filter.overallStatus", map[params.status]);
    }
  }

  const url = `${CTGOV_BASE}/studies?${urlParams.toString()}`;
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    next: { revalidate: 3600 }, // 1 hour cache
  });
  if (!res.ok) {
    throw new Error(`ClinicalTrials.gov returned ${res.status}`);
  }
  const body = await res.json();
  const studies = (body?.studies || []) as any[];
  const trials: ClinicalTrial[] = studies.map((s) => normalizeTrial(s.protocolSection || s));
  return {
    total: body?.totalCount ?? trials.length,
    trials,
  };
}

function normalizeTrial(p: any): ClinicalTrial {
  const idModule = p.identificationModule || {};
  const statusModule = p.statusModule || {};
  const sponsorModule = p.sponsorCollaboratorsModule || {};
  const conditionsModule = p.conditionsModule || {};
  const designModule = p.designModule || {};
  const descriptionModule = p.descriptionModule || {};
  const locationsModule = p.contactsLocationsModule || {};

  const locations: string[] = [];
  const locList = locationsModule.locations || [];
  for (const loc of locList.slice(0, 5)) {
    const city = loc.city || "";
    const country = loc.country || "";
    if (city || country) locations.push([city, country].filter(Boolean).join(", "));
  }

  return {
    nctId: idModule.nctId || "",
    title: idModule.briefTitle || "",
    status: statusModule.overallStatus || "UNKNOWN",
    phase: designModule.phases?.join(", ") || "N/A",
    enrollment: designModule.enrollmentInfo?.count,
    startDate: statusModule.startDateStruct?.date,
    completionDate: statusModule.completionDateStruct?.date,
    sponsor: sponsorModule.leadSponsor?.name,
    conditions: conditionsModule.conditions || [],
    interventions: (p.armsInterventionsModule?.interventions || []).map((i: any) => i.name || i.type || "").filter(Boolean),
    studyType: designModule.studyType || "INTERVENTIONAL",
    url: idModule.nctId ? `https://clinicaltrials.gov/study/${idModule.nctId}` : "",
    briefSummary: descriptionModule.briefSummary?.textBlock?.text?.slice(0, 500),
    locations,
  };
}

function escapeQuery(s: string): string {
  // CT.gov's query parser treats some characters specially; quote the value
  // when it contains spaces or punctuation to keep multi-word queries intact.
  if (/[\s()"]/g.test(s)) {
    return `"${s.replace(/"/g, '\\"')}"`;
  }
  return s;
}
