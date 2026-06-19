/**
 * Test env setup — runs BEFORE any test file is loaded.
 * Sets DATABASE_URL to a separate test database so we never touch the dev DB.
 */

const path = require("path");

process.env.NODE_ENV = "test";
process.env.DATABASE_URL = `file:${path.join(process.cwd(), "db", "test.db")}`;
process.env.JWT_SECRET = "test-secret-only-not-for-production-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";
