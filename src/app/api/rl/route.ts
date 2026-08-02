import { NextRequest, NextResponse } from "next/server";
import { checkRlAvailability } from "@/lib/services/ml-stubs";
import { db } from "@/lib/db";
import { writeAuditLog, requireAuth, badRequest, internalError } from "@/lib/api-helpers";
import { signAccessToken } from "@/lib/auth/server";
import { parse } from "csv-parse/sync";

/**
 * POST /api/rl
 * Body: { drug?: string, disease?: string, limit?: number }
 *
 * FE-002 ROOT FIX: The previous code unconditionally returned 501 — even
 * when RL_SERVICE_URL was set. There was NO code anywhere in src/ that
 * read the Phase 4 RL ranker's output CSV. A grep for
 * validated_hypotheses, policy_prob, gnn_score, rl_drug_ranker,
 * candidates.csv returned ZERO matches. The candidate fields (drug,
 * disease, reward, rank, policy_prob, safety_score, literature_support)
 * appeared NOWHERE in the Next.js codebase.
 *
 * The Phase 4 → API handoff was non-existent. The RL ranker's predictions
 * never reached the dashboard.
 *
 * ROOT FIX: This endpoint now implements TWO real integration paths:
 *
 *   1. HTTP proxy (production): If RL_SERVICE_URL is set, we POST the
 *      query to the standalone RL service (a FastAPI app wrapping
 *      rl_drug_ranker.py) and stream back the ranked candidates as JSON.
 *      The RL service is the source of truth — we never fabricate.
 *
 *   2. Local CSV (dev/demo): If RL_LOCAL_CSV is set (path to a CSV file
 *      produced by `python rl/rl_drug_ranker.py`), we parse it in-process
 *      and return the ranked candidates. This lets the dashboard show
 *      REAL RL output during development without standing up the FastAPI
 *      service. The CSV columns are documented in rl/rl_drug_ranker.py
 *      (drug, disease, gnn_score, safety_score, market_score, reward,
 *      rank, policy_prob, literature_support, etc.).
 *
 * In BOTH cases, the response is mapped to the Hypothesis schema fields
 * (plausibilityScore, safetyScore, marketScore, overallScore) so the
 * dashboard can render real RL predictions instead of mock data.
 *
 * If NEITHER env var is set, we return 503 service_not_deployed — we NEVER
 * fabricate predictions. A pharma company might act on a fake "high
 * confidence" prediction — that's a patient-safety violation.
 */
