/**
 * RxNorm service — drug name normalization and lookup.
 *
 * Source: NIH/NLM RxNorm REST API (https://rxnav.nlm.nih.gov/)
 * License: Public domain (U.S. government work).
 *
 * We use RxNorm because it is the authoritative source for clinical drug
 * nomenclature in the United States. It maps brand names, generic names,
 * and ingredient codes (RxCUI) to a single canonical concept.
 */

import { z } from "zod";

const RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST";

const RxNormConceptSchema = z.object({
  rxcui: z.string().optional(),
  name: z.string().optional(),
  synonym: z.string().optional(),
  tty: z.string().optional(),
});

export const RxNormSearchResultSchema = z.object({
  idGroup: z
    .object({
      name: z.string().optional(),
      rxnormId: z.array(z.string()).optional(),
    })
    .optional(),
});

export interface NormalizedDrug {
  rxcui: string;
  name: string;
  synonym?: string;
  tty: string;
}

/**
 * Approximate match search — finds concepts whose name sounds like the query.
 * This is the recommended endpoint for free-text drug search.
 */
export async function searchDrugsByName(query: string, limit = 10): Promise<NormalizedDrug[]> {
  const q = (query || "").trim();
  if (q.length < 2) return [];
  const url = `${RXNORM_BASE}/approximateTerm.json?term=${encodeURIComponent(q)}&maxEntries=${limit}`;
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    // Cache for 24h — drug nomenclature does not change frequently
    next: { revalidate: 86400 },
  });
  if (!res.ok) {
    throw new Error(`RxNorm approximateTerm returned ${res.status}`);
  }
  const body = await res.json();
  const candidates = (body?.approximateGroup?.candidate || []) as Array<{
    rxcui?: string;
    name?: string;
    synonym?: string;
    tty?: string;
  }>;
  const out: NormalizedDrug[] = [];
  for (const c of candidates) {
    if (!c.rxcui) continue;
    out.push({
      rxcui: c.rxcui,
      name: c.name || c.synonym || "",
      synonym: c.synonym,
      tty: c.tty || "",
    });
  }
  return out;
}

/**
 * Get all properties for a given RxCUI — including active ingredients,
 * brand names, and dose form. Useful for populating a drug detail page.
 */
export async function getDrugProperties(rxcui: string): Promise<{
  rxcui: string;
  name?: string;
  activeIngredients: string[];
  brandNames: string[];
  doseForm?: string;
  tty?: string;
}> {
  const url = `${RXNORM_BASE}/rxcui/${encodeURIComponent(rxcui)}/allProperties.json?prop=names+codes+attributes`;
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    next: { revalidate: 86400 },
  });
  if (!res.ok) {
    throw new Error(`RxNorm allProperties returned ${res.status}`);
  }
  const body = await res.json();
  const propGroups = body?.propConceptGroup?.propConcept || [];
  const activeIngredients: string[] = [];
  const brandNames: string[] = [];
  let doseForm: string | undefined;
  let name: string | undefined;
  let tty: string | undefined;
  for (const p of propGroups) {
    if (p.propCategory === "NAMES" && p.propName === "Active Ingredient") {
      if (p.propValue && !activeIngredients.includes(p.propValue)) {
        activeIngredients.push(p.propValue);
      }
    }
    if (p.propCategory === "NAMES" && p.propName === "Brand Name") {
      if (p.propValue && !brandNames.includes(p.propValue)) {
        brandNames.push(p.propValue);
      }
    }
    if (p.propCategory === "ATTRIBUTES" && p.propName === "Dose Form") {
      doseForm = p.propValue;
    }
    if (p.propCategory === "ATTRIBUTES" && p.propName === "RxNorm Name") {
      name = p.propValue;
    }
    if (p.propCategory === "ATTRIBUTES" && p.propName === "TTY") {
      tty = p.propValue;
    }
  }
  return { rxcui, name, activeIngredients, brandNames, doseForm, tty };
}
