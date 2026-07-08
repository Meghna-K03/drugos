"""
FIX-C (Phase 1 Infrastructure) — verification tests.

Covers three fixes:
  * C-5 — migration runner must NOT pick up ``*_rollback.sql`` sidecars.
  * C-6 — ``apache-airflow`` must be declared in ``requirements.txt`` so
    DAG files are importable outside Docker and ``test_dag_structure.py``
    no longer needs ``pytest.importorskip("airflow")``.
  * C-9 — ``_validate_security()`` must HONESTLY report that DisGeNET and
    DrugBank will crash on run when their prerequisites are missing, and
    ``health_check()`` must return ``{"healthy": False, "issues": [...]}``
    in that case.

These tests are designed to run WITHOUT airflow installed — they only
verify that the fix is in place (file content + health_check behavior).
The DAG-import smoke test lives in ``test_dag_structure.py`` (which, post
FIX-C6, no longer skips when airflow is absent — it will simply fail if
airflow is not installed, which is the desired behaviour).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


REPO_ROOT = PROJECT_ROOT.parent
REQ_FILE = REPO_ROOT / "requirements.txt"
PHASE1_REQ_FILE = PROJECT_ROOT / "requirements.txt"
MIGRATIONS_RUNNER = (
    PROJECT_ROOT / "database" / "migrations" / "run_migrations.py"
)
MIGRATIONS_DIR = PROJECT_ROOT / "database" / "migrations"


# ============================================================================
# C-5: migration glob must exclude ``*_rollback.sql`` files
# ============================================================================

class TestMigrationGlobExcludesRollback:
    """FIX-C5: ``MIGRATIONS_DIR.glob("*.sql")`` sites must filter out
    ``*_rollback.sql`` sidecars — otherwise on PostgreSQL the rollback
    files would ``DROP TABLE IF EXISTS drugs CASCADE; ...`` and destroy
    the staging schema on every fresh install, and on SQLite they abort
    with "You can only execute one statement at a time".
    """

    def test_migration_glob_excludes_rollback(self) -> None:
        """Every ``glob("*.sql")`` call in run_migrations.py must be
        accompanied by a ``_rollback.sql`` filter within 3 lines.
        """
        content = MIGRATIONS_RUNNER.read_text(encoding="utf-8")
        lines = content.split("\n")

        glob_lines = [
            i for i, line in enumerate(lines)
            if "glob(" in line and "*.sql" in line
        ]
        # We expect 6 sites per the FIX-C5 contract.
        assert len(glob_lines) >= 6, (
            f"Expected at least 6 glob('*.sql') sites in run_migrations.py, "
            f"found {len(glob_lines)}"
        )

        unfiltered: list[str] = []
        for idx in glob_lines:
            ctx = "\n".join(lines[max(0, idx - 3): idx + 4])
            if "_rollback" not in ctx:
                unfiltered.append(
                    f"line {idx + 1}: {lines[idx].strip()!r}"
                )
        assert not unfiltered, (
            "Unfiltered glob('*.sql') sites found in run_migrations.py "
            "(each must have a `_rollback.sql` filter within 3 lines):\n  - "
            + "\n  - ".join(unfiltered)
        )

    def test_get_sql_migration_files_skips_rollback(self) -> None:
        """``get_sql_migration_files()`` must NOT return any file whose
        name ends with ``_rollback.sql``.
        """
        # Import lazily so a syntax error in the runner surfaces clearly.
        from database.migrations.run_migrations import get_sql_migration_files

        files = get_sql_migration_files()
        names = [f.name for f in files]
        rollbacks = [n for n in names if n.endswith("_rollback.sql")]
        assert not rollbacks, (
            f"get_sql_migration_files() returned rollback files: {rollbacks}"
        )

    def test_migrations_directory_contains_rollback_sidecars(self) -> None:
        """Sanity check: the migrations directory actually contains
        rollback sidecars (otherwise the fix would be untestable).
        """
        rollbacks = sorted(MIGRATIONS_DIR.glob("*_rollback.sql"))
        assert len(rollbacks) >= 6, (
            f"Expected >=6 _rollback.sql sidecars in {MIGRATIONS_DIR}, "
            f"found {len(rollbacks)}: {[f.name for f in rollbacks]}"
        )

    def test_only_non_rollback_migrations_are_listed(self) -> None:
        """The list returned by ``get_sql_migration_files()`` must be
        exactly the non-rollback ``.sql`` files in the migrations dir.
        """
        from database.migrations.run_migrations import get_sql_migration_files

        actual = {f.name for f in get_sql_migration_files()}
        expected = {
            f.name
            for f in MIGRATIONS_DIR.glob("*.sql")
            if not f.name.endswith("_rollback.sql")
        }
        assert actual == expected, (
            f"get_sql_migration_files() mismatch.\n"
            f"  actual:   {sorted(actual)}\n"
            f"  expected: {sorted(expected)}"
        )


# ============================================================================
# C-6: apache-airflow must be declared in requirements.txt
# ============================================================================

class TestAirflowInRequirements:
    """FIX-C6: ``apache-airflow`` must be a real dependency — the 8 DAG
    files in ``phase1/dags/`` do ``from airflow.decorators import dag,
    task`` at module top level. Previously the comment in
    ``phase1/requirements.txt`` said "provided by Docker base image" and
    CI ``pytest.importorskip("airflow")`` silently skipped every DAG
    structure test.
    """

    def test_airflow_in_root_requirements(self) -> None:
        """``requirements.txt`` (repo root) must list ``apache-airflow``."""
        text = REQ_FILE.read_text(encoding="utf-8")
        lines = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        airflow_lines = [ln for ln in lines if "apache-airflow" in ln.lower()]
        assert airflow_lines, (
            f"apache-airflow not found in {REQ_FILE}. DAGs are un-importable "
            f"without it."
        )
        # Sanity: must pin a version >= 2.8.0
        first = airflow_lines[0]
        assert "2.8" in first or "2.9" in first or "3." in first, (
            f"apache-airflow pin looks wrong: {first!r}"
        )

    def test_airflow_in_phase1_requirements(self) -> None:
        """``phase1/requirements.txt`` must NOT still have the
        "provided by Docker base image" comment — it must declare
        ``apache-airflow`` explicitly.
        """
        text = PHASE1_REQ_FILE.read_text(encoding="utf-8")
        assert "provided by Docker base image" not in text, (
            "phase1/requirements.txt still says Airflow is 'provided by "
            "Docker base image' — DAGs are un-importable outside Docker."
        )
        assert "apache-airflow" in text.lower(), (
            f"apache-airflow not declared in {PHASE1_REQ_FILE}"
        )

    def test_dag_structure_test_no_longer_skips_on_missing_airflow(self) -> None:
        """``test_dag_structure.py`` must NOT contain an active
        ``pytest.importorskip("airflow")`` call (only allowed in comments).
        """
        dag_test = PROJECT_ROOT / "tests" / "test_dag_structure.py"
        text = dag_test.read_text(encoding="utf-8")

        # Find any active (non-comment, non-string) importorskip("airflow")
        # call. We accept the literal in comments/docstrings.
        active_lines = []
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "importorskip" in stripped and "airflow" in stripped:
                active_lines.append((i, stripped))
        assert not active_lines, (
            f"test_dag_structure.py still has an active importorskip("
            f"'airflow') call (would silently skip DAG tests):\n"
            + "\n".join(f"  L{ln}: {s}" for ln, s in active_lines)
        )


# ============================================================================
# C-9: health_check must be HONEST about DisGeNET/DrugBank prereqs
# ============================================================================

class TestHealthCheckHonestAboutPrereqs:
    """FIX-C9: when ``DISGENET_API_KEY`` and ``DRUGBANK_XML_PATH`` are not
    set, ``health_check()`` must return ``healthy=False`` and the issues
    list must honestly state the pipelines WILL CRASH (not "work at a
    lower rate limit" / "gracefully skip").
    """

    @pytest.fixture(autouse=True)
    def _clear_prereq_env(self, monkeypatch):
        """Force DisGeNET + DrugBank prereqs to be missing for every test
        in this class, regardless of the host environment.
        """
        monkeypatch.delenv("DISGENET_API_KEY", raising=False)
        monkeypatch.delenv("DRUGBANK_XML_PATH", raising=False)

    def test_health_check_returns_healthy_false_when_prereqs_missing(self) -> None:
        from pipelines import health_check

        result = health_check()
        assert result.get("healthy") is False, (
            f"Expected healthy=False when DisGeNET/DrugBank prereqs missing; "
            f"got healthy={result.get('healthy')!r}, status={result.get('status')!r}"
        )

    def test_health_check_returns_issues_list(self) -> None:
        from pipelines import health_check

        result = health_check()
        issues = result.get("issues", [])
        assert isinstance(issues, list), (
            f"Expected issues to be a list, got {type(issues).__name__}"
        )
        assert len(issues) >= 2, (
            f"Expected >=2 issues (DisGeNET + DrugBank), got {issues}"
        )

    def test_health_check_disgenet_issue_says_will_crash(self) -> None:
        from pipelines import health_check

        result = health_check()
        issues = result.get("issues", [])
        disgenet_issues = [
            s for s in issues if "DISGENET" in s.upper()
        ]
        assert disgenet_issues, (
            f"No DisGeNET issue in {issues}"
        )
        joined = " ".join(disgenet_issues).upper()
        # Must NOT claim it will "work" / "skip gracefully".
        assert "WILL CRASH" in joined, (
            f"DisGeNET issue must say 'WILL CRASH', got: {disgenet_issues}"
        )
        assert "LOWER RATE LIMIT" not in joined, (
            f"DisGeNET issue still says 'lower rate limit' (the old lie): "
            f"{disgenet_issues}"
        )

    def test_health_check_drugbank_issue_says_will_crash(self) -> None:
        from pipelines import health_check

        result = health_check()
        issues = result.get("issues", [])
        drugbank_issues = [
            s for s in issues if "DRUGBANK" in s.upper()
        ]
        assert drugbank_issues, (
            f"No DrugBank issue in {issues}"
        )
        joined = " ".join(drugbank_issues).upper()
        assert "WILL CRASH" in joined, (
            f"DrugBank issue must say 'WILL CRASH', got: {drugbank_issues}"
        )
        assert "GRACEFULLY SKIP" not in joined, (
            f"DrugBank issue still says 'gracefully skip' (the old lie): "
            f"{drugbank_issues}"
        )

    def test_validate_security_uses_error_severity(self) -> None:
        """``_validate_security()`` must mark DisGeNET + DrugBank
        missing-prereq checks with severity ``ERROR`` (was ``WARNING``),
        and ``overall`` must be ``INSECURE``.
        """
        from pipelines import _validate_security

        report = _validate_security()
        checks = {c["check"]: c for c in report.get("checks", [])}

        disgenet = checks.get("disgenet_api_key")
        assert disgenet is not None, "disgenet_api_key check missing"
        assert disgenet["severity"] == "ERROR", (
            f"DisGeNET severity must be ERROR, got {disgenet['severity']!r}"
        )

        drugbank = checks.get("drugbank_xml_path")
        assert drugbank is not None, "drugbank_xml_path check missing"
        assert drugbank["severity"] == "ERROR", (
            f"DrugBank severity must be ERROR, got {drugbank['severity']!r}"
        )

        assert report["overall"] == "INSECURE", (
            f"overall must be INSECURE when DisGeNET/DrugBank prereqs are "
            f"missing, got {report['overall']!r}"
        )