export async function POST(req: NextRequest) {
  // FE-001 (related): the dashboard must call this endpoint, so we require
  // auth. An unauthenticated caller could enumerate RL predictions.
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;

  let body: { drug?: string; disease?: string; limit?: number };
  try {
    body = await req.json();
  } catch {
    body = {};
  }
  const drug = (body.drug || "").trim();
  const disease = (body.disease || "").trim();
  const limit = Math.min(body.limit ?? 50, 200);

  const availability = checkRlAvailability();
  if (!availability.available) {
    // Fall through to local CSV check below — maybe the dev set RL_LOCAL_CSV
    // without setting RL_SERVICE_URL. That's a valid dev configuration.
    if (!process.env.RL_LOCAL_CSV) {
      return NextResponse.json(
        {
          error: "service_not_deployed",
          service: availability.service,
          description: availability.description,
          reason: availability.reason,
          documentation:
            "See Phase 4 of the build plan (RL-Driven Hypothesis Ranking). " +
            "Set RL_SERVICE_URL to proxy to the standalone RL service, or " +
            "RL_LOCAL_CSV to read a local output CSV in dev mode.",
        },
        { status: 503 }
      );
    }
  }

  // Path 1: HTTP proxy to the standalone RL service.
  const rlServiceUrl = process.env.RL_SERVICE_URL;
  if (rlServiceUrl) {
    try {
      const accessToken = signAccessToken(auth.user);
      const upstream = await fetch(`${rlServiceUrl.replace(/\/$/, "")}/rank`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ drug, disease, limit }),
      });
      if (!upstream.ok) {
        const text = await upstream.text();
        return NextResponse.json(
          {
            error: "rl_service_error",
            message: `RL service returned ${upstream.status}: ${text.slice(0, 500)}`,
          },
          { status: 502 }
        );
      }
      const data = await upstream.json();
      // Persist each candidate as a Hypothesis row so the user can
      // reference them later. We upsert by (drugName, diseaseName) within
      // the user's first project (or skip persistence if no project).
      await persistRlCandidates(auth.user.userId, data.candidates || []);
      await writeAuditLog({
        user: auth.user,
        action: "rl_query",
        resource: `rl:${drug || "*"}:${disease || "*"}`,
        metadata: { count: (data.candidates || []).length, source: "proxy" },
      });
      return NextResponse.json(data);
    } catch (e: any) {
      return internalError(`RL service proxy failed: ${e.message}`);
    }
  }

  // Path 2: Local CSV (dev/demo mode).
  const csvPath = process.env.RL_LOCAL_CSV;
  if (csvPath) {
    try {
      const candidates = await parseRlCsv(csvPath, { drug, disease, limit });
      await persistRlCandidates(auth.user.userId, candidates);
      await writeAuditLog({
        user: auth.user,
        action: "rl_query",
        resource: `rl:${drug || "*"}:${disease || "*"}`,
        metadata: { count: candidates.length, source: "local_csv" },
      });
      return NextResponse.json({
        candidates,
        source: "local_csv",
        csvPath,
        total: candidates.length,
      });
    } catch (e: any) {
      return internalError(`RL CSV parse failed: ${e.message}`);
    }
  }

  // Should not reach here (availability check above handles this), but
  // belt-and-suspenders.
  return NextResponse.json(
    { error: "service_not_deployed", message: "RL service is not configured." },
    { status: 503 }
  );
}

export async function GET() {
  // FE-001: dashboard calls GET for a default top-N list.
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const csvPath = process.env.RL_LOCAL_CSV;
  if (csvPath) {
    try {
      const candidates = await parseRlCsv(csvPath, { limit: 50 });
      return NextResponse.json({ candidates, source: "local_csv", total: candidates.length });
    } catch (e: any) {
      return internalError(`RL CSV parse failed: ${e.message}`);
    }
  }
  return NextResponse.json(
    { error: "service_not_deployed", message: "Set RL_SERVICE_URL or RL_LOCAL_CSV." },
    { status: 503 }
  );
}

// ---------------------------------------------------------------------------
// CSV parser — reads the RL ranker's output CSV and maps it to the
// Hypothesis schema fields that the dashboard expects.
// ---------------------------------------------------------------------------

interface RlCandidate {
  drug: string;
  disease: string;
  reward: number;
  rank: number;
  policyProb: number;
  plausibilityScore: number; // from gnn_score
  safetyScore: number;
  marketScore: number;
  overallScore: number; // weighted composite
  literatureSupport: boolean;
  isKnownPositive: boolean;
  confidence: number;
  pathwayScore: number;
  unmetNeedScore: number;
  efficacyScore: number;
  admeScore: number;
}

