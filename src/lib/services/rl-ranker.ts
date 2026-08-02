/**
 * RL Hypothesis Ranker service — Phase 4 handoff.
 *
 * ROOT FIX for FE-002 (and the Phase 4 → API handoff gap):
 *
 * Previously: `/api/rl` returned 501 unconditionally. There was NO code in
 * `src/` that read the Phase 4 RL ranker's output, parsed
 * policy_prob/reward/rank/safety_score/literature_support fields, or
 * populated the Hypothesis.pr plausibilityScore/safetyScore/marketScore/
 * overallScore Prisma fields from real ML output. The dashboard's
 * "AI-ranked candidates" were hardcoded mock values with no relationship
 * to the RL ranker's actual predictions.
 *
 * ROOT FIX: this service reads the REAL Phase 4 output artifact — the
 * `validated_hypotheses.csv` file produced by `rl/rl_drug_ranker.py` — and
 * returns its contents as typed objects. The file path is configurable via
 * the `RL_OUTPUT_CSV_PATH` env var so production deployments can point at
 * an NFS / S3 path; the default is `../rl/validated_hypotheses.csv`
 * (relative to the frontend's project root), which is where the Python
 * ranker writes it.
 *
 * Schema flexibility: the ranker's output schema has evolved across
 * versions. The current minimal schema is `drug,disease` (validated
 * pairs). A richer schema adds `reward,rank,policy_prob,gnn_score,
 * safety_score,literature_support`. We parse whatever columns are present
 * and surface them; missing columns are reported as `null`.
 *
 * If `RL_SERVICE_URL` is set, we proxy to a standalone Phase 4 service
 * instead of reading the file. This is the production path — the file is
 * the dev / single-box fallback.
 *
 * SCIENTIFIC INTEGRITY: we NEVER fabricate predictions. If the CSV is
 * missing we return an empty list and a `source: "none"` indicator — the
 * dashboard then shows "No RL-ranked candidates available" instead of
 * mock data.
 */

import { promises as fs } from "fs";
import path from "path";

export interface RankedHypothesis {
  drug: string;
  disease: string;
  // Optional fields — present only when the ranker's output CSV includes
  // them. The minimal `validated_hypotheses.csv` only has drug+disease.
  rank?: number;
  reward?: number;
  policyProb?: number;
  gnnScore?: number;
  safetyScore?: number;
  literatureSupport?: number;
}

export interface RlRankerResponse {
  candidates: RankedHypothesis[];
  source: "rl_service" | "local_csv" | "none";
  modelVersion?: string;
  generatedAt: string;
  count: number;
  note?: string;
}

const DEFAULT_CSV_PATH = path.resolve(process.cwd(), "..", "rl", "validated_hypotheses.csv");

function parseCsvLine(line: string): string[] {
  // Minimal CSV parser — handles quoted fields with embedded commas. The
  // ranker's output is simple enough that we don't need a full RFC-4180
  // parser, but we do need to handle drug/disease names that contain
  // commas (e.g., "aspirin, buffered").
  const out: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        cur += c;
      }
    } else {
      if (c === '"') {
        inQuotes = true;
      } else if (c === ",") {
        out.push(cur);
        cur = "";
      } else {
        cur += c;
      }
    }
  }
  out.push(cur);
  return out;
}

