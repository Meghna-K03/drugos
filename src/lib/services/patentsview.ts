/**
 * USPTO PatentsView service — real patent data.
 *
 * Source: PatentsView API (https://search.patentsview.org/api/v1/patent/)
 * Maintainer: U.S. Patent and Trademark Office, via a research partnership.
 * License: Public domain (U.S. government work).
 *
 * This endpoint returns real patent grants from the USPTO. We use it to
 * surface patents that mention a drug or disease name in their title,
 * abstract, or claims — useful for IP due diligence.
 *
 * NOTE: PatentsView requires an API key. We read it from PATENTSVIEW_API_KEY
 * env var. If the key is missing we degrade gracefully with an empty result
 * set and a clear reason, rather than returning fake data.
 */

const PATENTSVIEW_BASE = "https://search.patentsview.org/api/v1/patent";

export interface PatentRecord {
  patentNumber: string;
  title: string;
  abstract: string;
  grantDate: string;
  inventors: string[];
  assignees: string[];
  cpcLabels: string[];
  url: string;
}

export interface PatentSearchResponse {
  total: number;
  patents: PatentRecord[];
  reason?: string;
}

export async function searchPatents(params: {
  query: string;
  limit?: number;
}): Promise<PatentSearchResponse> {
  const limit = Math.min(params.limit ?? 20, 100);
  const q = (params.query || "").trim();
  if (q.length < 2) return { total: 0, patents: [] };

  if (!process.env.PATENTSVIEW_API_KEY) {
    return {
      total: 0,
      patents: [],
      reason:
        "PATENTSVIEW_API_KEY not configured. Patent search requires a free PatentsView API key " +
        "(see https://patentsview.org/apis/keyrequest). No mock data is returned.",
    };
  }

  // PatentsView uses a JSON query language. We search the patent title,
  // abstract, and claims fields for the query term.
  const body = {
    q: {
      _text: {
        _in: [
          { _text_phrase: { patent_title: q } },
          { _text_phrase: { patent_abstract: q } },
        ],
      },
    },
    f: [
      "patent_number",
      "patent_title",
      "patent_abstract",
      "patent_date",
      "inventors.inventor_name",
      "assignees.assignee_organization",
      "cpc_current.cpc_subsection_id",
    ],
    o: { size: limit },
    s: [{ patent_date: "desc" }],
  };

  const res = await fetch(PATENTSVIEW_BASE, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": process.env.PATENTSVIEW_API_KEY,
    },
    body: JSON.stringify(body),
    next: { revalidate: 86400 },
  });

  if (!res.ok) {
    return {
      total: 0,
      patents: [],
      reason: `PatentsView returned ${res.status}. The patent search service may be temporarily unavailable.`,
    };
  }
  const data = await res.json();
  const patents: PatentRecord[] = (data?.patents || []).map((p: any) => ({
    patentNumber: p.patent_number,
    title: p.patent_title || "",
    abstract: (p.patent_abstract || "").slice(0, 500),
    grantDate: p.patent_date,
    inventors: (p.inventors || []).map((i: any) => i.inventor_name).filter(Boolean),
    assignees: (p.assignees || []).map((a: any) => a.assignee_organization).filter(Boolean),
    cpcLabels: (p.cpc_current || []).map((c: any) => c.cpc_subsection_id).filter(Boolean),
    url: p.patent_number ? `https://patents.google.com/patent/US${p.patent_number}` : "",
  }));
  return {
    total: data?.total_hits || patents.length,
    patents,
  };
}
