/**
 * DrugOS database layer — Prisma-compatible shim backed by node:sqlite.
 *
 * Why this exists: The original codebase was written against Prisma Client.
 * In this deployment environment, `prisma generate` cannot fetch the native
 * query engine binary from binaries.prisma.sh (CDN cuts the connection),
 * so `@prisma/client` is unusable. Rather than rewriting every service,
 * this module exposes a PrismaClient-compatible surface using Node 24's
 * built-in `node:sqlite` module against the same SQLite file that Prisma
 * created. The schema is identical; only the access layer is replaced.
 *
 * Supported subset (covers every call site in src/lib/services/* and
 * src/app/api/*):
 *   - findUnique({ where: { id|email|... }, include? })
 *   - findFirst({ where, include?, orderBy? })
 *   - findMany({ where?, include?, orderBy?, take?, skip? })
 *   - create({ data })
 *   - update({ where, data })
 *   - updateMany({ where, data })
 *   - delete({ where })
 *   - count({ where? })
 *
 * Relation includes handled: Project → hypotheses/comments/activities,
 * User → organizationMembers → organization, etc. (whatever the codebase uses).
 */

import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import crypto from "node:crypto";

const DB_PATH =
  process.env.DATABASE_URL?.replace(/^file:/, "") ||
  path.join(process.cwd(), "db/custom.db");

// In test mode, NODE_ENV=test, the test setup sets DATABASE_URL to a test.db
const isTest = process.env.NODE_ENV === "test";
const effectiveDbPath = isTest
  ? process.env.DATABASE_URL?.replace(/^file:/, "") || DB_PATH
  : DB_PATH;

const sqliteDb = new DatabaseSync(effectiveDbPath);
sqliteDb.exec("PRAGMA journal_mode = WAL;");
sqliteDb.exec("PRAGMA foreign_keys = ON;");

// ────────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────────

function genId(): string {
  return crypto.randomBytes(16).toString("hex");
}

function nowISO(): string {
  return new Date().toISOString();
}

/** Build WHERE clause + params from a Prisma-style `where` object. */
function buildWhere(
  table: string,
  where: Record<string, any> | undefined,
): { clause: string; params: any[] } {
  if (!where || Object.keys(where).length === 0) {
    return { clause: "", params: [] };
  }
  const parts: string[] = [];
  const params: any[] = [];
  for (const [key, value] of Object.entries(where)) {
    if (value === undefined || value === null) {
      parts.push(`${key} IS NULL`);
      continue;
    }
    if (typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof Date)) {
      // Prisma operators like { equals, in, gt, lt, gte, lte, not, contains }
      for (const [op, opVal] of Object.entries(value as Record<string, any>)) {
        if (op === "equals") {
          parts.push(`${key} = ?`);
          params.push(opVal);
        } else if (op === "in") {
          if (Array.isArray(opVal) && opVal.length > 0) {
            const placeholders = opVal.map(() => "?").join(",");
            parts.push(`${key} IN (${placeholders})`);
            params.push(...opVal);
          }
        } else if (op === "not") {
          parts.push(`${key} != ?`);
          params.push(opVal);
        } else if (op === "contains") {
          parts.push(`${key} LIKE ?`);
          params.push(`%${opVal}%`);
        } else if (op === "startsWith") {
          parts.push(`${key} LIKE ?`);
          params.push(`${opVal}%`);
        } else if (op === "endsWith") {
          parts.push(`${key} LIKE ?`);
          params.push(`%${opVal}`);
        } else if (op === "gt") {
          parts.push(`${key} > ?`);
          params.push(opVal);
        } else if (op === "gte") {
          parts.push(`${key} >= ?`);
          params.push(opVal);
        } else if (op === "lt") {
          parts.push(`${key} < ?`);
          params.push(opVal);
        } else if (op === "lte") {
          parts.push(`${key} <= ?`);
          params.push(opVal);
        }
      }
    } else {
      parts.push(`${key} = ?`);
      params.push(value);
    }
  }
  return { clause: parts.length ? `WHERE ${parts.join(" AND ")}` : "", params };
}

