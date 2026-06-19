/**
 * Disease search via MeSH (Medical Subject Headings) — the NLM's controlled
 * vocabulary thesaurus used for indexing PubMed.
 *
 * Source: NLM MeSH REST API (https://id.nlm.nih.gov/mesh/lookup)
 * License: Public domain.
 *
 * We use MeSH because it is the single authoritative vocabulary for diseases
 * used across biomedical research. Every PubMed article is indexed against
 * MeSH descriptors.
 */

const MESH_BASE = "https://id.nlm.nih.gov/mesh/lookup";

export interface MeshDescriptor {
  descriptorUi: string; // e.g., D000001
  name: string;
  scopeNote?: string;
  allowDuplicates: boolean;
  treeNumber: string[]; // e.g., ["C01.001"] — placement in MeSH tree
}

/**
 * Lookup MeSH descriptors matching a free-text disease query.
 * Returns the canonical descriptor record(s) used to index that disease.
 */
export async function searchDiseasesByName(query: string, limit = 10): Promise<MeshDescriptor[]> {
  const q = (query || "").trim();
  if (q.length < 2) return [];
  const url = `${MESH_BASE}/descriptor?label=${encodeURIComponent(q)}&limit=${limit}`;
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    next: { revalidate: 86400 * 30 }, // MeSH updates ~weekly
  });
  if (!res.ok) {
    throw new Error(`MeSH descriptor lookup returned ${res.status}`);
  }
  const uris: string[] = await res.json();
  if (uris.length === 0) return [];
  const descriptors: MeshDescriptor[] = [];
  for (const uri of uris.slice(0, limit)) {
    const descriptorUi = uri.split("/").pop() || "";
    if (!descriptorUi) continue;
    // Fetch the name and scope note
    const nameRes = await fetch(`${MESH_BASE}/descriptor?resource=${encodeURIComponent(uri)}`, {
      headers: { Accept: "application/json" },
      next: { revalidate: 86400 * 30 },
    });
    if (!nameRes.ok) continue;
    const name = (await nameRes.text()).trim().replace(/^"|"$/g, "");
    let scopeNote: string | undefined;
    try {
      const snRes = await fetch(`${MESH_BASE}/scopeNote?resource=${encodeURIComponent(uri)}`, {
        headers: { Accept: "application/json" },
        next: { revalidate: 86400 * 30 },
      });
      if (snRes.ok) scopeNote = (await snRes.text()).trim().replace(/^"|"$/g, "");
    } catch {}
    let treeNumber: string[] = [];
    try {
      const tnRes = await fetch(`${MESH_BASE}/treeNumber?resource=${encodeURIComponent(uri)}`, {
        headers: { Accept: "application/json" },
        next: { revalidate: 86400 * 30 },
      });
      if (tnRes.ok) treeNumber = await tnRes.json();
    } catch {}
    descriptors.push({
      descriptorUi,
      name,
      scopeNote,
      allowDuplicates: false,
      treeNumber,
    });
  }
  return descriptors;
}
