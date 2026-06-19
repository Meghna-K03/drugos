/**
 * PubMed literature search — real biomedical publication data.
 *
 * Source: NCBI E-utilities (https://www.ncbi.nlm.nih.gov/books/NBK25501/)
 * Maintainer: U.S. National Center for Biotechnology Information (NCBI).
 * License: Public domain (U.S. government work).
 *
 * This is the SAME PubMed that researchers use at pubmed.ncbi.nlm.nih.gov.
 * Every result returned here is a real, peer-reviewed publication indexed
 * by MEDLINE.
 *
 * Rate limits: 3 req/sec without an API key, 10 req/sec with one.
 * We pass NCBI_API_KEY env var when available.
 */

const EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils";

/**
 * fetch with retry on 429 (rate limited). NCBI allows 3 req/sec without an
 * API key, 10 req/sec with one. When we hit 429, we retry with exponential
 * backoff honoring the Retry-After header.
 */
async function fetchWithRetry(url: URL | string, init?: RequestInit, maxRetries = 3): Promise<Response> {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const res = await fetch(url, init);
    if (res.status !== 429) return res;
    const retryAfter = res.headers.get("Retry-After");
    const delayMs = retryAfter ? parseInt(retryAfter, 10) * 1000 : Math.pow(2, attempt) * 1000 + 500;
    await new Promise((r) => setTimeout(r, delayMs));
  }
  // Final attempt — return whatever response we get
  return fetch(url, init);
}

export interface PubMedArticle {
  pmid: string;
  title: string;
  journal: string;
  authors: string[];
  pubDate: string;
  abstract?: string;
  doi?: string;
  url: string;
}

export interface PubMedSearchResponse {
  total: number;
  articles: PubMedArticle[];
}

/**
 * Search PubMed for articles matching the query. The query syntax supports
 * MeSH terms, boolean operators, and field qualifiers (e.g., "aspirin[Title]").
 */
export async function searchPubMed(params: {
  query: string;
  limit?: number;
  offset?: number;
  sort?: "relevance" | "pub_date" | "first_author";
  yearFrom?: number;
  yearTo?: number;
}): Promise<PubMedSearchResponse> {
  const limit = Math.min(params.limit ?? 15, 100);
  const start = params.offset ?? 0;

  // Build the search query — append date filters if provided.
  let term = params.query;
  if (params.yearFrom || params.yearTo) {
    const from = params.yearFrom || 1900;
    const to = params.yearTo || new Date().getFullYear();
    term += ` AND ("${from}"[Publication Date] : "${to}"[Publication Date])`;
  }

  // Sort parameter mapping
  const sortMap: Record<string, string> = {
    relevance: "",
    pub_date: "pub_date",
    first_author: "first_author",
  };
  const sort = sortMap[params.sort || "relevance"] || "";

  // Step 1: esearch — get PMIDs matching the query.
  const esearchUrl = new URL(`${EUTILS_BASE}/esearch.fcgi`);
  esearchUrl.searchParams.set("db", "pubmed");
  esearchUrl.searchParams.set("term", term);
  esearchUrl.searchParams.set("retmax", String(limit));
  esearchUrl.searchParams.set("retstart", String(start));
  esearchUrl.searchParams.set("retmode", "json");
  if (sort) esearchUrl.searchParams.set("sort", sort);
  if (process.env.NCBI_API_KEY) esearchUrl.searchParams.set("api_key", process.env.NCBI_API_KEY);

  const esearchRes = await fetchWithRetry(esearchUrl, {
    headers: { Accept: "application/json" },
    next: { revalidate: 3600 },
  });
  if (!esearchRes.ok) {
    throw new Error(`NCBI esearch returned ${esearchRes.status}`);
  }
  const esearchBody = await esearchRes.json();
  const pmids: string[] = esearchBody?.esearchresult?.idlist || [];
  const total = parseInt(esearchBody?.esearchresult?.count || "0", 10);

  if (pmids.length === 0) {
    return { total: 0, articles: [] };
  }

  // Step 2: esummary — get article metadata for each PMID.
  const esummaryUrl = new URL(`${EUTILS_BASE}/esummary.fcgi`);
  esummaryUrl.searchParams.set("db", "pubmed");
  esummaryUrl.searchParams.set("id", pmids.join(","));
  esummaryUrl.searchParams.set("retmode", "json");
  if (process.env.NCBI_API_KEY) esummaryUrl.searchParams.set("api_key", process.env.NCBI_API_KEY);

  const esummaryRes = await fetchWithRetry(esummaryUrl, {
    headers: { Accept: "application/json" },
    next: { revalidate: 3600 },
  });
  if (!esummaryRes.ok) {
    throw new Error(`NCBI esummary returned ${esummaryRes.status}`);
  }
  const esummaryBody = await esummaryRes.json();
  const result = esummaryBody?.result || {};
  const uids: string[] = result.uids || [];

  const articles: PubMedArticle[] = [];
  for (const uid of uids) {
    const a = result[uid];
    if (!a) continue;
    const authors: string[] = (a.authors || []).map((au: any) => au.name).filter(Boolean);
    const pubDate = [a.pubdate, a.epubdate].filter(Boolean).join(" / ");
    const article: PubMedArticle = {
      pmid: uid,
      title: a.title || "",
      journal: a.fulljournalname || a.source || "",
      authors,
      pubDate,
      url: `https://pubmed.ncbi.nlm.nih.gov/${uid}/`,
    };
    // Try to grab DOI from articleids list
    for (const idObj of a.articleids || []) {
      if (idObj.idtype === "doi" && idObj.value) {
        article.doi = idObj.value;
        break;
      }
    }
    articles.push(article);
  }

  return { total, articles };
}

/**
 * Fetch the full abstract for a single PMID. We use efetch with rettype=abstract
 * which returns the abstract text as plain text.
 */
export async function getAbstract(pmid: string): Promise<string> {
  const url = new URL(`${EUTILS_BASE}/efetch.fcgi`);
  url.searchParams.set("db", "pubmed");
  url.searchParams.set("id", pmid);
  url.searchParams.set("rettype", "abstract");
  url.searchParams.set("retmode", "text");
  if (process.env.NCBI_API_KEY) url.searchParams.set("api_key", process.env.NCBI_API_KEY);
  const res = await fetch(url, { next: { revalidate: 86400 } });
  if (!res.ok) throw new Error(`NCBI efetch returned ${res.status}`);
  const text = await res.text();
  return text.trim();
}