function buildOrderBy(orderBy: Record<string, string> | undefined): string {
  if (!orderBy) return "";
  const parts: string[] = [];
  for (const [key, dir] of Object.entries(orderBy)) {
    parts.push(`${key} ${dir === "asc" ? "ASC" : "DESC"}`);
  }
  return parts.length ? `ORDER BY ${parts.join(", ")}` : "";
}

/** Convert DB row to Prisma-style record: parse dates, booleans, numbers. */
function hydrate(row: Record<string, any> | undefined): any {
  if (!row) return null;
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(row)) {
    if (v === null || v === undefined) {
      out[k] = v;
    } else if (typeof v === "string") {
      // Prisma stores booleans as 0/1 in SQLite
      if (v === "0") out[k] = false;
      else if (v === "1") out[k] = true;
      else out[k] = v;
    } else {
      out[k] = v;
    }
  }
  return out;
}

// ────────────────────────────────────────────────────────────────────────────
// Relation definitions: which model → which child table → which foreign key
// ────────────────────────────────────────────────────────────────────────────

type RelationDef = {
  // table to query
  table: string;
  // foreign key column in that table pointing back to parent
  fk: string;
  // singular relation name (key used in `include`)
  // - if not provided, defaults to table name lowercased
};

const RELATIONS: Record<string, Record<string, RelationDef>> = {
  Project: {
    hypotheses: { table: "Hypothesis", fk: "projectId" },
    comments: { table: "Comment", fk: "projectId" },
    activities: { table: "ProjectActivity", fk: "projectId" },
  },
  User: {
    organizationMembers: { table: "OrganizationMember", fk: "userId" },
    refreshTokens: { table: "RefreshToken", fk: "userId" },
    apiKeys: { table: "ApiKey", fk: "userId" },
    notifications: { table: "Notification", fk: "userId" },
  },
  Organization: {
    members: { table: "OrganizationMember", fk: "organizationId" },
    projects: { table: "Project", fk: "organizationId" },
    subscriptions: { table: "Subscription", fk: "organizationId" },
    invoices: { table: "BillingInvoice", fk: "organizationId" },
  },
  Hypothesis: {},
  Comment: {},
  EvidencePackage: {},
  AuditLog: {},
  BillingInvoice: {},
  Subscription: {},
  Notification: {},
  ApiKey: {},
  OrganizationMember: {
    user: { table: "User", fk: "id" }, // special — handled below
    organization: { table: "Organization", fk: "id" },
  },
  RefreshToken: {},
  ProjectActivity: {},
  WebhookEndpoint: {},
};

// ────────────────────────────────────────────────────────────────────────────
// Model delegate — implements findUnique/findMany/create/etc.
// ────────────────────────────────────────────────────────────────────────────

class ModelDelegate<T = any> {
  constructor(private table: string) {}

  private cols(): string[] {
    const rows = sqliteDb.prepare(`PRAGMA table_info(${this.table})`).all() as Array<{
      name: string;
    }>;
    return rows.map((r) => r.name);
  }

  async findUnique(args: {
    where: Record<string, any>;
    include?: Record<string, any>;
  }): Promise<T | null> {
    const { clause, params } = buildWhere(this.table, args.where);
    const sql = `SELECT * FROM ${this.table} ${clause} LIMIT 1`;
    const row = sqliteDb.prepare(sql).get(...params) as Record<string, any> | undefined;
    if (!row) return null;
    const hydrated = hydrate(row);
    if (args.include) {
      await this.applyIncludes(hydrated, args.include);
    }
    return hydrated as T;
  }