async function parseRlCsv(
  path: string,
  filter: { drug?: string; disease?: string; limit?: number }
): Promise<RlCandidate[]> {
  const fs = await import("fs/promises");
  const content = await fs.readFile(path, "utf8");
  const records = parse(content, {
    columns: true,
    skip_empty_lines: true,
    trim: true,
  }) as Record<string, string>[];

  let candidates: RlCandidate[] = records.map((r, idx) => {
    const gnn = num(r, "gnn_score", 0);
    const safety = num(r, "safety_score", 0);
    const market = num(r, "market_score", 0);
    const reward = num(r, "reward", 0);
    const rank = num(r, "rank", idx + 1);
    const policyProb = num(r, "policy_prob", 0);
    const confidence = num(r, "confidence", 0);
    const pathwayScore = num(r, "pathway_score", 0);
    const unmetNeedScore = num(r, "unmet_need_score", 0);
    const efficacyScore = num(r, "efficacy_score", 0);
    const admeScore = num(r, "adme_score", 0);
    // Composite overall score — weighted sum of the three dimensions the
    // project docx specifies (plausibility, safety, market). Weights match
    // the RL reward function's default weights in rl_drug_ranker.py.
    const overall = 0.4 * gnn + 0.3 * safety + 0.3 * market;
    return {
      drug: r.drug || "",
      disease: r.disease || "",
      reward,
      rank,
      policyProb,
      plausibilityScore: gnn,
      safetyScore: safety,
      marketScore: market,
      overallScore: overall,
      literatureSupport: bool(r, "literature_support"),
      isKnownPositive: bool(r, "is_known_positive"),
      confidence,
      pathwayScore,
      unmetNeedScore,
      efficacyScore,
      admeScore,
    };
  });

  // Filter by optional drug/disease query.
  if (filter.drug) {
    const q = filter.drug.toLowerCase();
    candidates = candidates.filter((c) => c.drug.toLowerCase().includes(q));
  }
  if (filter.disease) {
    const q = filter.disease.toLowerCase();
    candidates = candidates.filter((c) => c.disease.toLowerCase().includes(q));
  }

  // Sort by overall score (desc) — the RL agent's ranking.
  candidates.sort((a, b) => b.overallScore - a.overallScore);

  // Reassign rank after sort.
  candidates = candidates.map((c, i) => ({ ...c, rank: i + 1 }));

  if (filter.limit) {
    candidates = candidates.slice(0, filter.limit);
  }
  return candidates;
}

function num(r: Record<string, string>, key: string, fallback: number): number {
  const v = r[key];
  if (v === undefined || v === null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function bool(r: Record<string, string>, key: string): boolean {
  const v = (r[key] || "").toLowerCase();
  return v === "1" || v === "true" || v === "yes";
}

// ---------------------------------------------------------------------------
// Persistence — store RL candidates as Hypothesis rows so the user can
// reference them in projects. Best-effort; failures are logged not thrown.
// ---------------------------------------------------------------------------

async function persistRlCandidates(userId: string, candidates: RlCandidate[]): Promise<void> {
  if (candidates.length === 0) return;
  try {
    // Find or create a default project for RL hypotheses.
    // We look for the user's first project; if none exists, skip persistence
    // (the candidates are still returned in the response).
    const membership = await db.organizationMember.findFirst({
      where: { userId },
      orderBy: { joinedAt: "asc" },
    });
    if (!membership) return;
    const project = await db.project.findFirst({
      where: { organizationId: membership.organizationId },
      orderBy: { createdAt: "asc" },
    });
    if (!project) return;

    // Upsert each candidate as a Hypothesis. We use upsert keyed on
    // (projectId, drugName, diseaseName) so re-running the RL agent
    // updates scores rather than creating duplicates.
    for (const c of candidates.slice(0, 50)) {
      const existing = await db.hypothesis.findFirst({
        where: {
          projectId: project.id,
          drugName: c.drug,
          diseaseName: c.disease,
        },
      });
      if (existing) {
        await db.hypothesis.update({
          where: { id: existing.id },
          data: {
            plausibilityScore: c.plausibilityScore,
            safetyScore: c.safetyScore,
            marketScore: c.marketScore,
            overallScore: c.overallScore,
            status: "validated",
          },
        });
      } else {
        await db.hypothesis.create({
          data: {
            projectId: project.id,
            title: `${c.drug} for ${c.disease}`,
            drugName: c.drug,
            diseaseName: c.disease,
            status: "validated",
            plausibilityScore: c.plausibilityScore,
            safetyScore: c.safetyScore,
            marketScore: c.marketScore,
            overallScore: c.overallScore,
            createdById: userId,
            notes: `RL rank ${c.rank}, reward ${c.reward.toFixed(4)}, policy_prob ${c.policyProb.toFixed(4)}, literature_support ${c.literatureSupport}`,
          },
        });
      }
    }
  } catch (e) {
    // Persistence is best-effort — the response still returns the candidates.
    console.error("persistRlCandidates failed:", e);
  }
}
