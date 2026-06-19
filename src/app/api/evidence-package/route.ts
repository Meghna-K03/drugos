import { NextRequest, NextResponse } from "next/server";
import { buildEvidencePackage, evidencePackageToMarkdown } from "@/lib/services/evidence-package";
import { requireAuth, badRequest, internalError, writeAuditLog } from "@/lib/api-helpers";
import { db } from "@/lib/db";

export async function POST(req: NextRequest) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;

  let body: { drug: string; disease: string; notes?: string; literatureLimit?: number; trialsLimit?: number };
  try {
    body = await req.json();
  } catch {
    return badRequest("Invalid JSON body");
  }
  if (!body.drug || !body.disease) {
    return badRequest("Both 'drug' and 'disease' fields are required");
  }
  try {
    const pkg = await buildEvidencePackage({
      drug: body.drug,
      disease: body.disease,
      literatureLimit: body.literatureLimit,
      trialsLimit: body.trialsLimit,
      notes: body.notes,
    });
    const markdown = evidencePackageToMarkdown(pkg);
    // Persist the package so the user can retrieve it later
    const record = await db.evidencePackage.create({
      data: {
        userId: auth.user.userId,
        drugName: pkg.drug,
        diseaseName: pkg.disease,
        title: `Evidence package: ${pkg.drug} for ${pkg.disease}`,
        summary: pkg.notes,
        payloadJson: JSON.stringify(pkg),
        status: "generated",
      },
    });
    await writeAuditLog({
      user: auth.user,
      action: "evidence_package_generated",
      resource: `evidence_package:${record.id}`,
      metadata: { drug: pkg.drug, disease: pkg.disease, literatureCount: pkg.literature.total, trialsCount: pkg.clinicalTrials.total },
    });
    return NextResponse.json({ id: record.id, package: pkg, markdown });
  } catch (e: any) {
    return internalError(`Evidence package build failed: ${e.message}`);
  }
}

export async function GET(req: NextRequest) {
  const auth = await requireAuth();
  if (auth.user === null) return auth.response;
  const id = req.nextUrl.searchParams.get("id");
  if (id) {
    const pkg = await db.evidencePackage.findUnique({ where: { id } });
    if (!pkg || pkg.userId !== auth.user.userId) {
      return NextResponse.json({ error: "not_found" }, { status: 404 });
    }
    return NextResponse.json({ id: pkg.id, package: JSON.parse(pkg.payloadJson), markdown: evidencePackageToMarkdown(JSON.parse(pkg.payloadJson)) });
  }
  const list = await db.evidencePackage.findMany({
    where: { userId: auth.user.userId },
    orderBy: { createdAt: "desc" },
    take: 50,
    select: { id: true, drugName: true, diseaseName: true, title: true, status: true, createdAt: true },
  });
  return NextResponse.json({ items: list });
}
