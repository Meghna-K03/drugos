#!/usr/bin/env node
/**
 * Database initialization script.
 *
 * Creates db/custom.db with the DrugOS schema if it doesn't exist,
 * using only Node's built-in `node:sqlite` module (no Prisma needed).
 *
 * Usage:  node scripts/init-db.js
 */

const { DatabaseSync } = require("node:sqlite");
const path = require("node:path");
const fs = require("node:fs");
const crypto = require("node:crypto");

const DB_DIR = path.join(__dirname, "..", "db");
const DB_PATH = path.join(DB_DIR, "custom.db");

if (!fs.existsSync(DB_DIR)) {
  fs.mkdirSync(DB_DIR, { recursive: true });
}

const db = new DatabaseSync(DB_PATH);
db.exec("PRAGMA journal_mode = WAL;");
db.exec("PRAGMA foreign_keys = ON;");

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS Organization (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  plan TEXT NOT NULL DEFAULT 'free',
  status TEXT NOT NULL DEFAULT 'active',
  seats INTEGER NOT NULL DEFAULT 1,
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS User (
  id TEXT PRIMARY KEY NOT NULL,
  email TEXT NOT NULL UNIQUE,
  passwordHash TEXT NOT NULL,
  name TEXT,
  role TEXT NOT NULL DEFAULT 'researcher',
  status TEXT NOT NULL DEFAULT 'active',
  mfaSecret TEXT,
  mfaEnabled BOOLEAN NOT NULL DEFAULT 0,
  emailVerified BOOLEAN NOT NULL DEFAULT 0,
  academicVerified BOOLEAN NOT NULL DEFAULT 0,
  lastLoginAt DATETIME,
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS OrganizationMember (
  id TEXT PRIMARY KEY NOT NULL,
  userId TEXT NOT NULL,
  organizationId TEXT NOT NULL,
  role TEXT NOT NULL,
  invitedBy TEXT,
  joinedAt DATETIME NOT NULL,
  FOREIGN KEY (userId) REFERENCES User(id),
  FOREIGN KEY (organizationId) REFERENCES Organization(id)
);

CREATE TABLE IF NOT EXISTS RefreshToken (
  id TEXT PRIMARY KEY NOT NULL,
  userId TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  expiresAt DATETIME NOT NULL,
  revokedAt DATETIME,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS Project (
  id TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  visibility TEXT NOT NULL DEFAULT 'private',
  ownerId TEXT NOT NULL,
  organizationId TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL,
  FOREIGN KEY (ownerId) REFERENCES User(id),
  FOREIGN KEY (organizationId) REFERENCES Organization(id)
);

CREATE TABLE IF NOT EXISTS Hypothesis (
  id TEXT PRIMARY KEY NOT NULL,
  projectId TEXT NOT NULL,
  title TEXT NOT NULL,
  drugName TEXT NOT NULL,
  diseaseName TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  plausibilityScore REAL,
  safetyScore REAL,
  marketScore REAL,
  overallScore REAL,
  notes TEXT,
  createdById TEXT NOT NULL,
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL,
  FOREIGN KEY (projectId) REFERENCES Project(id),
  FOREIGN KEY (createdById) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS Comment (
  id TEXT PRIMARY KEY NOT NULL,
  projectId TEXT NOT NULL,
  userId TEXT,
  authorName TEXT NOT NULL,
  body TEXT NOT NULL,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (projectId) REFERENCES Project(id),
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS ProjectActivity (
  id TEXT PRIMARY KEY NOT NULL,
  projectId TEXT NOT NULL,
  type TEXT NOT NULL,
  actorName TEXT NOT NULL,
  summary TEXT NOT NULL,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (projectId) REFERENCES Project(id)
);

CREATE TABLE IF NOT EXISTS EvidencePackage (
  id TEXT PRIMARY KEY NOT NULL,
  projectId TEXT,
  hypothesisId TEXT,
  userId TEXT,
  drugName TEXT NOT NULL,
  diseaseName TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  payloadJson TEXT NOT NULL,
  pdfPath TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS ApiKey (
  id TEXT PRIMARY KEY NOT NULL,
  organizationId TEXT NOT NULL,
  userId TEXT NOT NULL,
  name TEXT NOT NULL,
  hashedKey TEXT NOT NULL,
  prefix TEXT NOT NULL,
  lastUsedAt DATETIME,
  revokedAt DATETIME,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (organizationId) REFERENCES Organization(id),
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS AuditLog (
  id TEXT PRIMARY KEY NOT NULL,
  userId TEXT,
  actorName TEXT NOT NULL,
  action TEXT NOT NULL,
  resource TEXT,
  ip TEXT,
  userAgent TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS Subscription (
  id TEXT PRIMARY KEY NOT NULL,
  organizationId TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',
  status TEXT NOT NULL DEFAULT 'active',
  seats INTEGER NOT NULL DEFAULT 1,
  currentPeriodStart DATETIME NOT NULL,
  currentPeriodEnd DATETIME NOT NULL,
  cancelAtPeriodEnd BOOLEAN NOT NULL DEFAULT 0,
  createdAt DATETIME NOT NULL,
  updatedAt DATETIME NOT NULL,
  FOREIGN KEY (organizationId) REFERENCES Organization(id)
);

CREATE TABLE IF NOT EXISTS BillingInvoice (
  id TEXT PRIMARY KEY NOT NULL,
  organizationId TEXT NOT NULL,
  userId TEXT,
  number TEXT NOT NULL,
  amountCents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  status TEXT NOT NULL DEFAULT 'pending',
  periodStart DATETIME NOT NULL,
  periodEnd DATETIME NOT NULL,
  dueDate DATETIME NOT NULL,
  pdfUrl TEXT,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (organizationId) REFERENCES Organization(id),
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS Notification (
  id TEXT PRIMARY KEY NOT NULL,
  userId TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  readAt DATETIME,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (userId) REFERENCES User(id)
);

CREATE TABLE IF NOT EXISTS WebhookEndpoint (
  id TEXT PRIMARY KEY NOT NULL,
  organizationId TEXT NOT NULL,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT 1,
  lastTriggeredAt DATETIME,
  createdAt DATETIME NOT NULL,
  FOREIGN KEY (organizationId) REFERENCES Organization(id)
);
`;

db.exec(SCHEMA_SQL);

console.log("✓ Database initialized at", DB_PATH);

// Show table counts
const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all();
console.log("✓ Tables:", tables.map((t) => t.name).join(", "));
for (const t of tables) {
  const c = db.prepare(`SELECT COUNT(*) as n FROM ${t.name}`).get();
  console.log(`  ${t.name}: ${c.n} rows`);
}

db.close();