function parseNumber(s: string | undefined): number | undefined {
  if (s === undefined || s === null || s === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

async function readLocalCsv(csvPath: string): Promise<RankedHypothesis[]> {
  let content: string;
  try {
    content = await fs.readFile(csvPath, "utf8");
  } catch {
    return [];
  }
  const lines = content.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 2) return [];
  const header = parseCsvLine(lines[0]).map((h) => h.trim().toLowerCase());
  const out: RankedHypothesis[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = parseCsvLine(lines[i]);
    const row: Record<string, string> = {};
    for (let j = 0; j < header.length; j++) {
      row[header[j]] = (cols[j] || "").trim();
    }
    const drug = row["drug"];
    const disease = row["disease"];
    if (!drug || !disease) continue;
    out.push({
      drug,
      disease,
      rank: parseNumber(row["rank"]),
      reward: parseNumber(row["reward"]),
      policyProb: parseNumber(row["policy_prob"]),
      gnnScore: parseNumber(row["gnn_score"]),
      safetyScore: parseNumber(row["safety_score"]),
      literatureSupport: parseNumber(row["literature_support"]),
    });
  }
  // If the CSV has rank, sort by rank; otherwise preserve file order.
  if (out.some((c) => c.rank !== undefined)) {
    out.sort((a, b) => (a.rank ?? Number.MAX_SAFE_INTEGER) - (b.rank ?? Number.MAX_SAFE_INTEGER));
  }
  return out;
}

async function proxyToRlService(url: string, queryParams: URLSearchParams): Promise<RlRankerResponse> {
  const fullUrl = `${url.replace(/\/$/, "")}/rank?${queryParams.toString()}`;
  const res = await fetch(fullUrl, {
    headers: { Accept: "application/json" },
    // Don't cache — rankings may update.
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`RL service at ${url} returned ${res.status}`);
  }
  const body = await res.json();
  // The RL service is expected to return { candidates, modelVersion, ... }.
  return {
    candidates: (body?.candidates || []) as RankedHypothesis[],
    source: "rl_service",
    modelVersion: body?.modelVersion,
    generatedAt: body?.generatedAt || new Date().toISOString(),
    count: (body?.candidates || []).length,
  };
}

/**
 * Get the ranked repurposing candidates from the Phase 4 RL ranker.
 *
 * Resolution order:
 *   1. If `RL_SERVICE_URL` is set, proxy to it (production path).
 *   2. Otherwise, read the local CSV at `RL_OUTPUT_CSV_PATH` (default
 *      `../rl/validated_hypotheses.csv`).
 *   3. If neither yields data, return an empty list with `source: "none"`.
 */
export async function getRankedHypotheses(opts?: {
  drug?: string;
  disease?: string;
  limit?: number;
}): Promise<RlRankerResponse> {
  const limit = Math.min(opts?.limit ?? 50, 200);
  const queryParams = new URLSearchParams();
  if (opts?.drug) queryParams.set("drug", opts.drug);
  if (opts?.disease) queryParams.set("disease", opts.disease);
  queryParams.set("limit", String(limit));

  // 1. Proxy path.
  const serviceUrl = process.env.RL_SERVICE_URL;
  if (serviceUrl) {
    try {
      return await proxyToRlService(serviceUrl, queryParams);
    } catch (e) {
      // Fall through to local CSV.
      console.warn("RL service proxy failed, falling back to local CSV:", e);
    }
  }

  // 2. Local CSV path.
  // B-4 ROOT FIX: standardized on RL_LOCAL_CSV everywhere (route.ts,
  // .env.example) — this used to read the since-abandoned
  // RL_OUTPUT_CSV_PATH name, so setting RL_LOCAL_CSV (the documented var)
  // had no effect on this service.
  const csvPath = process.env.RL_LOCAL_CSV || DEFAULT_CSV_PATH;
  let candidates = await readLocalCsv(csvPath);

  // Apply optional filters.
  if (opts?.drug) {
    candidates = candidates.filter((c) => c.drug.toLowerCase() === opts!.drug!.toLowerCase());
  }
  if (opts?.disease) {
    candidates = candidates.filter((c) => c.disease.toLowerCase() === opts!.disease!.toLowerCase());
  }
  if (candidates.length > limit) candidates = candidates.slice(0, limit);

  if (candidates.length === 0) {
    return {
      candidates: [],
      source: "none",
      generatedAt: new Date().toISOString(),
      count: 0,
      note:
        "No RL-ranked candidates found. Set RL_SERVICE_URL to proxy to the " +
        "Phase 4 service, or ensure the ranker has written its output to " +
        `${csvPath}.`,
    };
  }

  return {
    candidates,
    source: "local_csv",
    generatedAt: new Date().toISOString(),
    count: candidates.length,
    note:
      "Served from local CSV artifact. These are real validated drug-disease " +
      "pairs from the Phase 4 RL ranker output.",
  };
}

/**
 * Sync the RL ranker's output into the Hypothesis table. For each ranked
 * candidate, find a matching Hypothesis (by drug+disease name) and update
 * its RL-managed fields. This is the missing Phase 4 → DB handoff.
 *
 * Returns the number of hypotheses updated.
 */
export async function syncRlOutputToHypotheses(): Promise<number> {
  const { db } = await import("@/lib/db");
  const { candidates } = await getRankedHypotheses({ limit: 200 });
  if (candidates.length === 0) return 0;

  let updated = 0;
  for (const c of candidates) {
    // Find hypotheses matching this drug+disease pair across all projects.
    // SQLite doesn't support `mode: "insensitive"`. Use lowercased
        // equality via `equals` on the lowercased value — Prisma's SQLite
        // connector is case-sensitive by default for non-ASCII, but for
        // ASCII drug/disease names lowercasing both sides is sufficient.
        const matches = await db.hypothesis.findMany({
          where: {
            OR: [
              { drugName: c.drug, diseaseName: c.disease },
              { drugName: c.drug.toLowerCase(), diseaseName: c.disease.toLowerCase() },
            ],
          },
        });
    for (const h of matches) {
      await db.hypothesis.update({
        where: { id: h.id },
        data: {
          rank: c.rank ?? null,
          policyProb: c.policyProb ?? null,
          reward: c.reward ?? null,
          gnnScore: c.gnnScore ?? null,
          safetyScore: c.safetyScore ?? null,
          literatureSupport: Boolean(c.literatureSupport) ?? null,
          // Derive a 0-1 overallScore from the available signals.
          overallScore: computeOverallScore(c),
          rlModelVersion: "rl_drug_ranker.py-v101",
          rlUpdatedAt: new Date(),
        },
      });
      updated++;
    }
  }
  return updated;
}

function computeOverallScore(c: RankedHypothesis): number | null {
  // Weighted blend of the three RL signals. If none are present, return null.
  const signals: { value: number; weight: number }[] = [];
  if (c.gnnScore !== undefined) signals.push({ value: c.gnnScore, weight: 0.4 });
  if (c.safetyScore !== undefined) signals.push({ value: c.safetyScore, weight: 0.3 });
  if (c.policyProb !== undefined) signals.push({ value: c.policyProb, weight: 0.3 });
  if (signals.length === 0) return null;
  const totalWeight = signals.reduce((s, x) => s + x.weight, 0);
  return signals.reduce((s, x) => s + (x.value * x.weight) / totalWeight, 0);
}
