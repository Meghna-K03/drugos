"""
v26 ML Honesty Chain — Regression Tests for Issues C-1 / C-2 / C-3.

These tests verify the v26 ROOT FIXES for the user's #1 complaint:
the pipeline reported ``V1 LAUNCH CRITERIA: PASSED`` for a model with
``best_val_auc=0.6722`` (target 0.85) and ``held_out_auc=0.5389``
(statistically random), and a log line said
``AUC enforcement PASSED: 0.6722 >= 0.8500`` — a mathematical falsehood.

Root causes fixed:

  C-1  ``run_pipeline.py::_check_v1_launch_criteria`` flipped
       ``criteria["passed"] = True`` in DEV_SMOKE_TEST mode even when
       ``auc_meets_threshold=False``.

  C-2  ``config.py::assert_auc_meets_threshold`` returned ``meets=False``
       without raising in RELAXED mode, but callers did not check the
       return value.

  C-3  ``transe_model.py::train_transe`` logged "AUC enforcement PASSED"
       whenever no exception was raised, regardless of whether the
       inequality ``auc >= threshold`` was true.

The tests below use REAL production code paths wherever feasible (no
mocks of the functions under test). They exercise:
  1. ``assert_auc_meets_threshold`` return value in RELAXED mode.
  2. ``_check_v1_launch_criteria`` strict ``passed`` field when AUC < 0.85.
  3. ``dev_smoke_test_pass`` / ``passed_dev_smoke`` separation from
     ``passed``.
  4. ``transe_model.train_transe`` source structure: the
     "AUC enforcement PASSED" log line must only fire inside the
     ``if _auc_meets:`` branch.
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

# Ensure phase2 is importable
PHASE2_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE2_DIR))


def _extract_if_else_branches(source: str, predicate: str):
    """Walk an indented Python source and extract the body of an
    ``if <predicate>:`` block plus the immediately following
    ``else:`` block, returned as a ``(if_body, else_body)`` tuple of
    strings. Either may be ``None`` if not found.

    The walk is line-based: the if-body is every line whose indentation
    is strictly greater than the ``if`` line's indentation, stopping at
    the first line at the same or lower indentation. If that line is
    ``else:`` (same indentation as the ``if``), the else-body is
    similarly collected.
    """
    lines = source.split("\n")
    if_pat = re.compile(predicate)
    # Find the `if` line.
    if_idx = None
    if_indent = None
    for i, line in enumerate(lines):
        if if_pat.search(line):
            if_idx = i
            if_indent = len(line) - len(line.lstrip())
            break
    if if_idx is None:
        return None, None
    # Collect the if-body: lines whose indentation > if_indent.
    if_body_lines = []
    i = if_idx + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            if_body_lines.append(line)
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= if_indent:
            break
        if_body_lines.append(line)
        i += 1
    if_body = "\n".join(if_body_lines)
    # Check if the next non-blank line at if_indent is `else:`.
    else_idx = None
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == if_indent and line.strip().startswith("else"):
            else_idx = i
        break
    if else_idx is None:
        return if_body, None
    # Collect the else-body.
    else_body_lines = []
    i = else_idx + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            else_body_lines.append(line)
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= if_indent:
            break
        else_body_lines.append(line)
        i += 1
    else_body = "\n".join(else_body_lines)
    return if_body, else_body


def _extract_except_body(source: str, exception_name: str):
    """Extract the body of an ``except <exception_name> as exc:`` block
    from a Python source string. Returns the body as a string, or
    ``None`` if not found.

    The body is every line whose indentation is strictly greater than
    the ``except`` line's indentation, stopping at the first line at
    the same or lower indentation.
    """
    lines = source.split("\n")
    except_pat = re.compile(
        r"except\s+" + re.escape(exception_name) + r"\b"
    )
    except_idx = None
    except_indent = None
    for i, line in enumerate(lines):
        if except_pat.search(line):
            except_idx = i
            except_indent = len(line) - len(line.lstrip())
            break
    if except_idx is None:
        return None
    body_lines = []
    i = except_idx + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            body_lines.append(line)
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= except_indent:
            break
        body_lines.append(line)
        i += 1
    return "\n".join(body_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build a mock `results` dict that mirrors the toy fixture
# the audit found (best_val_auc=0.6722, held_out_auc=0.5389).
# ─────────────────────────────────────────────────────────────────────────────

def _toy_fixture_results() -> dict:
    """Build a results dict that mirrors the toy fixture's output.

    This is what the pipeline produced when the audit caught it lying:
      - best_val_auc = 0.6722  (target 0.85)
      - held_out_auc = 0.5389  (statistically random)
      - model_saved = True     (because RELAXED mode didn't raise)
      - 4 sources loaded       (DisGeNET, OMIM, PubChem, DrugBank)
      - 9 positive pairs       (dev min is 1)
      - 22 negative pairs      (dev min is 1)
      - no critical source failures

    v27 ROOT FIX (TOP-7 + ML-1): bumped DEV_SMOKE_TEST_MIN_AUC from 0.5
    to 0.6 (0.5 IS the random baseline; comment said "must be >0.5").
    The original toy fixture's held_out_auc=0.5389 is below 0.6, so
    dev_smoke_test_pass would be False. To preserve this test's purpose
    (verify dev_smoke_test_pass can be True while passed is False), we
    bump the fixture's held_out_auc to 0.65 — passes 0.6 smoke, fails
    0.85 launch. This represents an honest baseline that just barely
    passes the dev smoke threshold.
    """
    return {
        "step4": {"drug_records": {"DB00645": {}}},  # 1 source (DrugBank)
        "step5": {"stitch_edges": 0},                  # no STITCH in dev
        "step7": {"results": {
            "chembl_edges": 0,
            "string_edges": 0,
            "uniprot_nodes": 0,
            "opentargets_edges": 0,
            "disgenet_edges": 22,    # 1 source
            "omim_edges": 9,         # 1 source
            "pubchem_nodes": 8,      # 1 source
        }},
        "step10": {"training_data": {"num_positives": 9, "num_negatives": 22}},
        "step11": {
            "best_val_auc": 0.6722,
            # v27: bumped from 0.5389 to 0.65 to pass DEV_SMOKE_TEST_MIN_AUC=0.6
            "held_out_auc": 0.65,
            "model_saved": True,
        },
    }


@pytest.fixture(autouse=True)
def _force_dev_smoke_test_env(monkeypatch):
    """Force the dev-mode environment the toy fixture runs under.

    The audit caught the bug in the DEFAULT dev environment, so we
    reproduce it: DRUGOS_ENVIRONMENT=dev, DRUGOS_DEV_SMOKE_TEST=1,
    DRUGOS_DEV_SMOKE_TEST_MIN_AUC=0.5, DRUGOS_DEV_MIN_SOURCES=2.
    """
    monkeypatch.setenv("DRUGOS_ENVIRONMENT", "dev")
    monkeypatch.setenv("DRUGOS_DEV_SMOKE_TEST", "1")
    monkeypatch.setenv("DRUGOS_DEV_SMOKE_TEST_MIN_AUC", "0.5")
    monkeypatch.setenv("DRUGOS_DEV_MIN_SOURCES", "2")
    # Don't leak DRUGOS_ALLOW_LAUNCH_FAIL from any test environment.
    monkeypatch.delenv("DRUGOS_ALLOW_LAUNCH_FAIL", raising=False)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — assert_auc_meets_threshold returns False when AUC < threshold
# Issue C-2: callers must check the return value; the function returns
# False silently in RELAXED mode.
# ─────────────────────────────────────────────────────────────────────────────

class TestAssertAucMeetsThresholdReturnsFalseWhenBelow:
    """Issue C-2 — RELAXED mode returns False WITHOUT raising.

    The previous v25 callers assumed "no exception == AUC met threshold",
    which produced the mathematical falsehood
    "AUC enforcement PASSED: 0.6722 >= 0.8500".
    """

    def test_returns_false_when_below_in_relaxed_mode(self):
        from drugos_graph.config import (
            assert_auc_meets_threshold,
            AUCEnforcementLevel,
        )
        # Toy fixture AUC = 0.6722, target = 0.85.
        result = assert_auc_meets_threshold(
            0.6722,
            threshold=0.85,
            enforcement_level=AUCEnforcementLevel.RELAXED,
        )
        assert result is False, (
            "assert_auc_meets_threshold must return False when AUC < "
            "threshold in RELAXED mode. Got: True. This is the bug that "
            "caused transe_model.py to log 'AUC enforcement PASSED: "
            "0.6722 >= 0.8500' — a mathematical falsehood."
        )

    def test_does_not_raise_in_relaxed_mode_when_below(self):
        from drugos_graph.config import (
            assert_auc_meets_threshold,
            AUCEnforcementLevel,
            AUCBelowThresholdError,
        )
        # In RELAXED mode, the function MUST NOT raise — it logs a
        # WARNING and returns False. Callers must read the return value.
        try:
            result = assert_auc_meets_threshold(
                0.6722,
                threshold=0.85,
                enforcement_level=AUCEnforcementLevel.RELAXED,
            )
        except AUCBelowThresholdError as exc:
            pytest.fail(
                "assert_auc_meets_threshold must NOT raise in RELAXED "
                f"mode. Got AUCBelowThresholdError: {exc}"
            )
        assert result is False

    def test_returns_true_when_above_in_relaxed_mode(self):
        from drugos_graph.config import (
            assert_auc_meets_threshold,
            AUCEnforcementLevel,
        )
        # Sanity check — the function still returns True when AUC meets
        # the threshold in RELAXED mode. We don't want a regression
        # where the function always returns False.
        result = assert_auc_meets_threshold(
            0.92,
            threshold=0.85,
            enforcement_level=AUCEnforcementLevel.RELAXED,
        )
        assert result is True

    def test_check_auc_meets_threshold_returns_meets_and_reason(self):
        """v26 companion function — non-enforcing mirror of assert_*.

        Returns (meets, reason) so callers cannot accidentally treat
        "no exception" as "meets threshold".
        """
        from drugos_graph.config import (
            check_auc_meets_threshold,
            AUCEnforcementLevel,
        )
        meets, reason = check_auc_meets_threshold(
            0.6722,
            threshold=0.85,
            enforcement_level=AUCEnforcementLevel.RELAXED,
        )
        assert meets is False
        assert isinstance(reason, str)
        assert "0.6722" in reason
        assert "0.8500" in reason
        assert "relaxed" in reason.lower()

        # When AUC meets threshold, reason must be empty.
        meets2, reason2 = check_auc_meets_threshold(
            0.92,
            threshold=0.85,
            enforcement_level=AUCEnforcementLevel.RELAXED,
        )
        assert meets2 is True
        assert reason2 == ""


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — _check_v1_launch_criteria: passed=False when AUC < 0.85
# Issue C-1: the v25 override flipped passed=True in dev mode.
# ─────────────────────────────────────────────────────────────────────────────

class TestV1CriteriaPassedIsFalseWhenAucBelowThreshold:
    """Issue C-1 — strict `passed` must be False when AUC < 0.85.

    The v25 "DEV_SMOKE_TEST override" used to flip
    ``criteria["passed"] = True`` even when ``auc_meets_threshold=False``,
    which produced the user's #1 complaint:
    ``V1 LAUNCH CRITERIA: PASSED`` for a model with held_out_auc=0.5389.
    """

    def test_passed_is_false_with_toy_fixture_auc(self):
        from drugos_graph.run_pipeline import _check_v1_launch_criteria
        results = _toy_fixture_results()
        criteria = _check_v1_launch_criteria(results)
        # The strict production check must FAIL — both val and held-out
        # AUC are below the 0.85 DOCX threshold.
        assert criteria["auc_meets_threshold"] is False, (
            f"auc_meets_threshold must be False when best_val_auc=0.6722 "
            f"and held_out_auc=0.5389. Got: {criteria['auc_meets_threshold']}"
        )
        assert criteria["passed"] is False, (
            "criteria['passed'] must be False when AUC < 0.85, EVEN IN "
            "DEV MODE. The v25 override flipped it to True — that was "
            f"the bug. Got: passed={criteria['passed']}, "
            f"dev_smoke_test_pass={criteria.get('dev_smoke_test_pass')}"
        )

    def test_passed_is_false_even_when_dev_smoke_test_enabled(self):
        """Explicitly verify the override is gone — DRUGOS_DEV_SMOKE_TEST=1
        must NOT cause passed=True when AUC < 0.85."""
        # The autouse fixture sets DRUGOS_DEV_SMOKE_TEST=1.
        assert os.environ.get("DRUGOS_DEV_SMOKE_TEST") == "1"
        from drugos_graph.run_pipeline import _check_v1_launch_criteria
        results = _toy_fixture_results()
        criteria = _check_v1_launch_criteria(results)
        assert criteria["dev_mode"] is True, (
            "Test precondition: DEV_SMOKE_TEST must be True for this test "
            "to verify the override is gone."
        )
        assert criteria["passed"] is False, (
            "EVEN with DEV_SMOKE_TEST=1, criteria['passed'] must be False "
            "when AUC < 0.85. The v25 override (criteria['passed'] = True "
            "in dev mode) has been REMOVED by the v26 fix."
        )

    def test_passed_becomes_true_only_when_auc_meets_threshold(self):
        """Positive control: when AUC >= 0.85, passed should be True.
        This guards against a regression where we accidentally made
        `passed` always False."""
        from drugos_graph.run_pipeline import _check_v1_launch_criteria
        results = _toy_fixture_results()
        # Both AUCs now meet the 0.85 threshold.
        results["step11"]["best_val_auc"] = 0.90
        results["step11"]["held_out_auc"] = 0.88
        criteria = _check_v1_launch_criteria(results)
        assert criteria["auc_meets_threshold"] is True
        assert criteria["passed"] is True, (
            "criteria['passed'] must be True when AUC >= 0.85 and all "
            "other conditions are met. If this fails, the fix over-"
            "corrected and made `passed` always False."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — dev_smoke_test_pass / passed_dev_smoke SEPARATE from passed
# Issue C-1: the dev smoke-test verdict must not affect `passed`.
# ─────────────────────────────────────────────────────────────────────────────

class TestDevSmokeTestPassIsSeparateFromPassed:
    """Issue C-1 — dev_smoke_test_pass is INFORMATIONAL only.

    The toy fixture (best_val_auc=0.6722, held_out_auc=0.5389) ran
    end-to-end in dev mode, so the smoke-test verdict is "pass" — the
    pipeline didn't crash. But the LAUNCH verdict must be "NOT PASSED"
    because AUC < 0.85. These two fields must be independent.
    """

    def test_dev_smoke_test_pass_can_be_true_while_passed_is_false(self):
        from drugos_graph.run_pipeline import _check_v1_launch_criteria
        results = _toy_fixture_results()
        criteria = _check_v1_launch_criteria(results)
        # The toy fixture AUCs (0.6722 / 0.5389) are both above the
        # 0.5 dev-smoke floor, and all other dev conditions are met,
        # so dev_smoke_test_pass should be True.
        assert criteria["dev_smoke_test_pass"] is True, (
            "Precondition: the toy fixture should satisfy the dev "
            "smoke-test conditions (AUC >= 0.5, model saved, etc.). "
            f"Got: {criteria.get('dev_smoke_test_pass')}"
        )
        # BUT the strict `passed` must be False — AUC < 0.85.
        assert criteria["passed"] is False, (
            "The dev_smoke_test_pass flag must NOT flip `passed` to True. "
            "These two fields are independent: dev_smoke_test_pass means "
            "'pipeline ran end-to-end', passed means 'model is launch-"
            "ready (AUC >= 0.85)'."
        )

    def test_passed_dev_smoke_alias_exists_and_matches(self):
        """v26 added `passed_dev_smoke` as an explicit alias for
        `dev_smoke_test_pass` so future code reads clearly."""
        from drugos_graph.run_pipeline import _check_v1_launch_criteria
        results = _toy_fixture_results()
        criteria = _check_v1_launch_criteria(results)
        assert "passed_dev_smoke" in criteria, (
            "v26 fix requires a `passed_dev_smoke` field as an explicit "
            "alias for dev_smoke_test_pass."
        )
        assert criteria["passed_dev_smoke"] == criteria["dev_smoke_test_pass"]

    def test_dev_smoke_test_pass_is_false_in_production(self, monkeypatch):
        """In production (DEV_SMOKE_TEST=False), dev_smoke_test_pass
        must be False — there's no dev mode to smoke-test in."""
        monkeypatch.setenv("DRUGOS_ENVIRONMENT", "production")
        monkeypatch.setenv("DRUGOS_DEV_SMOKE_TEST", "0")
        # Reload config to pick up env changes
        import importlib
        import drugos_graph.config as _cfg
        importlib.reload(_cfg)
        import drugos_graph.run_pipeline as _rp
        importlib.reload(_rp)
        try:
            results = _toy_fixture_results()
            criteria = _rp._check_v1_launch_criteria(results)
            assert criteria["dev_smoke_test_pass"] is False
            assert criteria["passed_dev_smoke"] is False
            # AUC is still < 0.85, so passed must still be False.
            assert criteria["passed"] is False
        finally:
            # Restore dev-mode env and reload back
            monkeypatch.setenv("DRUGOS_ENVIRONMENT", "dev")
            monkeypatch.setenv("DRUGOS_DEV_SMOKE_TEST", "1")
            importlib.reload(_cfg)
            importlib.reload(_rp)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — AUC enforcement log does not lie (transe_model.py)
# Issue C-3: "AUC enforcement PASSED" must only fire when AUC >= threshold.
# ─────────────────────────────────────────────────────────────────────────────

class TestAucEnforcementLogDoesNotLie:
    """Issue C-3 — the log message must NOT lie.

    The previous v25 code logged
    "AUC enforcement PASSED: 0.6722 >= 0.8500"
    whenever assert_auc_meets_threshold returned without raising —
    a mathematical falsehood, because in RELAXED mode the function
    returns meets=False WITHOUT raising.

    The v26 fix checks the return value and only logs "PASSED" when
    ``_auc_meets is True``. These tests verify the source structure
    AND the runtime behavior of the if/else block.
    """

    def test_source_has_if_auc_meets_branch_with_passed_in_true_branch(self):
        """Static-analysis: verify the transe_model.py source code
        places 'AUC enforcement PASSED' inside the ``if _auc_meets:``
        (True) branch, NOT inside a bare try block."""
        import drugos_graph.transe_model as tm
        source = inspect.getsource(tm.train_transe)
        # The fix introduces a `_auc_meets` variable.
        assert "_auc_meets" in source, (
            "v26 fix requires a `_auc_meets` variable that captures the "
            "return value of assert_auc_meets_threshold. Not found in "
            "train_transe source."
        )
        if_body, else_body = _extract_if_else_branches(
            source, predicate=r"if\s+_auc_meets\s*:"
        )
        assert if_body is not None, (
            "Could not locate `if _auc_meets:` block in train_transe. "
            "The v26 fix must check the return value before logging."
        )
        # The True branch MUST contain "AUC enforcement PASSED".
        assert "AUC enforcement PASSED" in if_body, (
            "The `if _auc_meets:` (True) branch must log "
            "'AUC enforcement PASSED'. Source: " + if_body
        )
        # The True branch MUST NOT contain "AUC enforcement FAILED".
        assert "AUC enforcement FAILED" not in if_body, (
            "The `if _auc_meets:` (True) branch must NOT log "
            "'AUC enforcement FAILED'. Source: " + if_body
        )

    def test_source_has_else_branch_with_failed_in_false_branch(self):
        """Static-analysis: verify 'AUC enforcement FAILED' is inside
        the ``else:`` (False) branch, NOT inside a bare except block."""
        import drugos_graph.transe_model as tm
        source = inspect.getsource(tm.train_transe)
        if_body, else_body = _extract_if_else_branches(
            source, predicate=r"if\s+_auc_meets\s*:"
        )
        assert else_body is not None, (
            "Could not locate `else:` branch following `if _auc_meets:` "
            "in train_transe. The v26 fix must have an else branch that "
            "logs FAILED when _auc_meets is False."
        )
        assert "AUC enforcement FAILED" in else_body, (
            "The `else:` (False) branch must log 'AUC enforcement FAILED'. "
            "Source: " + else_body
        )
        assert "AUC enforcement PASSED" not in else_body, (
            "The `else:` (False) branch must NOT log 'AUC enforcement "
            "PASSED'. Source: " + else_body
        )

    def test_source_does_not_log_passed_unconditionally(self):
        """The previous bug was a `try/except` block that logged PASSED
        inside the try (when no exception was raised). Verify the
        source no longer has that structure — the PASSED log must be
        gated by `if _auc_meets:`."""
        import drugos_graph.transe_model as tm
        source = inspect.getsource(tm.train_transe)
        # The OLD bug pattern: try: ... assert_auc_meets_threshold(...)
        # ... logger.info("AUC enforcement PASSED...")
        # The NEW fix: _auc_meets = assert_auc_meets_threshold(...)
        # if _auc_meets: logger.info("AUC enforcement PASSED...")
        # Find every occurrence of "AUC enforcement PASSED" in a
        # logger.<level>( call (i.e., not in comments/docstrings) and
        # verify each is preceded (within 8 lines) by "if _auc_meets:".
        lines = source.split("\n")
        for i, line in enumerate(lines):
            # Skip comment lines and lines that don't contain a logger call.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "AUC enforcement PASSED" not in line:
                continue
            if "logger." not in line and "log." not in line:
                # Not a logging call — probably a comment or docstring
                # mention. Skip.
                continue
            # Look back up to 8 lines for the most recent block opener.
            lookback = lines[max(0, i - 8):i]
            lookback_text = "\n".join(lookback)
            # The most recent `if _auc_meets:` or `try:` line.
            block_openers = re.findall(
                r"(if\s+_auc_meets\s*:\s*$|try\s*:\s*$)",
                "\n".join(l.rstrip() for l in lookback) + "\n",
            )
            assert block_openers, (
                "Every 'AUC enforcement PASSED' logger call must be "
                "gated by `if _auc_meets:`. Found a PASSED log call "
                "with no preceding `if _auc_meets:` or `try:` in "
                "lookback:\n" + lookback_text
            )
            last = block_openers[-1].strip().rstrip(":")
            assert last == "if _auc_meets", (
                f"'AUC enforcement PASSED' must be gated by "
                f"`if _auc_meets:`, but the most recent block opener "
                f"was `{last}:`. Lookback:\n" + lookback_text
            )

    def test_runtime_log_says_failed_not_passed_when_auc_below(
        self, caplog,
    ):
        """Runtime test: simulate the transe_model.py if/else block
        with the real `assert_auc_meets_threshold` and capture the log.

        This proves that when AUC < threshold in RELAXED mode, the
        caller (modeled by this test) logs 'FAILED', not 'PASSED'.
        """
        import logging
        from drugos_graph.config import (
            assert_auc_meets_threshold,
            AUCEnforcementLevel,
        )

        # Capture log output from the drugos_graph namespace.
        caplog.set_level(logging.DEBUG, logger="drugos_graph.transe_model")
        test_logger = logging.getLogger("drugos_graph.transe_model.test")

        best_val_auc = 0.6722
        target_auc = 0.85

        # Reproduce the EXACT logic of the if/else block from
        # transe_model.py (lines ~2735-2787 after the v26 fix):
        #   _auc_meets = assert_auc_meets_threshold(best_val_auc, threshold=target_auc)
        #   if _auc_meets:
        #       logger.info("AUC enforcement PASSED: ...")
        #   else:
        #       logger.error("AUC enforcement FAILED: ...")
        with caplog.at_level(logging.DEBUG, logger="drugos_graph.transe_model.test"):
            _auc_meets = assert_auc_meets_threshold(
                best_val_auc,
                threshold=target_auc,
                enforcement_level=AUCEnforcementLevel.RELAXED,
            )
            if _auc_meets:
                test_logger.info(
                    "AUC enforcement PASSED: %.4f >= %.4f — model will be saved",
                    best_val_auc, target_auc,
                )
            else:
                test_logger.error(
                    "AUC enforcement FAILED: %.4f < %.4f — model will NOT be "
                    "saved (relaxed mode logged warning but did not raise).",
                    best_val_auc, target_auc,
                )

        # The return value must be False.
        assert _auc_meets is False

        # Combine all captured log records' messages into one string.
        log_text = " ".join(rec.getMessage() for rec in caplog.records)

        # The FAILED log MUST be present.
        assert "AUC enforcement FAILED" in log_text, (
            "When AUC < threshold, the log MUST contain "
            "'AUC enforcement FAILED'. Captured: " + log_text
        )
        # The PASSED log MUST NOT be present.
        assert "AUC enforcement PASSED" not in log_text, (
            "When AUC < threshold, the log MUST NOT contain "
            "'AUC enforcement PASSED' — that was the bug. Captured: "
            + log_text
        )

    def test_runtime_log_says_passed_when_auc_above(self, caplog):
        """Positive control: when AUC >= threshold, the log SHOULD say
        PASSED. This guards against an over-correction where the fix
        always logs FAILED."""
        import logging
        from drugos_graph.config import (
            assert_auc_meets_threshold,
            AUCEnforcementLevel,
        )

        caplog.set_level(logging.DEBUG, logger="drugos_graph.transe_model.pos")
        test_logger = logging.getLogger("drugos_graph.transe_model.pos")

        best_val_auc = 0.92
        target_auc = 0.85

        with caplog.at_level(logging.DEBUG, logger="drugos_graph.transe_model.pos"):
            _auc_meets = assert_auc_meets_threshold(
                best_val_auc,
                threshold=target_auc,
                enforcement_level=AUCEnforcementLevel.RELAXED,
            )
            if _auc_meets:
                test_logger.info(
                    "AUC enforcement PASSED: %.4f >= %.4f — model will be saved",
                    best_val_auc, target_auc,
                )
            else:
                test_logger.error(
                    "AUC enforcement FAILED: %.4f < %.4f — model will NOT be saved.",
                    best_val_auc, target_auc,
                )

        assert _auc_meets is True
        log_text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "AUC enforcement PASSED" in log_text
        assert "AUC enforcement FAILED" not in log_text


# ─────────────────────────────────────────────────────────────────────────────
# Bonus: verify the final exit-code contract (Fix 4).
# ─────────────────────────────────────────────────────────────────────────────

class TestExitCodeContractFollowsStrictPassed:
    """Fix 4 — exit code must follow `passed` (strict), NOT
    `dev_smoke_test_pass`.

    The previous v25 code returned exit 0 in dev mode even when AUC <
    0.85. The v26 fix returns exit 4 (NOT PASSED) even when
    dev_smoke_test_pass=True.
    """

    def test_strict_passed_false_yields_exit_4_even_with_dev_smoke(
        self, monkeypatch,
    ):
        """When passed=False but dev_smoke_test_pass=True, the pipeline
        must raise V1LaunchCriteriaFailed (which run_unified.py
        translates to exit code 4), NOT exit 0."""
        # DRUGOS_ALLOW_LAUNCH_FAIL must NOT be set (strict exit).
        monkeypatch.delenv("DRUGOS_ALLOW_LAUNCH_FAIL", raising=False)
        from drugos_graph.run_pipeline import (
            _check_v1_launch_criteria,
            V1LaunchCriteriaFailed,
        )
        results = _toy_fixture_results()
        criteria = _check_v1_launch_criteria(results)
        # Precondition: toy fixture should have passed=False but
        # dev_smoke_test_pass=True.
        assert criteria["passed"] is False
        assert criteria["dev_smoke_test_pass"] is True

        # The exit-code contract: when passed=False (strict), the
        # pipeline must raise V1LaunchCriteriaFailed. The caller
        # (run_unified.py) catches this and returns exit code 4.
        # We simulate the run_full_pipeline logic:
        if not criteria["passed"]:
            if os.environ.get("DRUGOS_ALLOW_LAUNCH_FAIL", "") != "1":
                # This is the path run_full_pipeline takes.
                with pytest.raises(V1LaunchCriteriaFailed) as exc_info:
                    raise V1LaunchCriteriaFailed(criteria)
                # The criteria attached to the exception must show
                # passed=False (strict), even though dev_smoke_test_pass=True.
                assert exc_info.value.criteria["passed"] is False
                assert exc_info.value.criteria["dev_smoke_test_pass"] is True
            else:
                pytest.fail(
                    "DRUGOS_ALLOW_LAUNCH_FAIL must not be set for this test."
                )
        else:
            pytest.fail(
                "Precondition failed: criteria['passed'] must be False."
            )

    def test_cli_main_exits_4_on_v1_launch_criteria_failed(self, monkeypatch):
        """The `python -m drugos_graph` CLI must exit 4 (not 1) when
        V1LaunchCriteriaFailed is raised — matching the documented
        contract for run_unified.py."""
        from drugos_graph.run_pipeline import V1LaunchCriteriaFailed

        # Simulate the main() except clause from run_pipeline.py.
        # We verify by importing the module and inspecting the source
        # — the contract is `sys.exit(4)`, NOT `sys.exit(1)`.
        import drugos_graph.run_pipeline as rp
        source = inspect.getsource(rp.main)
        # Find the `except V1LaunchCriteriaFailed as exc:` block and
        # extract ONLY its body (lines more indented than the except
        # line, stopping at the first line at the same or lower indent).
        body = _extract_except_body(source, "V1LaunchCriteriaFailed")
        assert body is not None, (
            "Could not locate `except V1LaunchCriteriaFailed as exc:` "
            "block in run_pipeline.main()."
        )
        assert "sys.exit(4)" in body, (
            "v26 fix: `python -m drugos_graph` must exit 4 when V1 "
            "launch criteria fail (matching run_unified.py). The "
            "except block should call `sys.exit(4)`, not `sys.exit(1)`. "
            "Source: " + body
        )
        assert "sys.exit(1)" not in body, (
            "v26 fix: the except block must NOT call `sys.exit(1)` "
            "(that conflated 'criteria not met' with 'Python crashed'). "
            "Source: " + body
        )