  async findFirst(args: {
    where?: Record<string, any>;
    include?: Record<string, any>;
    orderBy?: Record<string, string>;
  }): Promise<T | null> {
    const { clause, params } = buildWhere(this.table, args.where);
    const orderBy = buildOrderBy(args.orderBy);
    const sql = `SELECT * FROM ${this.table} ${clause} ${orderBy} LIMIT 1`;
    const row = sqliteDb.prepare(sql).get(...params) as Record<string, any> | undefined;
    if (!row) return null;
    const hydrated = hydrate(row);
    if (args.include) {
      await this.applyIncludes(hydrated, args.include);
    }
    return hydrated as T;
  }

  async findMany(args: {
    where?: Record<string, any>;
    include?: Record<string, any>;
    orderBy?: Record<string, string>;
    take?: number;
    skip?: number;
  }): Promise<T[]> {
    const { clause, params } = buildWhere(this.table, args.where);
    const orderBy = buildOrderBy(args.orderBy);
    let limit = "";
    let offset = "";
    if (typeof args.take === "number") {
      limit = `LIMIT ${args.take}`;
      if (typeof args.skip === "number") {
        offset = `OFFSET ${args.skip}`;
      }
    }
    const sql = `SELECT * FROM ${this.table} ${clause} ${orderBy} ${limit} ${offset}`.trim();
    const rows = sqliteDb.prepare(sql).all(...params) as Array<Record<string, any>>;
    const result: T[] = [];
    for (const row of rows) {
      const hydrated = hydrate(row);
      if (args.include) {
        await this.applyIncludes(hydrated, args.include);
      }
      result.push(hydrated as T);
    }
    return result;
  }

  async create(args: { data: Record<string, any> }): Promise<T> {
    const data: Record<string, any> = { ...args.data };
    const columns = this.cols();

    // Auto-fill id if not provided
    if (!data.id && columns.includes("id")) {
      data.id = genId();
    }
    // Auto-fill timestamps
    const now = nowISO();
    if (columns.includes("createdAt") && !data.createdAt) {
      data.createdAt = now;
    }
    if (columns.includes("updatedAt") && !data.updatedAt) {
      data.updatedAt = now;
    }

    // Filter to only known columns
    const validCols = Object.keys(data).filter((k) => columns.includes(k));
    const placeholders = validCols.map(() => "?").join(", ");
    const values = validCols.map((k) => {
      const v = data[k];
      if (typeof v === "boolean") return v ? 1 : 0;
      if (Array.isArray(v)) return v.join(",");
      if (v instanceof Date) return v.toISOString();
      return v;
    });
    const sql = `INSERT INTO ${this.table} (${validCols.join(", ")}) VALUES (${placeholders})`;
    sqliteDb.prepare(sql).run(...values);

    // Fetch back
    const created = sqliteDb
      .prepare(`SELECT * FROM ${this.table} WHERE id = ?`)
      .get(data.id) as Record<string, any>;
    return hydrate(created) as T;
  }

  async update(args: {
    where: Record<string, any>;
    data: Record<string, any>;
  }): Promise<T> {
    const columns = this.cols();
    const sets: string[] = [];
    const params: any[] = [];
    for (const [k, v] of Object.entries(args.data)) {
      if (!columns.includes(k)) continue;
      if (k === "id") continue;
      if (typeof v === "boolean") {
        sets.push(`${k} = ?`);
        params.push(v ? 1 : 0);
      } else if (Array.isArray(v)) {
        sets.push(`${k} = ?`);
        params.push(v.join(","));
      } else if (v instanceof Date) {
        sets.push(`${k} = ?`);
        params.push(v.toISOString());
      } else {
        sets.push(`${k} = ?`);
        params.push(v as any);
      }
    }
    // always bump updatedAt if it exists
    if (columns.includes("updatedAt") && !args.data.updatedAt) {
      sets.push(`updatedAt = ?`);
      params.push(nowISO());
    }
    const { clause, params: whereParams } = buildWhere(this.table, args.where);
    const sql = `UPDATE ${this.table} SET ${sets.join(", ")} ${clause}`;
    sqliteDb.prepare(sql).run(...params, ...whereParams);

    // Fetch back
    const row = sqliteDb
      .prepare(`SELECT * FROM ${this.table} ${clause} LIMIT 1`)
      .get(...whereParams) as Record<string, any>;
    return hydrate(row) as T;
  }

