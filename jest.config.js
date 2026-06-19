/**
 * Backend test suite — Jest config.
 */
module.exports = {
  preset: "ts-jest",
  testEnvironment: "node",
  testMatch: ["<rootDir>/tests/api/**/*.test.ts", "<rootDir>/src/lib/services/__tests__/**/*.test.ts"],
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  transform: {
    "^.+\\.tsx?$": ["ts-jest", { tsconfig: { target: "ES2022", module: "CommonJS", esModuleInterop: true, skipLibCheck: true, resolveJsonModule: true } }],
  },
  transformIgnorePatterns: ["/node_modules/(?!(next|@prisma/client|@radix-ui|lucide-react))"],
  setupFiles: ["<rootDir>/tests/api/env.ts"],
  setupFilesAfterEnv: ["<rootDir>/tests/api/setup.ts"],
  testTimeout: 60000,
  collectCoverageFrom: [
    "src/lib/services/**/*.ts",
    "src/lib/auth/**/*.ts",
    "src/app/api/**/*.ts",
  ],
};
