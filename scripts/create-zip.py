#!/usr/bin/env python3
"""
Create a complete ZIP file of the DrugOS codebase (frontend + backend + tests).

Excludes:
  - node_modules/
  - .next/ (build cache)
  - db/*.db (binary database files — will be recreated via `prisma db push`)
  - dev.log, server.log (logs)
  - test-results/, playwright-report/ (test artifacts)
  - .git/ (version control history)
  - upload/ (user-provided reference docs)
  - download/ (generated outputs)
  - skills/ (skill definitions — not part of the application)
  - examples/ (framework examples — not part of the application)
  - agent-ctx/ (agent context files)
  - __pycache__/ (Python cache)
  - *.pyc, *.log
"""

import os
import zipfile
import sys
from pathlib import Path

ROOT = Path("/home/z/my-project")
OUTPUT = Path("/home/z/my-project/download/drugos_final.zip")

EXCLUDE_DIRS = {
    "node_modules",
    ".next",
    ".git",
    "upload",
    "download",
    "skills",
    "examples",
    "agent-ctx",
    "__pycache__",
    "test-results",
    "playwright-report",
    ".turbo",
    ".cache",
    "playwright-report",
    "screenshots",
    ".zscripts",        # build infra scripts, not part of the application
    "mini-services",    # unrelated microservice scaffolding
    "tool-results",     # agent intermediate outputs
}

EXCLUDE_FILES = {
    "dev.log",
    "server.log",
    "bun.lock",
    "package-lock.json",
    "yarn.lock",
    "tsconfig.tsbuildinfo",
    # Noisy legacy screen-generation scripts (not part of the application)
    "gen_all.py",
    "gen_all_screens.py",
    "gen_batch1.py",
    "gen_engine.py",
    "gen_final.py",
    "gen_screens_part2.py",
    "generate_screens.py",
    "generate-demo-guide.js",
    # Zipped artifacts (avoid recursion)
    "drugos_complete.zip",
}

EXCLUDE_PATTERNS = [
    ".log",
    ".db-journal",
    ".pyc",
    ".zip",
]

INCLUDE_PATHS = [
    # All source files
    "src",
    # Prisma schema & migrations
    "prisma",
    # Tests
    "tests",
    # Scripts (run-all-tests.sh, run-integration-tests.js, run-e2e-tests.js, create-zip.py)
    "scripts",
    # Config files
    "package.json",
    "tsconfig.json",
    "tailwind.config.ts",
    "postcss.config.mjs",
    "next.config.ts",
    "eslint.config.mjs",
    "components.json",
    "jest.config.js",
    "playwright.config.ts",
    "Caddyfile",
    ".env.example",
    ".gitignore",
    "README.md",
    "SETUP.md",
    "worklog.md",
    "next-env.d.ts",
    # Public assets
    "public",
]


def should_exclude(path: Path) -> bool:
    """Return True if this path should be excluded from the ZIP."""
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if not parts:
        return True
    # Exclude any path under an excluded directory
    for excluded in EXCLUDE_DIRS:
        if excluded in parts:
            return True
    # Exclude specific files
    if path.name in EXCLUDE_FILES:
        return True
    # Exclude by extension
    for pat in EXCLUDE_PATTERNS:
        if path.name.endswith(pat):
            return True
    # Exclude hidden files except a small allowlist. Crucially, .env (which
    # contains the JWT_SECRET and other secrets) is NOT in the allowlist and
    # therefore is excluded from the ZIP.
    if path.name.startswith(".") and path.name not in {
        ".env.example",
        ".env",
        ".gitignore",
        ".eslintrc.json",
        ".editorconfig",
    }:
        return True
    return False


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()

    file_count = 0
    total_bytes = 0

    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Walk the entire project and include matching files
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # Modify dirnames in-place to skip excluded dirs
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            current_dir = Path(dirpath)
            for filename in filenames:
                full_path = current_dir / filename
                if should_exclude(full_path):
                    continue
                # Compute the path inside the ZIP (relative to ROOT, with a top-level "drugos" folder)
                rel_path = full_path.relative_to(ROOT)
                zip_path = Path("drugos") / rel_path
                try:
                    zf.write(full_path, str(zip_path))
                    file_count += 1
                    total_bytes += full_path.stat().st_size
                except (OSError, PermissionError) as e:
                    print(f"WARN: skipped {full_path}: {e}", file=sys.stderr)

    print(f"Created {OUTPUT}")
    print(f"  Files: {file_count}")
    print(f"  Source size: {total_bytes / 1024 / 1024:.2f} MB")
    print(f"  ZIP size: {OUTPUT.stat().st_size / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