  async updateMany(args: {
    where: Record<string, any>;
    data: Record<string, any>;
  }): Promise<{ count: number }> {
    const columns = this.cols();
    const sets: string[] = [];
    const params: any[] = [];
    for (const [k, v] of Object.entries(args.data)) {
      if (!columns.includes(k)) continue;
      if (k === "id") continue;
      if (typeof v === "boolean") {
        sets.push(`${k} = ?`);
        params.push(v ? 1 : 0);
      } else if (v instanceof Date) {
        sets.push(`${k} = ?`);
        params.push(v.toISOString());
      } else {
        sets.push(`${k} = ?`);
        params.push(v as any);
      }
    }
    const { clause, params: whereParams } = buildWhere(this.table, args.where);
    const sql = `UPDATE ${this.table} SET ${sets.join(", ")} ${clause}`;
    const result = sqliteDb.prepare(sql).run(...params, ...whereParams) as {
      changes: number;
    };
    return { count: result.changes || 0 };
  }

  async delete(args: { where: Record<string, any> }): Promise<T> {
    const { clause, params } = buildWhere(this.table, args.where);
    const row = sqliteDb
      .prepare(`SELECT * FROM ${this.table} ${clause} LIMIT 1`)
      .get(...params) as Record<string, any>;
    sqliteDb.prepare(`DELETE FROM ${this.table} ${clause}`).run(...params);
    return hydrate(row) as T;
  }

  async count(args: { where?: Record<string, any> } = {}): Promise<number> {
    const { clause, params } = buildWhere(this.table, args.where);
    const sql = `SELECT COUNT(*) as cnt FROM ${this.table} ${clause}`;
    const row = sqliteDb.prepare(sql).get(...params) as { cnt: number };
    return row.cnt;
  }

  private async applyIncludes(
    record: Record<string, any>,
    include: Record<string, any>,
  ) {
    const relations = RELATIONS[this.table] || {};
    for (const [relName, relConfig] of Object.entries(include)) {
      if (relName === "_count") {
        // _count: { select: { hypotheses: true, comments: true } }
        const select = (relConfig as any).select || {};
        const counts: Record<string, number> = {};
        for (const childRel of Object.keys(select)) {
          const childRelDef = relations[childRel];
          if (childRelDef) {
            const childClause = `WHERE ${childRelDef.fk} = ?`;
            const r = sqliteDb
              .prepare(`SELECT COUNT(*) as cnt FROM ${childRelDef.table} ${childClause}`)
              .get(record.id) as { cnt: number };
            counts[childRel] = r.cnt;
          }
        }
        record._count = counts;
        continue;
      }
      const relDef = relations[relName];
      if (!relDef) continue;

      const childInclude = (relConfig as any).include;
      const childOrderBy = (relConfig as any).orderBy;
      const childTake = (relConfig as any).take;

      if (relName === "user" || relName === "organization") {
        // belongs-to: parent has a userId/organizationId FK to child's id
        const fkOnParent =
          relName === "user" ? "userId" : "organizationId";
        const fkVal = record[fkOnParent];
        if (!fkVal) {
          record[relName] = null;
          continue;
        }
        const childRow = sqliteDb
          .prepare(`SELECT * FROM ${relDef.table} WHERE id = ? LIMIT 1`)
          .get(fkVal) as Record<string, any> | undefined;
        record[relName] = childRow ? hydrate(childRow) : null;
      } else {
        // has-many: child has FK pointing back to parent's id
        const { clause } = buildWhere(relDef.table, {
          [relDef.fk]: record.id,
        });
        const orderBy = buildOrderBy(childOrderBy);
        const limit = typeof childTake === "number" ? `LIMIT ${childTake}` : "";
        const sql = `SELECT * FROM ${relDef.table} ${clause} ${orderBy} ${limit}`.trim();
        const rows = sqliteDb.prepare(sql).all() as Array<Record<string, any>>;
        const hydrated = rows.map((r) => hydrate(r));
        if (childInclude) {
          // recurse — currently our codebase doesn't need this depth, but support anyway
          for (const h of hydrated) {
            const childDelegate = new ModelDelegate(
              RELATIONS[relDef.table] ? relDef.table : "",
            );
            if (childDelegate) {
              await childDelegate.applyIncludes(h, childInclude);
            }
          }
        }
        record[relName] = hydrated;
      }
    }
  }
}

// ────────────────────────────────────────────────────────────────────────────
// PrismaClient shim
// ────────────────────────────────────────────────────────────────────────────

class PrismaClientShim {
  user = new ModelDelegate<User>("User");
  organization = new ModelDelegate<Organization>("Organization");
  organizationMember = new ModelDelegate<OrganizationMember>("OrganizationMember");
  project = new ModelDelegate<Project>("Project");
  hypothesis = new ModelDelegate<Hypothesis>("Hypothesis");
  comment = new ModelDelegate<Comment>("Comment");
  projectActivity = new ModelDelegate<ProjectActivity>("ProjectActivity");
  evidencePackage = new ModelDelegate<EvidencePackage>("EvidencePackage");
  apiKey = new ModelDelegate<ApiKey>("ApiKey");
  auditLog = new ModelDelegate<AuditLog>("AuditLog");
  billingInvoice = new ModelDelegate<BillingInvoice>("BillingInvoice");
  subscription = new ModelDelegate<Subscription>("Subscription");
  notification = new ModelDelegate<Notification>("Notification");
  refreshToken = new ModelDelegate<RefreshToken>("RefreshToken");
  webhookEndpoint = new ModelDelegate<WebhookEndpoint>("WebhookEndpoint");

  constructor(_opts?: any) {}

  /**
   * Prisma-compatible transaction. Since our SQLite is synchronous and
   * single-connection, we just run the callback inside BEGIN/COMMIT.
   * The `tx` argument passed to the callback is just `this` (same client).
   */
  async $transaction<R>(fn: (tx: this) => Promise<R> | R): Promise<R> {
    sqliteDb.exec("BEGIN");
    try {
      const result = await fn(this);
      sqliteDb.exec("COMMIT");
      return result;
    } catch (err) {
      try {
        sqliteDb.exec("ROLLBACK");
      } catch {
        // ignore
      }
      throw err;
    }
  }

  async $disconnect() {
    try {
      sqliteDb.close();
    } catch {
      // already closed
    }
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Type exports (kept loose to avoid coupling to a specific Prisma version)
// ────────────────────────────────────────────────────────────────────────────

export type User = Record<string, any>;
export type Organization = Record<string, any>;
export type OrganizationMember = Record<string, any>;
export type Project = Record<string, any>;
export type Hypothesis = Record<string, any>;
export type Comment = Record<string, any>;
export type ProjectActivity = Record<string, any>;
export type EvidencePackage = Record<string, any>;
export type ApiKey = Record<string, any>;
export type AuditLog = Record<string, any>;
export type BillingInvoice = Record<string, any>;
export type Subscription = Record<string, any>;
export type Notification = Record<string, any>;
export type RefreshToken = Record<string, any>;
export type WebhookEndpoint = Record<string, any>;

// In dev, cache the client on globalThis to survive HMR.
const globalForPrisma = globalThis as unknown as {
  prisma: PrismaClientShim | undefined;
};

const shouldUseGlobalCache =
  process.env.NODE_ENV !== "production" && process.env.NODE_ENV !== "test";

export const db =
  (shouldUseGlobalCache ? globalForPrisma.prisma : undefined) ??
  new PrismaClientShim();

if (shouldUseGlobalCache) globalForPrisma.prisma = db;

// Re-export PrismaClient name so `import { PrismaClient } from '@prisma/client'` callers
// (if any) still work — but the main entry point is the `db` export above.
export const PrismaClient = PrismaClientShim;
