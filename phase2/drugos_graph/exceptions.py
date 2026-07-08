"""DrugOS Graph Module — Exception Hierarchy
============================================
Domain-specific exceptions for the DrugOS data pipeline.

This module centralises the exception types raised by the data loaders
(notably ``uniprot_loader``) so that callers can catch failures with the
appropriate granularity. Every exception here carries enough context
(URL, accession, line number, remediation hint) to debug a 3 AM pipeline
failure without re-reading the source file.

Design rules (audit issues D6-006, D1-002):
  * Exceptions are **additive** — they do not replace any stdlib exception.
    Existing ``except FileNotFoundError`` / ``except Exception`` blocks in
    callers continue to work because these new types subclass the most
    relevant stdlib base.
  * Every exception stores a structured ``context`` dict so that the
    dead-letter writer and the structured logger can serialise it without
    re-formatting strings.
  * No third-party dependencies — stdlib only.

Fixes: D6-006 (wrap raw URLError), D1-002 (Loader Protocol error contract),
       D5-005/D5-007 (data-integrity guard errors).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__: list[str] = [
    "DrugOSDataError",
    "UniProtDownloadError",
    "UniProtParseError",
    "UniProtDataIntegrityError",
    # DRKG exceptions — added by drkg_loader v2.0 audit fix
    # (DRKG-002 / BUG 6.2 in drkg_loader_repair_prompt.md).
    "DRKGDownloadError",
    "DRKGParseError",
    "DRKGDataIntegrityError",
    # DrugBank exceptions — added by drugbank_parser v2.0 audit fix
    # (drugbank_parser_fix_prompt.md — Domain 6 Reliability, FIX 6.14).
    # These three exception types mirror the UniProt and DRKG trios
    # (Download / Parse / DataIntegrity) so that ``run_pipeline.py`` and
    # the ``Loader`` Protocol can treat all loaders polymorphically.
    "DrugBankDownloadError",
    "DrugBankParseError",
    "DrugBankDataIntegrityError",
    # ChEMBL exceptions — added by the chembl_loader v2.0 audit fix
    # (chembl_loader institutional-grade rewrite — Domain 6 Reliability).
    "ChEMBLDownloadError",
    "ChEMBLParseError",
    "ChEMBLDataIntegrityError",
    # STRING exceptions — added by the string_loader v1.0 institutional-grade
    # audit fix (master_prompt_fix_string_loader.md — Sections 5, 9).
    # The first three mirror the UniProt/DRKG/DrugBank/ChEMBL trios
    # (Download / Parse / DataIntegrity). The remaining four are
    # STRING-specific:
    #   * StringEdgeLoadMismatchError — Neo4j load dropped edges (D5-10)
    #   * CircuitBreakerOpenError     — repeated download failure (R6-12)
    #   * CriticalDataSourceError     — 0 edges on required source (D5-10)
    #   * SecurityError               — URL/path-traversal / SSRF (S9-02/03)
    #   * ConfigurationError          — invalid STRING_* config (C12-08)
    "StringDownloadError",
    "StringParseError",
    "StringDataIntegrityError",
    "StringEdgeLoadMismatchError",
    "CircuitBreakerOpenError",
    "CriticalDataSourceError",
    "SecurityError",
    "ConfigurationError",
    # STITCH exceptions — added by the stitch_loader v1.1.0 institutional-grade
    # audit fix (master_prompt_fix_stitch_loader.md — Sections 3, 9).
    # The first three mirror the STRING trio (Download / Parse / DataIntegrity)
    # plus three STITCH-specific extras:
    #   * StitchEdgeLoadMismatchError — Neo4j load dropped edges (BUG-15.1)
    #   * StitchSecurityError         — URL/path-traversal / SSRF (BUG-9.1, GAP-9.4)
    #   * StitchConfigurationError    — invalid STITCH_* config (GAP-12.4, BUG-12.1)
    # All STITCH exceptions subclass DrugOSDataError so callers can write
    # `except DrugOSDataError` to catch any pipeline failure while still
    # letting unrelated bugs propagate. They are SIBLING classes to the
    # STRING exceptions — STITCH errors must NOT be caught by
    # `except StringDownloadError` (catch granularity, master prompt R7).
    "StitchDownloadError",
    "StitchParseError",
    "StitchDataIntegrityError",
    "StitchEdgeLoadMismatchError",
    "StitchSecurityError",
    "StitchConfigurationError",
    # SIDER exceptions — added by sider_loader v1.0.0 institutional-grade
    # audit fix (master_prompt — Section 3, Phase 0.4 / Domain 6 / Domain 9).
    # These six exception types extend the DrugOSDataError hierarchy with
    # SIDER-specific failures. They follow the same ``context`` kwarg pattern
    # as every other loader exception so that the dead-letter writer and the
    # structured logger can serialise them without re-formatting strings.
    #
    # IMPORTANT catch-granularity design (mirrors STITCH/STRING siblings):
    #   * SIDER exceptions are SIBLINGS of the STITCH/STRING exceptions — they
    #     do NOT subclass Stitch*Error or String*Error. This means
    #     ``except StitchDownloadError`` will NOT catch ``SiderDownloadError``.
    #     Callers wanting to catch any loader failure should use
    #     ``except DrugOSDataError``.
    #   * ``SiderParseError`` MULTIPLE-INHERITS from ``DrugOSDataError`` AND
    #     ``FileNotFoundError`` so existing ``except FileNotFoundError`` blocks
    #     in callers continue to work (backward compat — Rule R3).
    #
    # Patient-safety doctrine: SIDER is the SOLE source of adverse-event
    # data feeding the RL safety-signal dimension. If this loader emits a
    # wrong CID, wrong UMLS CUI, or fails silently, the safety ranker will
    # see zero adverse events for every drug and rank dangerous drugs as
    # GREEN (recommend). These exceptions MUST NOT be silenced — every
    # ``except SiderCriticalError`` block must either re-raise or fail the
    # pipeline loudly (Rule R5: no silent failures).
    "SiderCriticalError",
    "SiderDownloadError",
    "SiderParseError",
    "SiderDataQualityError",
    "SiderSchemaError",
    "SiderDualWriteError",
    # OpenTargets exceptions — added by opentargets_loader v2.0 institutional-grade
    # audit fix (opentargets_loader_repair_prompt.md — Section 2.7).
    # These seven exception types extend the DrugOSDataError hierarchy with
    # OpenTargets-specific failures. They follow the same ``context`` kwarg
    # pattern as every other loader exception so that the dead-letter writer
    # and the structured logger can serialise them without re-formatting.
    #
    # IMPORTANT catch-granularity design (mirrors SIDER/STITCH/STRING siblings):
    #   * OpenTargets exceptions are SIBLINGS of the SIDER/STITCH/STRING
    #     exceptions — they do NOT subclass Sider*Error / Stitch*Error /
    #     String*Error. This means ``except StitchDownloadError`` will NOT
    #     catch ``OpenTargetsDownloadError``. Callers wanting to catch any
    #     loader failure should use ``except DrugOSDataError``.
    #   * ``OpenTargetsParseError`` MULTIPLE-INHERITS from ``DrugOSDataError``
    #     AND ``FileNotFoundError`` so existing ``except FileNotFoundError``
    #     blocks in callers continue to work (backward compat — Rule R3).
    #
    # Patient-safety doctrine: OpenTargets is the SOLE source of evidence-
    # scored drug-target-disease triples feeding the Graph Transformer's
    # confidence training objective. If this loader silently drops 100% of
    # records (the v1 SCI-1 condition), the model trains on an empty
    # OpenTargets signal — worse than no signal. These exceptions MUST NOT
    # be silenced — every ``except OpenTargetsDataIntegrityError`` block
    # must either re-raise or fail the pipeline loudly (Rule R5: no silent
    # failures).
    "OpenTargetsDownloadError",
    "OpenTargetsParseError",
    "OpenTargetsDataIntegrityError",
    "OpenTargetsSecurityError",
    "OpenTargetsConfigurationError",
    "OpenTargetsEdgeLoadMismatchError",
    "OpenTargetsSchemaError",
    # ClinicalTrials exceptions — added by clinicaltrials_loader v2.1.0
    # institutional-grade audit fix
    # (PROMPT_fix_clinicaltrials_loader.md — 148 findings across 16 domains).
    # These seven exception types extend the DrugOSDataError hierarchy with
    # ClinicalTrials-specific failures. They follow the same ``context`` kwarg
    # pattern as every other loader exception so that the dead-letter writer
    # and the structured logger can serialise them without re-formatting.
    #
    # IMPORTANT catch-granularity design (mirrors OpenTargets/SIDER/STITCH/STRING
    # siblings):
    #   * ClinicalTrials exceptions are SIBLINGS of the OpenTargets / SIDER /
    #     STITCH / STRING exceptions — they do NOT subclass any sibling. This
    #     means ``except OpenTargetsDownloadError`` will NOT catch
    #     ``ClinicalTrialsDownloadError``. Callers wanting to catch any loader
    #     failure should use ``except DrugOSDataError``.
    #   * ``ClinicalTrialsParseError`` MULTIPLE-INHERITS from ``DrugOSDataError``
    #     AND ``FileNotFoundError`` so existing ``except FileNotFoundError``
    #     blocks in callers continue to work (backward compat — Rule R3).
    #
    # Patient-safety doctrine: ClinicalTrials.gov AACT is the SOLE source of
    # clinical-trial evidence feeding the RL ranker's "has been tested in
    # humans" dimension. If this loader silently fabricates a
    # ``Warfarin -> Disease X`` edge (because Warfarin was the *comparator*
    # arm, not the experimental arm), the ranker learns that Warfarin treats
    # X. A clinician who trusts that ranker can prescribe Warfarin off-label
    # to a patient for whom it is contraindicated. THAT PATIENT CAN DIE.
    # These exceptions MUST NOT be silenced — every
    # ``except ClinicalTrialsDataIntegrityError`` block must either re-raise
    # or fail the pipeline loudly (Rule R5: no silent failures).
    "ClinicalTrialsDownloadError",
    "ClinicalTrialsParseError",
    "ClinicalTrialsDataIntegrityError",
    "ClinicalTrialsSecurityError",
    "ClinicalTrialsConfigurationError",
    "ClinicalTrialsEdgeLoadMismatchError",
    "ClinicalTrialsSchemaError",
    # GEO exceptions — added by geo_loader v1.0.0 institutional-grade audit
    # fix (GEO_LOADER_MASTER_REPAIR_PROMPT.md — 192 findings across 16 domains).
    # These eight exception types extend the DrugOSDataError hierarchy with
    # GEO-specific failures. They follow the same ``context`` kwarg pattern
    # as every other loader exception so the dead-letter writer and the
    # structured logger can serialise them without re-formatting strings.
    #
    # IMPORTANT catch-granularity design (mirrors ClinicalTrials/OpenTargets/
    # SIDER/STITCH/STRING siblings):
    #   * GEO exceptions are SIBLINGS of the other loader exceptions — they
    #     do NOT subclass ClinicalTrials*Error / OpenTargets*Error /
    #     Sider*Error / Stitch*Error / String*Error. This means
    #     ``except SiderDownloadError`` will NOT catch ``GeoDownloadError``.
    #     Callers wanting to catch any loader failure should use
    #     ``except DrugOSDataError``.
    #   * ``GeoParseError`` MULTIPLE-INHERITS from ``DrugOSDataError`` AND
    #     ``FileNotFoundError`` so existing ``except FileNotFoundError``
    #     blocks in callers continue to work (backward compat — Rule R4).
    #
    # Patient-safety doctrine: GEO is the SOLE source of
    # Protein→expressed_in→Anatomy edges in the KG. If this loader silently
    # produces zero records, the KG lacks the entire tissue-specificity
    # modality, the model cannot learn that a drug target is absent from the
    # disease tissue, and a clinician can be handed a "high-confidence"
    # repurposing candidate that will fail in Phase II — or harm a patient
    # in a clinical-trial setting. These exceptions MUST NOT be silenced —
    # every ``except GeoCriticalError`` block must either re-raise or fail
    # the pipeline loudly (master prompt Rule R5).
    "GeoConfigurationError",
    "GeoSecurityError",
    "GeoDownloadError",
    "GeoDownloadRequiredError",
    "GeoParseError",
    "GeoDataQualityError",
    "GeoCriticalError",
    "GeoNotImplementedError",
    # Entity-Resolver exceptions -- added by entity_resolver v1.1.0
    # institutional-grade audit fix (ENTITY_RESOLVER_FIX_PROMPT.md --
    # 188 findings across 16 domains).
    # These five exception types extend the DrugOSDataError hierarchy with
    # entity-resolver-specific failures. They follow the same ``context``
    # kwarg pattern as every other module exception so the dead-letter
    # writer and the structured logger can serialise them without
    # re-formatting.
    #
    # IMPORTANT catch-granularity design (mirrors loader siblings):
    #   * Resolver exceptions are SIBLINGS of the loader exceptions -- they
    #     do NOT subclass any loader-specific error. Callers wanting to
    #     catch any pipeline failure should use ``except DrugOSDataError``.
    #   * ``ResolverError`` is the parent of the four sub-types below.
    #
    # Patient-safety doctrine: the entity resolver decides whether DrugBank
    # "DB00945", ChEMBL "CHEMBL25", PubChem "2244", and DRKG
    # "Compound::DB00945" all refer to the SAME molecule (aspirin). If it
    # emits the wrong canonical ID, two records of the same molecule become
    # two separate nodes in the KG, the GNN learns wrong drug-disease edges,
    # and a clinician can act on a wrong recommendation. These exceptions
    # MUST NOT be silenced -- every ``except ResolverProvenanceError`` /
    # ``except ResolverConflictError`` block must either re-raise or fail
    # the pipeline loudly (master prompt Rule R5).
    "ResolverError",
    "ResolverConfigurationError",
    "ResolverConflictError",
    "ResolverDataQualityError",
    "ResolverProvenanceError",
    # Evaluation exceptions — evaluation.py v2.0 audit fix
    "EvaluationError",
    "EvaluationInputError",
    "EvaluationIntegrityError",
    "EvaluationReproducibilityError",
    "EvaluationSecurityError",
    # v9 audit fix F7.8 — fail-closed validation for unknown node labels.
    "UnknownLabelError",
]


class DrugOSDataError(Exception):
    """Base class for all DrugOS data-pipeline errors.

    All loader-specific exceptions subclass this so that a caller can
    write ``except DrugOSDataError`` to catch any pipeline failure while
    still letting unrelated bugs propagate.

    Attributes
    ----------
    context : dict
        Structured key/value pairs describing the failure (URL, accession,
        line number, parser version, etc.). Always present, possibly empty.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.context: Dict[str, Any] = dict(context) if context else {}
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.context:
            return f"{base} | context={self.context}"
        return base


class UniProtDownloadError(DrugOSDataError):
    """Raised when the UniProt flat file cannot be downloaded.

    Wraps the underlying ``urllib.error.URLError`` / ``socket.timeout`` /
    ``OSError`` so that callers receive a single, well-typed exception with
    a remediation hint (e.g. the UniProt status page URL) instead of a raw
    stdlib error.

    Typical causes:
      * Network/DNS failure (retryable — see ``retry_count`` in config).
      * TLS certificate verification failure (D9-001).
      * URL not in the allowlist (D9-002 — refused before any network call).
      * Downloaded file fails the size or SHA-256 check (D5-006/D5-007).
      * Downloaded file fails the content-sniff assertion (D12-002 — tar.gz
        served where a flat .dat.gz was expected).
    """


class UniProtParseError(DrugOSDataError):
    """Raised when the UniProt flat file cannot be parsed safely.

    Raised when the per-line parse-error rate exceeds the configured
    threshold (default 1% of kept records — D6-003) or when a structural
    invariant is violated (e.g. the file is not valid UTF-8 even in
    degraded mode — D4-006).

    Individual malformed lines do NOT raise this exception; they are
    logged at WARNING, written to the dead-letter queue (D6-004), and
    skipped so that one bad line cannot kill a 570k-entry parse.
    """


class UniProtDataIntegrityError(DrugOSDataError):
    """Raised when parsed data fails a data-quality guard.

    Distinct from ``UniProtParseError`` (a *format* problem): this
    exception means the file parsed fine but the *content* is wrong.

    Typical causes:
      * Record count is more than 50% below ``expected_record_count``
        (D5-005) — almost always a URL/format mismatch or a corrupted file.
      * SHA-256 of the downloaded file does not match the pinned value
        (D5-007).
      * A record returned by ``uniprot_to_node_records`` is missing the
        required ``accession`` field (D1-003).
      * NCBI TaxID disagrees with the OS-line organism (D3-005).
    """


# =============================================================================
# DRKG exceptions — added by the drkg_loader v2.0 audit fix
# (drkg_loader_repair_prompt.md — Domain 6 Reliability, BUG 6.2).
#
# These three exception types mirror the UniProt trio (Download / Parse /
# DataIntegrity) so that ``run_pipeline.py`` and the ``Loader`` Protocol
# can treat all loaders polymorphically. Every DRKG exception subclasses
# ``DrugOSDataError`` so callers can write ``except DrugOSDataError`` to
# catch any pipeline failure while still letting unrelated bugs propagate.
# =============================================================================


class DRKGDownloadError(DrugOSDataError):
    """Raised when the DRKG ``drkg.tar.gz`` cannot be downloaded safely.

    Wraps the underlying ``urllib.error.URLError`` / ``socket.timeout`` /
    ``OSError`` so that callers receive a single, well-typed exception
    with a remediation hint, instead of a raw stdlib error.

    Typical causes (per the audit, drkg_loader_repair_prompt.md §Domain 9):
      * Network/DNS failure after all retries exhausted (BUG 6.1).
      * TLS certificate verification failure (BUG 9.1).
      * URL not in the allowlist ``config.ALLOWED_DRKG_URLS``
        (BUG 9.2 — refused before any network call).
      * Downloaded file fails the gzip content-sniff assertion
        (GAP 5.10 — server returned an HTML error page).
      * Tar member fails the path-traversal / type-safety check
        (BUG 9.4 / BUG 9.5).
      * ``allow_stale=False`` AND no cached copy exists.

    Fixes: BUG 6.1, BUG 6.2, BUG 9.1, BUG 9.2, GAP 5.10, BUG 9.4, BUG 9.5,
           BUG 14.4.
    """


class DRKGParseError(DrugOSDataError):
    """Raised when ``drkg.tsv`` cannot be parsed safely.

    Raised when ``pandas.errors.ParserError`` is caught (BUG 6.6), when
    the parsed DataFrame is missing required columns (BUG 15.3), or when
    a structural invariant is violated (e.g. no row contains the ``::``
    separator — BUG 4.3 / BUG 4.4).

    Individual malformed rows do NOT raise this exception; they are
    logged at WARNING, written to the dead-letter queue
    (``data/dead_letter/drkg_malformed.jsonl`` — GAP 5.11), and skipped
    so that one bad row cannot kill a 5.9M-triple parse.

    Fixes: BUG 4.3, BUG 4.4, BUG 6.6, BUG 15.3, BUG 16.1.
    """


class DRKGDataIntegrityError(DrugOSDataError):
    """Raised when parsed DRKG data fails a data-quality guard.

    Distinct from ``DRKGParseError`` (a *format* problem): this
    exception means the TSV parsed fine but the *content* is wrong.

    Typical causes (per the audit, drkg_loader_repair_prompt.md
    §Domain 5):
      * Row count is more than 5% below ``expected_record_count`` (BUG 5.1)
        — almost always a truncated download or corrupted file.
      * Entity-type count deviates from the expected 13 by more than ±1
        (BUG 5.2).
      * Relation-type count deviates from the expected 107 by more than ±1
        (BUG 5.2).
      * SHA-256 of the downloaded ``drkg.tar.gz`` does not match the
        pinned value (BUG 5.8) — possible MITM or S3 corruption.
      * Downloaded file size is below 90% of ``size_bytes`` (BUG 5.9) or
        above ``max_size_bytes`` (BUG 5.9).
      * ``build_edge_index_maps`` cannot find a head/tail entity in
        ``entity_maps`` (BUG 2.1) — indicates a parser bug.
      * ``build_networkx_graph`` finds an entity ID with multiple
        ``entity_type`` values (BUG 4.10) — data corruption.

    Fixes: BUG 2.1, BUG 4.10, BUG 5.1, BUG 5.2, BUG 5.8, BUG 5.9,
           GAP 5.10, GUARD 5.13.
    """


# =============================================================================
# DrugBank exceptions — added by the drugbank_parser v2.0 audit fix
# (drugbank_parser_fix_prompt.md — Domain 6 Reliability, FIX 6.14).
#
# These three exception types mirror the UniProt and DRKG trios (Download /
# Parse / DataIntegrity) so that ``run_pipeline.py`` and the ``Loader``
# Protocol can treat all loaders polymorphically. Every DrugBank exception
# subclasses ``DrugOSDataError`` so callers can write ``except DrugOSDataError``
# to catch any pipeline failure while still letting unrelated bugs propagate.
#
# Patient-safety doctrine: DrugBank is the canonical FDA-approved-drug
# reference for the project. If this parser emits a wrong or missing field,
# the model will silently train on garbage and recommend the wrong drug to a
# clinician. These exceptions MUST NOT be silenced — every ``except
# DrugBankDataIntegrityError`` block must either re-raise or fail the
# pipeline loudly.
# =============================================================================


class DrugBankDownloadError(DrugOSDataError):
    """Raised when the DrugBank XML dump cannot be downloaded safely.

    Wraps the underlying ``urllib.error.URLError`` / ``socket.timeout`` /
    ``OSError`` so that callers receive a single, well-typed exception
    with a remediation hint, instead of a raw stdlib error.

    Typical causes (per the audit, drugbank_parser_fix_prompt.md §Domain 9):
      * Network/DNS failure after all retries exhausted (FIX 6.3).
      * TLS certificate verification failure (FIX 9.2).
      * URL not in the allowlist ``config.ALLOWED_DRUGBANK_URLS``
        (FIX 9.1 — refused before any network call).
      * Downloaded file fails the XML content-sniff assertion (FIX 5.13 —
        DrugBank requires academic registration; an HTML login page is
        served where the XML was expected).
      * Downloaded file fails the size check (FIX 5.5) — too small (likely
        an error page) or too large (likely malicious).
      * SHA-256 of the downloaded file does not match the pinned value
        (FIX 5.2 — possible MITM or S3 corruption).
      * Credentials missing or invalid (FIX 9.4 — DrugBank requires
        academic registration; username/password read via
        ``config.get_secret``).

    Fixes: FIX 1.2, FIX 5.2, FIX 5.4, FIX 5.5, FIX 5.13, FIX 6.3,
           FIX 9.1, FIX 9.2, FIX 9.4.
    """


class DrugBankParseError(DrugOSDataError):
    """Raised when the DrugBank XML cannot be parsed safely.

    Raised when ``ET.ParseError`` is caught (FIX 5.12 — truncated or
    malformed XML), when the parser exceeds the memory ceiling (FIX 6.8),
    when the parser exceeds the timeout (FIX 6.6), when a per-drug parse
    fails repeatedly (FIX 6.12 — per-drug exception isolation), or when a
    structural invariant is violated (e.g. the root element is not
    ``<drugbank>`` — FIX 5.1).

    Individual malformed ``<drug>`` elements do NOT raise this exception;
    they are logged at WARNING, written to the dead-letter queue
    (``data/dead_letter/drugbank_malformed.jsonl`` — FIX 6.2), and
    skipped so that one bad drug cannot kill a 15k-drug parse.

    Fixes: FIX 5.1, FIX 5.12, FIX 6.1, FIX 6.4, FIX 6.6, FIX 6.8,
           FIX 6.12, FIX G.13.
    """


class DrugBankDataIntegrityError(DrugOSDataError):
    """Raised when parsed DrugBank data fails a data-quality guard.

    Distinct from ``DrugBankParseError`` (a *format* problem): this
    exception means the XML parsed fine but the *content* is wrong.

    Typical causes (per the audit, drugbank_parser_fix_prompt.md
    §Domain 5 and §Phase Q — Guards):
      * Parsed drug count is 0 (Guard G.1 — empty drugs list MUST NOT
        reach ``kg_builder``).
      * Parsed drug count is < 50% of ``expected_record_count`` (FIX 5.3
        — almost always a URL/format mismatch or a corrupted file).
      * SHA-256 of the source XML does not match the pinned value
        (FIX 5.2 — possible MITM or S3 corruption).
      * Downloaded file size is below 1 MB (FIX 5.5 — likely an HTML
        error page) or above ``max_size_bytes`` (FIX 5.5 — likely
        malicious).
      * Namespace mismatch (FIX 5.1 — root element namespace is not in
        ``DRUGBANK_NAMESPACE_ALIASES``).
      * DrugBank version downgrade detected (Guard G.16 — refusing to
        overwrite newer data with older).
      * Non-human target edge with ``organism_filter=9606`` reached the
        output (Guard G.3 — filter should have removed it).
      * Withdrawn drug reached ``drugbank_to_node_records`` without
        ``withdrawn=True`` flag set (Guard G.4 — patient safety).
      * Invalid SMILES reached the output (Guard G.5 —
        ``chemberta_encoder`` would silently drop the entire batch).
      * drugbank_id does not match ``^DB\\d{5,7}$`` (Guard G.14).
      * DrugBank file is severely stale (Guard G.11 — > 4x expected
        update frequency).
      * Parser is already running in another process (Guard G.12 —
        concurrent-execution guard).
      * Pathological XML detected (Guard G.13 — billion-laughs attack).
      * Non-academic deployment context (Guard G.17 — DrugBank CC BY-NC
        4.0 license prohibits non-academic use).
      * Majority of approved drugs have ``approval_year=None`` (Guard
        G.10 — temporal split would silently fall back to random).

    Fixes: FIX 5.1, FIX 5.2, FIX 5.3, FIX 5.5, FIX 5.6, FIX 5.7,
           FIX 5.19, FIX 5.20, FIX 14.11, FIX G.1, FIX G.3, FIX G.4,
           FIX G.5, FIX G.7, FIX G.8, FIX G.9, FIX G.10, FIX G.11,
           FIX G.12, FIX G.13, FIX G.14, FIX G.15, FIX G.16,
           FIX G.17, FIX G.18.
    """


# =============================================================================
# ChEMBL exceptions — added by the chembl_loader v2.0 audit fix
# (chembl_loader institutional-grade rewrite — Domain 6 Reliability).
#
# These three exception types mirror the UniProt, DRKG, and DrugBank trios
# (Download / Parse / DataIntegrity) so that ``run_pipeline.py`` and the
# ``Loader`` Protocol can treat all loaders polymorphically. Every ChEMBL
# exception subclasses ``DrugOSDataError`` so callers can write
# ``except DrugOSDataError`` to catch any pipeline failure while still
# letting unrelated bugs propagate.
#
# Patient-safety doctrine: ChEMBL is the primary source for drug-target
# bioactivity data. If this loader emits a wrong UniProt accession or a
# wrong relation type (e.g., "inhibits" for an agonist), the Graph
# Transformer will train on inverted edges and the RL ranker will
# recommend the wrong drug to a clinician. These exceptions MUST NOT be
# silenced — every ``except ChEMBLDataIntegrityError`` block must either
# re-raise or fail the pipeline loudly.
# =============================================================================


class ChEMBLDownloadError(DrugOSDataError):
    """Raised when the ChEMBL SQLite dump cannot be downloaded safely.

    Wraps the underlying ``urllib.error.URLError`` / ``socket.timeout`` /
    ``OSError`` so that callers receive a single, well-typed exception
    with a remediation hint, instead of a raw stdlib error.

    Typical causes:
      * Network/DNS failure after all retries exhausted.
      * TLS certificate verification failure.
      * URL not in the allowlist ``config.ALLOWED_CHEMBL_URLS``
        (refused before any network call).
      * Downloaded file fails the gzip/tar content-sniff assertion
        (server returned an HTML error page).
      * Tar member fails the path-traversal / type-safety check.
      * Downloaded file size is below ``config.CHEMBL_MIN_VALID_SIZE_BYTES``
        (truncated) or above ``max_size_bytes`` (likely malicious).
      * SHA-256 of the downloaded file does not match the pinned value
        (possible MITM or EBI FTP corruption).

    Fixes: Domain 6 Reliability, Domain 9 Security.
    """


class ChEMBLParseError(DrugOSDataError):
    """Raised when the ChEMBL SQLite database cannot be queried safely.

    Raised when ``sqlite3.OperationalError`` / ``sqlite3.DatabaseError``
    is caught (malformed or wrong-version database), when the SQL query
    returns unexpected columns (schema mismatch), or when a structural
    invariant is violated (e.g. no .db file found in the extracted
    directory).

    Individual malformed rows do NOT raise this exception; they are
    logged at WARNING, written to the dead-letter queue
    (``data/dead_letter/chembl_malformed.jsonl``), and skipped so that
    one bad row cannot kill a 2.4M-row parse.

    Fixes: Domain 6 Reliability, Domain 5 Data Quality.
    """


class ChEMBLDataIntegrityError(DrugOSDataError):
    """Raised when parsed ChEMBL data fails a data-quality guard.

    Distinct from ``ChEMBLParseError`` (a *format* problem): this
    exception means the database queried fine but the *content* is wrong.

    Typical causes:
      * Activity count is 0 (Guard — empty activities MUST NOT reach the
        knowledge graph).
      * Activity count is < 50% of ``expected_record_count`` (almost
        always a schema/version mismatch or a corrupted file).
      * SHA-256 of the downloaded tar.gz does not match the pinned value
        (possible MITM or EBI FTP corruption).
      * Downloaded file size is below ``CHEMBL_MIN_VALID_SIZE_BYTES``
        (likely an error page) or above ``max_size_bytes``.
      * A ChEMBL compound ID does not match ``^CHEMBL\\d{1,7}$``.
      * A UniProt accession does not match the expected regex.
      * pChEMBL value is outside [0, 14] range.
      * Majority of rows have no UniProt accession after crosswalk
        resolution (entity resolution failure).

    Fixes: Domain 5 Data Quality, Domain 7 Idempotency, Domain 16 Lineage.
    """


# =============================================================================
# STRING exceptions — added by string_loader v1.0 institutional-grade audit fix
# (master_prompt_fix_string_loader.md — Sections 5, 9).
#
# These eight exception types extend the DrugOSDataError hierarchy with
# STRING-specific failures. They follow the same ``context`` kwarg pattern
# as every other loader exception so that the dead-letter writer and the
# structured logger can serialise them without re-formatting strings.
# Fixes: D5-01/02/03/07/10 (data integrity), R6-03/12 (reliability),
#        S9-02/03 (security), C12-08 (configuration), D2-07 (unresolved policy).
# =============================================================================


class StringDownloadError(DrugOSDataError):
    """Raised when the STRING PPI file cannot be downloaded.

    Distinct from ``StringParseError`` (the file is downloaded but unreadable)
    and ``StringDataIntegrityError`` (the file parses but is corrupt).

    Typical causes:
      * URL is not in ``ALLOWED_STRING_URLS`` (SSRF guard — S9-02).
      * URL scheme is not HTTPS (S9-01).
      * Network timeout after all retries exhausted (R6-01).
      * HTTP 4xx/5xx response from string-db.org.
      * Downloaded file size is below ``STRING_MIN_VALID_SIZE_BYTES``
        (likely an HTML error page — R6-03).
      * Gzip magic bytes missing (``\\x1f\\x8b``) — R6-03.
      * Circuit breaker is open after 5 consecutive failures (R6-12).

    Fixes: S9-01 (TLS), S9-02 (URL allowlist), R6-01 (retry),
           R6-03 (BadGzipFile), R6-12 (circuit breaker).
    """


class StringParseError(DrugOSDataError):
    """Raised when the downloaded STRING file cannot be parsed.

    Distinct from ``StringDownloadError`` (transport problem) and
    ``StringDataIntegrityError`` (content problem).

    Typical causes:
      * File is not valid gzip (``gzip.BadGzipFile``).
      * Required columns are missing (``_validate_columns`` — D5-04).
      * File encoding is not UTF-8 / UTF-8-BOM (D5-05).
      * Pandas ``pd.read_csv`` raises on malformed whitespace-separated row.

    Fixes: D5-04 (column validation), D5-05 (dtype/schema enforcement),
           R6-03 (BadGzipFile propagation), R6-07 (parse DLQ).
    """


class StringDataIntegrityError(DrugOSDataError):
    """Raised when parsed STRING data fails a data-quality guard.

    Distinct from ``StringParseError`` (a *format* problem): this exception
    means the file parsed but the *content* is wrong.

    Typical causes:
      * SHA-256 of the downloaded file does not match the pinned value (D5-01).
      * File size is outside [0.5x, 2.0x] expected (D5-02).
      * Row count is outside [0.5x, 2.0x] expected (D5-03).
      * Required columns missing or unexpected columns present (D5-04 / D2-08).
      * Score is outside [0, 1000] range after filtering (S3-11 / D5-07).
      * Crosswalk is a dict instead of an IDCrosswalk instance (C14-06).
      * ``score_threshold`` is outside [0, 1000] (S3-01).
      * ``unresolved_policy="raise"`` and an unresolved edge is encountered (D2-07).
      * Score distribution is implausible (mean outside expected band — S3-11).

    Fixes: S3-01/11 (scientific correctness), D5-01/02/03/04/07 (data quality),
           C14-06 (crosswalk type guard), D2-07/08 (design).
    """


class EdgeLoadMismatchError(DrugOSDataError):
    """Raised when Neo4j load drops a significant fraction of edges.

    Generic base for all edge-load-mismatch errors. Source-specific
    subclasses (StringEdgeLoadMismatchError, StitchEdgeLoadMismatchError,
    etc.) inherit from this for callers who want to catch any mismatch.

    If more than 5% of edges in a batch are silently dropped (src or dst
    node not found in graph), this exception is raised. The missing edges
    represent lost biological signal that would corrupt the downstream
    Graph Transformer.

    Fixes: DQ-3 (silently dropped edges), L-1 (zero observability).
    """


class StringEdgeLoadMismatchError(EdgeLoadMismatchError):
    """Raised when Neo4j load drops edges silently (D5-10).

    The STRING loader emits ``N`` edge records; after the bulk load, the
    Neo4j ``LOAD`` reports ``M < N`` edges actually written. If
    ``M < N`` (and ``N > 0``), this exception is raised — the missing
    edges represent lost biological signal that would corrupt the
    downstream Graph Transformer.

    This is distinct from ``CriticalDataSourceError`` (which is raised
    when ``M == 0`` — total load failure).

    Fixes: D5-10 (referential integrity), D5-13 (cross-source check).
    """


class CircuitBreakerOpenError(DrugOSDataError):
    """Raised when the download circuit breaker is open (R6-12).

    The circuit breaker trips after ``CIRCUIT_BREAKER_FAILURE_THRESHOLD``
    (default 5) consecutive download failures and stays open for
    ``CIRCUIT_BREAKER_COOLDOWN_SECONDS`` (default 3600). While open, any
    call to ``download_string`` raises this exception immediately
    without attempting a network call.

    The breaker auto-resets after the cooldown period elapses.

    Fixes: R6-12 (circuit breaker).
    """


class CriticalDataSourceError(DrugOSDataError):
    """Raised when a critical data source yields zero usable records (D5-10).

    STRING is listed in ``CRITICAL_SOURCES`` (config.py). If a STRING load
    produces 0 edges (after filtering, crosswalk, deduplication), the
    downstream Graph Transformer has no PPI edges to message-pass over,
    and the V1 launch criterion of AUC > 0.85 is unreachable. This
    exception makes the failure LOUD (master prompt Rule R5: no silent
    failures) so that the pipeline fails fast rather than shipping a
    degraded model.

    Distinct from ``StringEdgeLoadMismatchError`` (some edges loaded, some
    dropped) — this exception means **zero** edges were loaded.

    Fixes: D5-10 (zero-edge guard), R6-04 (STRING_REQUIRED flag),
           master prompt Rule R5 (no silent failures).
    """


class SecurityError(DrugOSDataError):
    """Raised when a security guard rejects an input (S9-02 / S9-03).

    Typical causes:
      * URL is not in ``ALLOWED_STRING_URLS`` (SSRF guard — S9-02).
      * URL scheme is not HTTPS (S9-01).
      * Filename contains path-traversal characters (``..``, ``/``, ``\\`` — S9-03).
      * Filename does not end in ``.gz`` (S9-03).

    These failures are NOT retried — a security violation indicates a
    configuration error or an attacker, not a transient network issue.

    Fixes: S9-02 (URL allowlist), S9-03 (path traversal), S9-06 (URL
           sanitisation in logs).
    """


class ConfigurationError(DrugOSDataError):
    """Raised when STRING configuration is invalid (C12-08).

    Typical causes:
      * ``DATA_SOURCES["string"]`` is missing required keys.
      * URL is not HTTPS / not in allowlist.
      * Filename does not end in ``.gz``.
      * ``expected_record_count`` is not a positive integer.
      * ``max_size_bytes`` is not a positive integer.
      * ``DRUGOS_STRING_CONFIG`` env var points to a YAML file that
        cannot be parsed (C12-12).
      * ``score_threshold`` env var is not an integer in [0, 1000].

    Fixes: C12-08 (config validation), C12-12 (YAML config loading).
    """


# =============================================================================
# STITCH exceptions — added by stitch_loader v1.1.0 institutional-grade audit fix
# (master_prompt_fix_stitch_loader.md — Sections 3, 9).
#
# These six exception types extend the DrugOSDataError hierarchy with
# STITCH-specific failures. They follow the same ``context`` kwarg pattern
# as every other loader exception so that the dead-letter writer and the
# structured logger can serialise them without re-formatting strings.
#
# IMPORTANT catch-granularity design (master prompt Section 0, R7):
#   * STITCH exceptions are SIBLINGS of the STRING exceptions — they do NOT
#     subclass String*Error. This means ``except StringDownloadError`` will
#     NOT catch ``StitchDownloadError``. Callers wanting to catch any
#     loader failure should use ``except DrugOSDataError``.
#   * ``StitchParseError`` MULTIPLE-INHERITS from ``DrugOSDataError`` AND
#     ``FileNotFoundError`` so existing ``except FileNotFoundError`` blocks
#     in callers continue to work (Rule R3 — backward compat).
#
# Patient-safety doctrine: STITCH contributes ~20M Compound→Protein edges.
# If this loader emits a wrong CID, wrong protein, wrong organism, or wrong
# stereochemistry (CIDm vs CIDs), the Graph Transformer learns garbage
# associations and the RL ranker recommends the wrong drug to a clinician.
# These exceptions MUST NOT be silenced — every ``except
# StitchDataIntegrityError`` block must either re-raise or fail the
# pipeline loudly (Rule R5: no silent failures).
#
# Fixes: BUG-6.1, BUG-6.2, BUG-6.3, BUG-5.2, BUG-5.3, BUG-7.2, BUG-3.3,
#        BUG-2.5, BUG-9.1, GAP-9.4, BUG-14.4, GAP-12.4, BUG-12.1, BUG-15.1.
# =============================================================================


class StitchDownloadError(DrugOSDataError):
    """Raised when the STITCH CPI file cannot be downloaded.

    Subclasses ``DrugOSDataError`` (NOT ``StringDownloadError`` — siblings
    for catch granularity, master prompt R7).

    Distinct from ``StitchParseError`` (the file is downloaded but unreadable)
    and ``StitchDataIntegrityError`` (the file parses but is corrupt).

    Typical causes (per master_prompt_fix_stitch_loader.md):
      * URL is not in ``ALLOWED_STITCH_URLS`` (SSRF guard — BUG-9.1).
      * URL scheme is not HTTPS (BUG-9.1).
      * Network timeout after all retries exhausted (BUG-6.1).
      * HTTP 4xx/5xx response from stitch.embl.de.
      * Downloaded file size is below ``STITCH_MIN_VALID_SIZE_BYTES``
        (likely an HTML error page — BUG-6.1).
      * Gzip magic bytes missing (``\\x1f\\x8b``) — BUG-6.1.

    Fixes: BUG-6.1 (retry/timeout/atomic download), BUG-9.1 (URL allowlist),
           BUG-9.2 (TLS).
    """


class StitchParseError(DrugOSDataError, FileNotFoundError):
    """Raised when the downloaded STITCH file cannot be parsed.

    Subclasses BOTH ``DrugOSDataError`` AND ``FileNotFoundError`` (multiple
    inheritance) so existing ``except FileNotFoundError`` blocks in callers
    continue to work (Rule R3 — backward compat). This is critical because
    the v0 ``parse_stitch_interactions`` raised ``FileNotFoundError`` directly.

    Distinct from ``StitchDownloadError`` (transport problem) and
    ``StitchDataIntegrityError`` (content problem).

    Typical causes:
      * File is not valid gzip (``gzip.BadGzipFile``) — BUG-6.3.
      * File is missing on disk — BUG-6.2 (replaces raw FileNotFoundError).
      * Required columns are missing (``_validate_columns`` — BUG-5.3).
      * File encoding is not UTF-8 / UTF-8-BOM (GAP-15.4).
      * Pandas ``pd.read_csv`` raises ``ParserError`` on malformed TSV — BUG-6.3.
      * STITCH file version mismatch (GAP-15.3).

    Fixes: BUG-5.3 (column validation), BUG-6.2 (FileNotFoundError compat),
           BUG-6.3 (BadGzipFile propagation), GAP-15.4 (encoding).
    """


class StitchDataIntegrityError(DrugOSDataError):
    """Raised when parsed STITCH data fails a data-quality guard.

    Distinct from ``StitchParseError`` (a *format* problem): this exception
    means the file parsed fine but the *content* is wrong.

    Typical causes (per master_prompt_fix_stitch_loader.md):
      * SHA-256 of the downloaded file does not match the pinned value
        (BUG-7.2 — possible MITM or S3 corruption).
      * File size is outside [0.5x, 2.0x] expected (BUG-5.2).
      * Row count is outside [0.5x, 2.0x] expected (BUG-5.2).
      * Score is outside [0, 1000] range after filtering (BUG-3.3).
      * Score column is missing entirely (BUG-4.3, BUG-11.5).
      * (src_type, rel_type, dst_type) not in CORE_EDGE_TYPES (GAP-14.4).
      * CID is outside PubChem range [1, 370M] (GAP-3.6).
      * STITCH file is severely stale (GAP-5.6 — > 2x expected update frequency).
      * ``unresolved_policy="raise"`` and an unresolved edge is encountered
        (BUG-2.3).

    Fixes: BUG-3.3 (score scale), BUG-5.2 (row count), BUG-7.2 (checksum),
           BUG-4.3 (score column guard), BUG-11.5 (no silent skip),
           BUG-2.3 (unresolved policy), BUG-2.5 (action map validation),
           GAP-14.4 (CORE_EDGE_TYPES validation), GAP-3.6 (CID range),
           GAP-5.6 (freshness).
    """


class StitchEdgeLoadMismatchError(EdgeLoadMismatchError):
    """Raised when Neo4j load drops STITCH edges silently.

    The STITCH loader emits ``N`` edge records; after the bulk load, the
    Neo4j ``LOAD`` reports ``M < N`` edges actually written. If
    ``M < N`` (and ``N > 0``), this exception is raised — the missing
    edges represent lost biological signal that would corrupt the
    downstream Graph Transformer.

    This is distinct from ``CriticalDataSourceError`` (which is raised
    when ``M == 0`` — total load failure).

    Fixes: BUG-15.1 (kg_builder contract + mismatch raise).
    """


class StitchSecurityError(DrugOSDataError):
    """Raised when a security guard rejects a STITCH input.

    Typical causes:
      * URL is not in ``ALLOWED_STITCH_URLS`` (SSRF guard — BUG-9.1).
      * URL scheme is not HTTPS (BUG-9.1).
      * URL resolves to a private/internal IP (SSRF — BUG-9.1).
      * URL contains embedded credentials (``@`` — BUG-9.1).
      * Filename contains path-traversal characters
        (``..``, ``/``, ``\\``, null bytes — GAP-9.4).
      * Filename does not end in ``.gz`` (GAP-9.4).

    These failures are NOT retried — a security violation indicates a
    configuration error or an attacker, not a transient network issue.

    Fixes: BUG-9.1 (URL allowlist), GAP-9.4 (path traversal).
    """


class StitchConfigurationError(DrugOSDataError):
    """Raised when STITCH configuration is invalid.

    Distinct from ``StitchDataIntegrityError`` (a content problem): this
    exception means the *configuration* is wrong, not the data.

    Typical causes (per master_prompt_fix_stitch_loader.md):
      * ``DATA_SOURCES["stitch"]`` is missing required keys.
      * URL is not HTTPS / not in allowlist.
      * Filename does not end in ``.gz``.
      * ``expected_record_count`` is not a positive integer.
      * ``max_size_bytes`` is not a positive integer.
      * ``timeout_seconds`` is not a positive integer.
      * ``retry_count`` is not a non-negative integer.
      * ``score_threshold`` env var is not an integer in [0, 1000]
        (BUG-12.1).
      * ``DRUGOS_STITCH_CONFIG`` env var points to a YAML file that
        cannot be parsed (GAP-12.2).

    Fixes: GAP-12.4 (config validation), BUG-12.1 (threshold type/range).
    """


# =============================================================================
# SIDER exceptions — added by sider_loader v1.0.0 institutional-grade audit fix
# (master_prompt — Section 3 Phase 0.4, Domain 6 Reliability, Domain 9 Security).
#
# These six exception types extend the DrugOSDataError hierarchy with
# SIDER-specific failures. They follow the same ``context`` kwarg pattern
# as every other loader exception so that the dead-letter writer and the
# structured logger can serialise them without re-formatting strings.
#
# IMPORTANT catch-granularity design (mirrors STITCH/STRING siblings):
#   * SIDER exceptions are SIBLINGS of the STITCH/STRING exceptions — they
#     do NOT subclass Stitch*Error or String*Error. This means
#     ``except StitchDownloadError`` will NOT catch ``SiderDownloadError``.
#     Callers wanting to catch any loader failure should use
#     ``except DrugOSDataError``.
#   * ``SiderParseError`` MULTIPLE-INHERITS from ``DrugOSDataError`` AND
#     ``FileNotFoundError`` so existing ``except FileNotFoundError`` blocks
#     in callers continue to work (backward compat — Rule R3).
#
# Patient-safety doctrine: SIDER is the SOLE source of adverse-event data
# feeding the RL safety-signal dimension. If this loader emits a wrong CID,
# wrong UMLS CUI, or fails silently, the safety ranker will see zero
# adverse events for every drug and rank dangerous drugs as GREEN
# (recommend). These exceptions MUST NOT be silenced — every
# ``except SiderCriticalError`` block must either re-raise or fail the
# pipeline loudly (Rule R5: no silent failures).
#
# Fixes: Phase 0.4 (A1.1 — SIDER is critical), D6.3 (no graceful
#        degradation), D6.1/D6.2 (retry/timeout), D6.4 (parse errors),
#        D6.9 (HTTP errors), D6.12 (corrupt-file quarantine), D9.1/D9.2
#        (TLS / URL scheme), D2.13 (dual-write mutual exclusion), D5.1
#        (row-count guard), D15.10 (column-count guard).
# =============================================================================


class SiderCriticalError(DrugOSDataError):
    """Raised when SIDER (a CRITICAL data source) yields zero usable records.

    SIDER is listed in ``CRITICAL_SOURCES`` (config.py). If a SIDER load
    produces 0 rows (after parsing, before filtering) OR if the download
    fails after all retries, the downstream RL safety ranker has no
    adverse-event edges to aggregate onto Compound nodes — every drug
    would be ranked GREEN (recommend) by default. This exception makes the
    failure LOUD (master prompt Rule R5: no silent failures) so that the
    pipeline fails fast rather than shipping a deadly safety ranker.

    Distinct from ``SiderDownloadError`` (transport problem) and
    ``SiderParseError`` (format problem): this exception means the pipeline
    CANNOT continue safely without SIDER data.

    Typical causes (per master_prompt Phase 0.4 / A1.1 / G6):
      * ``parse_sider_side_effects`` produced 0 rows (empty file, all rows
        DLQ'd, or schema drift).
      * ``download_sider`` failed after all retries.
      * Row count is outside ``[EXPECTED_SIDER_ROW_COUNT_MIN,
        EXPECTED_SIDER_ROW_COUNT_MAX]`` (D5.1).

    Fixes: A1.1 (CRITICAL_SOURCES), D6.3 (no graceful degradation),
           G6 (RL ranker protected from "no adverse events = safe").
    """


class SiderDownloadError(DrugOSDataError):
    """Raised when the SIDER meddra_all_se.tsv.gz cannot be downloaded.

    Subclasses ``DrugOSDataError`` (NOT ``StitchDownloadError`` — siblings
    for catch granularity, master prompt R7).

    Distinct from ``SiderParseError`` (the file is downloaded but unreadable)
    and ``SiderCriticalError`` (the pipeline cannot continue safely).

    Typical causes (per master_prompt Domain 6 Reliability / Domain 9 Security):
      * Network/DNS failure after all retries exhausted (D6.1).
      * TLS certificate verification failure (D9.1).
      * URL scheme is not HTTPS (D9.2).
      * HTTP 4xx/5xx response from sideeffects.embl.de (D6.9).
      * Downloaded file size is below ``SIDER_MIN_VALID_SIZE_BYTES``
        (likely an HTML error page — D4.26).
      * Gzip magic bytes missing (``\\x1f\\x8b``) — D4.10.
      * Downloaded 0-byte file (D4.26).
      * sha256 mismatch with the pinned value (D4.19, D3.8).

    Fixes: D6.1 (retry/timeout/atomic download), D6.9 (HTTP error types),
           D9.1 (TLS), D9.2 (URL scheme), D4.10 (atomic write),
           D4.26 (zero-byte guard).
    """


class SiderParseError(DrugOSDataError, FileNotFoundError):
    """Raised when the downloaded SIDER file cannot be parsed.

    Subclasses BOTH ``DrugOSDataError`` AND ``FileNotFoundError`` (multiple
    inheritance) so existing ``except FileNotFoundError`` blocks in callers
    continue to work (Rule R3 — backward compat). This is critical because
    the v0 ``parse_sider_side_effects`` raised ``FileNotFoundError`` directly.

    Distinct from ``SiderDownloadError`` (transport problem) and
    ``SiderCriticalError`` (pipeline cannot continue).

    Typical causes (per master_prompt Domain 6 Reliability / Domain 15 Interop):
      * File is not valid gzip (``gzip.BadGzipFile``) — D6.4.
      * File is missing on disk — D6.4 (replaces raw FileNotFoundError).
      * Wrong column count (``SIDER_EXPECTED_COLUMN_COUNT = 6``) — D15.10.
      * File encoding is not UTF-8 / UTF-8-BOM (D4.14).
      * Pandas ``pd.read_csv`` raises ``ParserError`` on malformed TSV — D6.4.
      * SIDER file version mismatch (D12.6).

    Fixes: D6.4 (BadGzipFile/ParserError propagation), D15.10 (column count),
           D4.14 (encoding), D12.6 (version validation).
    """


class SiderDataQualityError(DrugOSDataError):
    """Raised when parsed SIDER data fails a data-quality guard.

    Distinct from ``SiderParseError`` (a *format* problem): this exception
    means the file parsed fine but the *content* is wrong.

    Typical causes (per master_prompt Domain 5 Data Quality):
      * Row count is outside ``[EXPECTED_SIDER_ROW_COUNT_MIN,
        EXPECTED_SIDER_ROW_COUNT_MAX]`` (D5.1).
      * CID is outside PubChem range ``[1, 370_000_000]`` (D3.12 / D5.13).
      * ``umls_id_meddra`` does not match ``^C\\d{7}$`` (D3.4 / D5.6).
      * ``side_effect_name`` is empty or a sentinel null (D3.9 / D5.7).
      * ``meddra_type`` is not in ``VALID_MEDDRA_TYPES`` (D2.12 / D5.5).
      * ``stitch_id_flat`` numeric portion != ``stitch_id_stereo`` numeric
        portion (D3.10 / D5.4 — same compound, different CID).

    Fixes: D5.1 (row count), D3.12/D5.13 (CID range), D3.4/D5.6 (UMLS),
           D3.9/D5.7 (side_effect_name), D2.12/D5.5 (meddra_type),
           D3.10/D5.4 (stitch ID consistency).
    """


class SiderSchemaError(DrugOSDataError):
    """Raised when the SIDER output schema is violated.

    Distinct from ``SiderParseError`` (input format) and
    ``SiderDataQualityError`` (input content): this exception means the
    loader's *output* schema is wrong — e.g. an emitted edge record is
    missing a required field, has the wrong type, or fails referential
    integrity with the node records.

    Typical causes (per master_prompt Domain 15 Interoperability):
      * Emitted edge record missing ``src_id``, ``dst_id``, ``src_type``,
        ``dst_type``, ``rel_type``, or ``props`` (D15.6).
      * ``src_type`` is not ``"Compound"`` (D15.8).
      * ``dst_type`` is not the canonical ``"MedDRA_Term"`` (Phase 0.3 / D15.11).
      * ``rel_type`` is not the canonical ``"causes_adverse_event"`` (Phase 0.3).
      * Edge references a ``dst_id`` that is not in the node set (D5.8 —
        referential integrity).

    Fixes: D15.6 (TypedDict contract), D15.8 (src_id int), D15.11 (dst_type),
           Phase 0.3 (canonical spelling), D5.8 (referential integrity).
    """


class SiderDualWriteError(DrugOSDataError):
    """Raised when both canonical and legacy SIDER edge emitters are called.

    SIDER has two edge emitters:
      * ``sider_to_edge_records`` — canonical (``causes_adverse_event``,
        ``MedDRA_Term``).
      * ``sider_to_legacy_edge_records`` — legacy (``causes_side_effect``,
        ``Side Effect``), kept for migration-period dual-write.

    Calling BOTH in the same process would emit every adverse-event edge
    twice (once canonical, once legacy) — the RL safety ranker would then
    double-count adverse events, marking safe drugs as RED (do not
    recommend). This exception makes the conflict LOUD (master prompt
    Rule R5: no silent failures, G13: dual-write protected).

    Typical causes (per master_prompt D2.13 / G13):
      * ``sider_to_edge_records(df)`` followed by
        ``sider_to_legacy_edge_records(df)`` in the same process.
      * ``sider_to_legacy_edge_records(df)`` followed by
        ``sider_to_edge_records(df)`` in the same process.

    Recovery: pick ONE emitter. New code SHOULD use ``sider_to_edge_records``.
    The legacy emitter is scheduled for removal in v2.0 (D2.10).

    Fixes: D2.13 (dual-write mutual exclusion), G13 (migration dual-write
           protected from double edges).
    """


# =============================================================================
# OpenTargets exceptions — added by opentargets_loader v2.0 institutional-grade
# audit fix (opentargets_loader_repair_prompt.md — Section 2.7).
#
# These seven exception types extend the DrugOSDataError hierarchy with
# OpenTargets-specific failures. They follow the same ``context`` kwarg
# pattern as every other loader exception so that the dead-letter writer and
# the structured logger can serialise them without re-formatting strings.
#
# IMPORTANT catch-granularity design (mirrors SIDER/STITCH/STRING siblings):
#   * OpenTargets exceptions are SIBLINGS of the SIDER/STITCH/STRING
#     exceptions — they do NOT subclass Sider*Error / Stitch*Error /
#     String*Error. This means ``except StitchDownloadError`` will NOT
#     catch ``OpenTargetsDownloadError``. Callers wanting to catch any
#     loader failure should use ``except DrugOSDataError``.
#   * ``OpenTargetsParseError`` MULTIPLE-INHERITS from ``DrugOSDataError``
#     AND ``FileNotFoundError`` so existing ``except FileNotFoundError``
#     blocks in callers continue to work (backward compat — Rule R3).
#
# Patient-safety doctrine: OpenTargets is the SOLE source of evidence-scored
# drug-target-disease triples feeding the Graph Transformer's confidence
# training objective. If this loader silently drops 100% of records
# (the v1 SCI-1 condition), the model trains on an empty OpenTargets
# signal — worse than no signal. These exceptions MUST NOT be silenced —
# every ``except OpenTargetsDataIntegrityError`` block must either re-raise
# or fail the pipeline loudly (Rule R5: no silent failures).
#
# Fixes: SCI-1 (parser fix), SCI-15 (0 records raises in CLINICAL),
#        REL-1/REL-2 (retry/timeout), REL-3 (atomic write),
#        REL-5 (dead-letter), REL-8 (typed exceptions),
#        SEC-1/SEC-2/SEC-3 (TLS/allowlist/path-traversal),
#        COMP-9 (typed exceptions for all failure modes).
# =============================================================================


class OpenTargetsDownloadError(DrugOSDataError):
    """Raised when the OpenTargets evidence JSONL cannot be downloaded.

    Subclasses ``DrugOSDataError`` (NOT ``StitchDownloadError`` /
    ``SiderDownloadError`` — siblings for catch granularity, master prompt R7).

    Distinct from ``OpenTargetsParseError`` (the file is downloaded but
    unreadable) and ``OpenTargetsDataIntegrityError`` (the pipeline cannot
    continue safely).

    Typical causes (per opentargets_loader_repair_prompt.md Domain 6 / Domain 9):
      * Network/DNS failure after all retries exhausted (REL-1).
      * TLS certificate verification failure (SEC-1).
      * URL scheme is not HTTPS (SEC-2).
      * URL not in ``ALLOWED_OPENTARGETS_URLS`` allowlist (SEC-2).
      * HTTP 4xx/5xx response from EBI FTP (REL-1).
      * Downloaded file size is below ``OPENTARGETS_MIN_VALID_SIZE_BYTES``
        (likely an HTML error page — DQ-3).
      * Gzip magic bytes missing (``\\x1f\\x8b``) — DQ-2.
      * Downloaded 0-byte file (DQ-2).
      * sha256 mismatch with the pinned value (DQ-1, DQ-14).
      * Path-traversal attempt in output filename (SEC-3).

    Fixes: REL-1 (retry/timeout/atomic download), REL-2 (HTTP error types),
           SEC-1 (TLS), SEC-2 (URL scheme + allowlist), SEC-3 (path traversal),
           DQ-1 (sha256), DQ-2 (size + magic), DQ-3 (content sniff).
    """


class OpenTargetsParseError(DrugOSDataError, FileNotFoundError):
    """Raised when the downloaded OpenTargets file cannot be parsed.

    Subclasses BOTH ``DrugOSDataError`` AND ``FileNotFoundError`` (multiple
    inheritance) so existing ``except FileNotFoundError`` blocks in callers
    continue to work (Rule R3 — backward compat). This is critical because
    the v1 ``parse_opentargets_evidence`` raised ``FileNotFoundError`` directly.

    Distinct from ``OpenTargetsDownloadError`` (transport problem) and
    ``OpenTargetsDataIntegrityError`` (pipeline cannot continue).

    Typical causes (per Domain 6 Reliability / Domain 15 Interop):
      * File is not valid gzip (``gzip.BadGzipFile``) — REL-4.
      * File is missing on disk — REL-4 (replaces raw FileNotFoundError).
      * Wrong JSON schema (e.g. nested ``entry["drug"]["id"]`` instead of flat
        ``entry["drugId"]`` — SCI-1, but this is now handled gracefully).
      * File encoding is not UTF-8 / UTF-8-BOM (COD-5).
      * Circuit breaker triggered (too many consecutive per-record failures —
        REL-9).

    Fixes: REL-4 (per-record error isolation + circuit breaker),
           REL-5 (dead-letter queue), COD-5 (BOM handling),
           SCI-1 (real flat schema).
    """


class OpenTargetsDataIntegrityError(DrugOSDataError):
    """Raised when parsed OpenTargets data fails a data-quality guard.

    Distinct from ``OpenTargetsParseError`` (a *format* problem): this
    exception means the file parsed fine but the *content* is wrong, OR
    the pipeline cannot continue safely without OpenTargets data.

    Typical causes (per Domain 5 Data Quality / Section 0.4 escalation):
      * 0 records parsed (empty file, all rows DLQ'd, or schema drift) — SCI-15.
      * <50% target resolution rate (crosswalk not loaded) — Section 0.4.
      * <expected_record_count × 0.5 records kept (truncated download) — DQ-1.
      * sha256/size mismatch with the pinned value — DQ-1, DQ-2.
      * Schema-version drift — DQ-13.
      * <90% target resolution rate in REGULATORY mode — Section 0.4.
      * Any non-human record detected in REGULATORY mode — Section 0.4.
      * Any ID failing format validation in REGULATORY mode — Section 0.4.

    Fixes: SCI-15 (0 records raises in CLINICAL), Section 0.4 (escalation),
           DQ-1/DQ-2/DQ-13 (integrity guards).
    """


class OpenTargetsSecurityError(DrugOSDataError):
    """Raised when the OpenTargets loader detects a security violation.

    Distinct from ``OpenTargetsDownloadError`` (transport problem): this
    exception means a security policy was violated, regardless of whether
    the download succeeded.

    Typical causes (per Domain 9 Security):
      * URL not in ``ALLOWED_OPENTARGETS_URLS`` allowlist (SEC-2).
      * URL scheme is not HTTPS (SEC-2).
      * URL contains embedded credentials (SEC-2).
      * Path-traversal attempt in output filename (SEC-3).
      * Output path resolves outside ``RAW_DIR`` (SEC-3).
      * Filename contains null bytes (SEC-3).
      * Filename does not end in ``.gz`` (SEC-3).

    Fixes: SEC-1 (TLS), SEC-2 (URL allowlist + scheme), SEC-3 (path traversal).
    """


class OpenTargetsConfigurationError(DrugOSDataError):
    """Raised when the OpenTargets loader configuration is invalid.

    Distinct from ``OpenTargetsDataIntegrityError`` (data problem): this
    exception means the *configuration* is wrong before any data is read.

    Typical causes (per Domain 12 Configuration):
      * ``OpenTargetsConfig.min_score`` not in [0, 1] (CONF-1).
      * ``OpenTargetsConfig.min_resolution_rate`` not in [0, 1] (CONF-1).
      * ``OpenTargetsConfig.organism_tax_id`` not positive (CONF-1).
      * ``OpenTargetsConfig.neo4j_batch_size`` not positive (CONF-1).
      * ``OpenTargetsConfig.progress_log_interval`` not positive (CONF-1).
      * ``OpenTargetsConfig.staleness_days`` not positive (CONF-1).
      * ``per_evidence_type_thresholds`` value not in [0, 1] (CONF-1).
      * Crosswalk object missing required methods (CONF-4).
      * ``DATA_SOURCES["opentargets"]`` missing required keys (CONF-1).

    Fixes: CONF-1 (config validation), CONF-4 (crosswalk contract).
    """


class OpenTargetsEdgeLoadMismatchError(EdgeLoadMismatchError):
    """Raised when the OpenTargets edge load drops edges unexpectedly.

    Distinct from ``OpenTargetsDataIntegrityError`` (data problem): this
    exception means the data was correct but the KG load dropped edges
    that should have been written.

    Typical causes (per Domain 5 Data Quality / Domain 15 Interop):
      * Neo4j ``load_edges_bulk_create`` reported fewer edges written than
        provided (D5-10).
      * Edge record schema mismatch with ``EDGE_PRODUCERS`` contract (ARCH-2).
      * Edge references a node ID not in the node set (D5-8 — referential
        integrity).

    Fixes: D5-10 (edge load mismatch), ARCH-2 (EDGE_PRODUCERS contract),
           D5-8 (referential integrity).
    """


class OpenTargetsSchemaError(DrugOSDataError):
    """Raised when the OpenTargets output schema is violated.

    Distinct from ``OpenTargetsParseError`` (input format) and
    ``OpenTargetsDataIntegrityError`` (input content): this exception means
    the loader's *output* schema is wrong — e.g. an emitted edge record is
    missing a required field, has the wrong type, or fails referential
    integrity with the node records.

    Typical causes (per Domain 15 Interoperability):
      * Emitted edge record missing ``src_id``, ``dst_id``, ``src_type``,
        ``dst_type``, ``rel_type``, or ``props`` (D15.6).
      * ``src_type`` is not ``"Compound"`` (D15.8).
      * ``rel_type`` is ``"indication"`` (FORBIDDEN — SCI-8).
      * Edge ``_provenance`` missing one of ``OPENTARGETS_PROVENANCE_KEYS``
        (LIN-1..5, COMP-2..5).
      * Edge missing ``_source``, ``_license``, ``_attribution``,
        ``_schema_version`` (COMP-2, COMP-3).

    Fixes: D15.6 (TypedDict contract), D15.8 (src_type Compound),
           SCI-8 (no "indication" label), LIN-1..5 / COMP-2..5 (provenance),
           ARCH-2 (EDGE_PRODUCERS contract).
    """


# =============================================================================
# ClinicalTrials exceptions — added by clinicaltrials_loader v2.1.0
# institutional-grade audit fix (PROMPT_fix_clinicaltrials_loader.md —
# 148 findings across 16 domains).
#
# These seven exception types extend the DrugOSDataError hierarchy with
# ClinicalTrials.gov / AACT-specific failures. They follow the same
# ``context`` kwarg pattern as every other loader exception so that the
# dead-letter writer and the structured logger can serialise them without
# re-formatting strings.
#
# IMPORTANT catch-granularity design (mirrors OpenTargets/SIDER/STITCH/STRING
# siblings):
#   * ClinicalTrials exceptions are SIBLINGS of the OpenTargets / SIDER /
#     STITCH / STRING exceptions — they do NOT subclass any sibling. This
#     means ``except OpenTargetsDownloadError`` will NOT catch
#     ``ClinicalTrialsDownloadError``. Callers wanting to catch any loader
#     failure should use ``except DrugOSDataError``.
#   * ``ClinicalTrialsParseError`` MULTIPLE-INHERITS from ``DrugOSDataError``
#     AND ``FileNotFoundError`` so existing ``except FileNotFoundError``
#     blocks in callers continue to work (backward compat — Rule R3).
#
# Patient-safety doctrine: ClinicalTrials.gov AACT is the SOLE source of
# clinical-trial evidence feeding the RL ranker's "has been tested in
# humans" dimension. A silently fabricated ``Warfarin -> Disease X`` edge
# (because Warfarin was the *comparator* arm, not the experimental arm)
# teaches the ranker that Warfarin treats X. A clinician who trusts that
# ranker can prescribe Warfarin off-label to a patient for whom it is
# contraindicated — THAT PATIENT CAN DIE. These exceptions MUST NOT be
# silenced — every ``except ClinicalTrialsDataIntegrityError`` block must
# either re-raise or fail the pipeline loudly (Rule R5: no silent failures).
#
# Fixes: PROMPT_fix_clinicaltrials_loader.md Issues 1.1 (Loader Protocol),
#        6.1-6.11 (Reliability), 9.1-9.10 (Security),
#        11.5 (error context), 12.7 (config validation).
# =============================================================================


class ClinicalTrialsDownloadError(DrugOSDataError):
    """Raised when the AACT sqlite snapshot cannot be downloaded.

    Subclasses ``DrugOSDataError`` (NOT ``OpenTargetsDownloadError`` /
    ``SiderDownloadError`` — siblings for catch granularity, master prompt R7).

    Distinct from ``ClinicalTrialsParseError`` (the file is downloaded but
    unreadable) and ``ClinicalTrialsDataIntegrityError`` (the pipeline cannot
    continue safely).

    Typical causes (per PROMPT_fix_clinicaltrials_loader.md Domain 6 / Domain 9):
      * Network/DNS failure after all retries exhausted (Issue 6.1).
      * TLS certificate verification failure (Issue 9.1).
      * URL scheme is not HTTPS (Issue 9.1).
      * HTTP 4xx/5xx response from AACT server (Issue 4.2, 6.1).
      * Downloaded file size exceeds ``max_size_bytes`` (Issue 12.4).
      * ZIP magic bytes missing (likely HTML error page) (Issue 4.9, 6.8).
      * SHA-256 mismatch with the pinned value (Issue 6.10, 12.5).
      * Path-traversal attempt in output filename (Issue 4.3, 9.3).
      * Circuit breaker tripped (Issue 6.11).

    Fixes: Issues 4.2 (urlopen+Request), 6.1 (retry), 6.2 (timeout),
           6.3 (atomic write), 6.4 (allow_stale), 6.10 (checksum),
           6.11 (circuit breaker), 9.1 (TLS), 12.4 (max_size_bytes),
           12.5 (checksum), 4.9 (corrupt zip sniff).
    """


class ClinicalTrialsParseError(DrugOSDataError, FileNotFoundError):
    """Raised when the downloaded AACT sqlite file cannot be parsed.

    Subclasses BOTH ``DrugOSDataError`` AND ``FileNotFoundError`` (multiple
    inheritance) so existing ``except FileNotFoundError`` blocks in callers
    continue to work (Rule R3 — backward compat). This is critical because
    the v1 ``parse_clinicaltrials`` raised ``FileNotFoundError`` directly.

    Distinct from ``ClinicalTrialsDownloadError`` (transport problem) and
    ``ClinicalTrialsDataIntegrityError`` (pipeline cannot continue).

    Typical causes (per Domain 6 Reliability / Domain 15 Interop):
      * No ``.db`` file found in ``ct_dir`` after extraction (Issue 4.5, 11.9).
      * DB is not a valid sqlite file (Issue 4.5, 9.5).
      * DB is missing one of the required AACT tables (``studies``,
        ``interventions``, ``conditions``, ``designs``) (Issue 4.5, 9.5).
      * Unrecognized AACT schema — neither ``interventions_mesh_terms`` table
        nor ``mesh_term`` column on ``interventions`` (Issue 3.1, C1).
      * SQL query fails (Issue 11.5 — re-raised with context).
      * Zip extraction interrupted — sentinel file missing (Issue 4.8, 6.9).

    Fixes: Issues 3.1 (schema detection), 4.1 (try/finally sqlite),
           4.4 (read-only sqlite), 4.5 (validate AACT DB),
           4.8 (extraction sentinel), 6.9 (partial extraction),
           9.4 (read-only sqlite), 9.5 (DB validation),
           11.5 (error context), 11.9 (log before raise).
    """


class ClinicalTrialsDataIntegrityError(DrugOSDataError):
    """Raised when parsed ClinicalTrials data fails a data-quality guard.

    Distinct from ``ClinicalTrialsParseError`` (a *format* problem): this
    exception means the file parsed fine but the *content* is wrong, OR
    the pipeline cannot continue safely without ClinicalTrials data.

    Typical causes (per Domain 5 Data Quality):
      * Null or empty ``nct_id`` in a row (Issue 5.1, C10).
      * NCT ID fails ``^NCT\\d{8}$`` format validation (Issue 3.15, 14.8).
      * Row count deviates from ``expected_record_count`` by >50%
        (Issue 5.9, 12.4).
      * Garbage MeSH term in src_id or dst_id (Issue 5.11).
      * Empty ``src_id`` or ``dst_id`` after fallback chain (Issue 4.7, C10).
      * Phase value not in ``_VALID_PHASES`` controlled vocabulary
        (Issue 2.10, 5.5).
      * ``phases`` parameter contains LIKE wildcards (``%`` or ``_``)
        (Issue 9.7).
      * ``enrollment`` < 30 in a Phase 3 trial (Issue 3.6 — suspect trial).

    Fixes: Issues 4.7 (empty IDs), 5.1 (null nct_id), 5.5 (controlled vocab),
           5.9 (expected count), 5.11 (garbage MeSH), 3.6 (enrollment),
           3.15 (NCT format), 9.7 (LIKE injection), 14.8 (NCT format).
    """


class ClinicalTrialsSecurityError(DrugOSDataError):
    """Raised when the ClinicalTrials loader detects a security violation.

    Distinct from ``ClinicalTrialsDownloadError`` (transport problem): this
    exception means a security policy was violated, regardless of whether
    the download succeeded.

    Typical causes (per Domain 9 Security):
      * URL not in ``ALLOWED_CLINICALTRIALS_URLS`` allowlist (Issue 9.1).
      * URL scheme is not HTTPS (Issue 9.1).
      * URL contains embedded credentials (Issue 9.1).
      * Path-traversal attempt (zip-slip) in extracted filename
        (Issue 4.3, 9.3).
      * Output path resolves outside ``RAW_DIR`` (Issue 4.3, 9.3).
      * Filename contains null bytes (Issue 4.3).
      * Suspected secret in edge props (Issue 9.10).
      * Extracted DB file permissions too open (Issue 9.8).

    Fixes: Issues 4.3 (safe extract), 9.1 (TLS+allowlist), 9.3 (zip-slip),
           9.8 (file perms), 9.9 (log sanitization), 9.10 (secret scanning).
    """


class ClinicalTrialsConfigurationError(DrugOSDataError):
    """Raised when the ClinicalTrials loader configuration is invalid.

    Distinct from ``ClinicalTrialsDataIntegrityError`` (data problem): this
    exception means the *configuration* is wrong before any data is read.

    Typical causes (per Domain 12 Configuration):
      * ``phases`` is an empty tuple/list (Issue 2.10, 4.6).
      * ``phases`` contains a LIKE wildcard (Issue 9.7).
      * ``phases`` value not in ``_VALID_PHASES`` (Issue 2.10, 5.5).
      * ``intervention_types`` is empty (Issue 2.7).
      * ``intervention_types`` value not in allowed set (Issue 2.7).
      * ``study_types`` is empty (Issue 3.7).
      * ``allowed_statuses`` is empty (Issue 3.11).
      * ``min_enrollment`` is negative (Issue 3.6).
      * ``max_trial_age_years`` is non-positive when set (Issue 3.13).
      * ``chunksize`` is non-positive (Issue 8.1).
      * ``limit`` is non-positive when set (Issue 8.5).
      * ``DATA_SOURCES["clinicaltrials"]`` missing required keys (Issue 12.7).
      * ``cfg["url"]`` is not HTTPS (Issue 12.7).
      * ``cfg["retry_count"]`` < 0 (Issue 12.7).
      * ``cfg["timeout_seconds"]`` <= 0 (Issue 12.7).
      * ``pinned_aact_release`` set but file not found (Issue 7.8).

    Fixes: Issues 2.7 (intervention_type config), 2.10 (phase validation),
           3.6 (min_enrollment), 3.7 (study_types), 3.11 (allowed_statuses),
           3.13 (max_trial_age_years), 7.8 (pinned release), 8.1 (chunksize),
           8.5 (limit), 9.7 (LIKE injection), 12.7 (config validation).
    """


class ClinicalTrialsEdgeLoadMismatchError(EdgeLoadMismatchError):
    """Raised when the ClinicalTrials edge load drops edges unexpectedly.

    Distinct from ``ClinicalTrialsDataIntegrityError`` (data problem): this
    exception means the data was correct but the KG load dropped edges
    that should have been written.

    Typical causes (per Domain 5 Data Quality / Domain 15 Interop):
      * Neo4j ``load_edges_bulk_create`` reported fewer edges written than
        provided (Issue 5.4).
      * Edge record schema mismatch with ``EDGE_PRODUCERS`` contract
        (Issue 1.4, 14.1, 15.3).
      * Edge references a node ID not in the node set (Issue 5.4 —
        referential integrity; >50% orphan rate triggers warning).

    Fixes: Issues 1.4 (EDGE_PRODUCERS contract), 5.4 (referential integrity),
           14.1 (rel_type matches schema registry), 15.3 (rel_type matches).
    """


class ClinicalTrialsSchemaError(DrugOSDataError):
    """Raised when the ClinicalTrials output schema is violated.

    Distinct from ``ClinicalTrialsParseError`` (input format) and
    ``ClinicalTrialsDataIntegrityError`` (input content): this exception means
    the loader's *output* schema is wrong — e.g. an emitted edge record is
    missing a required field, has the wrong type, or fails referential
    integrity with the node records.

    Typical causes (per Domain 15 Interoperability):
      * Emitted edge record missing ``src_id``, ``dst_id``, ``src_type``,
        ``dst_type``, ``rel_type``, or ``props`` (Issue 2.6).
      * ``src_type`` is not ``"Compound"`` (Issue 15.9).
      * ``dst_type`` is not ``"Disease"`` (Issue 15.9).
      * ``rel_type`` is ``"clinical_trial"`` (DEPRECATED — Issue 2.1).
      * ``rel_type`` is ``"treats"`` (FORBIDDEN — reserved for FDA-approved
        drugs from DrugBank; Issue 2.1, 14.1).
      * Edge ``_provenance`` missing one of ``CLINICALTRIALS_PROVENANCE_KEYS``
        (Issue 16.1-16.12).
      * Edge missing ``_source``, ``_license``, ``_attribution``,
        ``_schema_version`` (Issue 13.7, 13.8, 14.4, 14.5).

    Fixes: Issues 2.1 (rel_type), 2.6 (TypedDict contract),
           13.7 (license attribution), 13.8 (citation),
           14.1 (rel_type matches schema registry), 14.4 (license),
           14.5 (citation), 14.6 (schema versioning),
           15.3 (rel_type matches), 15.9 (Neo4j label compat),
           16.1-16.12 (lineage).
    """


# =============================================================================
# GEO exceptions — added by geo_loader v1.0.0 institutional-grade audit fix
# (GEO_LOADER_MASTER_REPAIR_PROMPT.md — 192 findings across 16 domains).
#
# These eight exception types extend the DrugOSDataError hierarchy with
# GEO (Gene Expression Omnibus)-specific failures. They follow the same
# ``context`` kwarg pattern as every other loader exception so the dead-
# letter writer and the structured logger can serialise them without
# re-formatting strings.
#
# IMPORTANT catch-granularity design (mirrors ClinicalTrials/OpenTargets/
# SIDER/STITCH/STRING siblings):
#   * GEO exceptions are SIBLINGS of the other loader exceptions — they
#     do NOT subclass any sibling. This means
#     ``except ClinicalTrialsDownloadError`` will NOT catch
#     ``GeoDownloadError``. Callers wanting to catch any loader failure
#     should use ``except DrugOSDataError``.
#   * ``GeoParseError`` MULTIPLE-INHERITS from ``DrugOSDataError`` AND
#     ``FileNotFoundError`` so existing ``except FileNotFoundError``
#     blocks in callers continue to work (backward compat — Rule R4).
#
# Patient-safety doctrine: GEO is the SOLE source of
# Protein→expressed_in→Anatomy edges in the KG. If this loader silently
# produces zero records, the KG lacks the entire tissue-specificity
# modality, the Graph Transformer cannot learn that a drug target is
# absent from the disease tissue, and a clinician can be handed a
# "high-confidence" repurposing candidate that will fail in Phase II —
# or harm a patient in a clinical-trial setting. These exceptions MUST
# NOT be silenced — every ``except GeoCriticalError`` block must either
# re-raise or fail the pipeline loudly (master prompt Rule R5).
#
# Fixes: GEO_LOADER_MASTER_REPAIR_PROMPT.md Section 6 (Phase 0.1, 0.5,
#        0.6, 0.7), Domain 6 (Reliability), Domain 9 (Security),
#        Domain 12 (Configuration).
# =============================================================================


class GeoConfigurationError(DrugOSDataError):
    """Raised when the GEO loader configuration is invalid or missing.

    Subclasses ``DrugOSDataError`` (NOT ``ClinicalTrialsConfigurationError``
    or any sibling — siblings for catch granularity, master prompt R7).

    Distinct from ``GeoDownloadError`` (transport problem) and
    ``GeoParseError`` (file format problem): this exception means the
    *configuration* is wrong BEFORE any data is read.

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Domain 12):
      * ``DATA_SOURCES["geo"]`` key missing entirely (GEO-12.7).
      * ``DATA_SOURCES["geo"]`` missing required keys: ``url``, ``filename``,
        ``version``, ``pinned``, ``release_date``, ``sha256``, ``md5``,
        ``license``, ``size_bytes``, ``max_size_bytes``,
        ``expected_record_count``, ``retry_count``, ``retry_backoff_seconds``,
        ``timeout_seconds``, ``url_scheme``, ``schema_version`` (GEO-12.12).
      * ``cfg.url`` does not start with ``https://`` (GEO-9.7, GEO-12.6).
      * ``cfg.timeout_seconds`` <= 0 (GEO-12.6).
      * ``cfg.retry_count`` < 0 (GEO-12.6).
      * ``cfg.retry_backoff_seconds`` < 0 (GEO-12.6).
      * ``cfg.max_size_bytes`` <= 0 (GEO-12.6).
      * ``cfg.expected_record_count`` <= 0 (GEO-12.6).
      * ``cfg.version`` does not match ``GSE\\d+`` (GEO-2.10, GEO-12.6).
      * ``cfg.schema_version`` is empty (GEO-12.6).
      * ``series_id`` does not match ``GSE\\d+`` (GEO-2.1, GEO-3.14,
        GEO-7.5, GEO-12.1 — Phase 0.4).
      * ``series_id`` does not match pinned ``cfg.version`` when
        ``cfg.pinned == True`` (GEO-15.4).
      * Cannot create ``RAW_DIR/geo/`` directory (PermissionError)
        (GEO-1.8, GEO-6.3).

    Fixes: GEO-1.8 (mkdir side effect), GEO-2.1 (default series_id),
           GEO-2.10 (schema version negotiation), GEO-3.14 (pinned series),
           GEO-4.4 (raise not return RAW_DIR), GEO-4.9 (explicit None check),
           GEO-7.5 (default series_id), GEO-9.7 (TLS mandatory),
           GEO-12.1 (no hardcoded GSE1), GEO-12.6 (config validation),
           GEO-12.7 (mandatory config), GEO-12.12 (missing keys),
           GEO-15.4 (version compat check).
    """


class GeoSecurityError(DrugOSDataError):
    """Raised when the GEO loader detects a security violation.

    Distinct from ``GeoDownloadError`` (transport problem): this exception
    means a security policy was violated, regardless of whether the
    download succeeded.

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Domain 9):
      * ``series_id`` contains path-traversal characters
        (e.g. ``../../../etc/passwd``) (GEO-9.2, Phase 0.7).
      * ``series_id`` contains null bytes (GEO-9.2).
      * ``series_id`` does not match ``GSE\\d+`` (also raises
        ``GeoConfigurationError``; this exception is raised when the
        failure is specifically a security-policy violation, e.g. an
        attacker-supplied value).
      * URL scheme is not HTTPS (GEO-9.7).
      * URL contains embedded credentials (GEO-9.7).
      * ``verify_tls=False`` was passed (TLS verification is mandatory;
        disabling it is a security violation) (GEO-9.7).
      * Sanitization detected null bytes / control characters in a SOFT
        metadata field (GEO-9.4).
      * Suspected secret in output record (GEO-9.10).

    Fixes: GEO-9.2 (path traversal), GEO-9.4 (input sanitization),
           GEO-9.7 (TLS mandatory), GEO-9.10 (sensitive data flag).
    """


class GeoDownloadError(DrugOSDataError):
    """Raised when the GEO SOFT file cannot be downloaded.

    Subclasses ``DrugOSDataError`` (NOT ``ClinicalTrialsDownloadError`` or
    any sibling — siblings for catch granularity, master prompt R7).

    Distinct from ``GeoDownloadRequiredError`` (auto-download disabled)
    and ``GeoParseError`` (file present but unreadable).

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Domain 6):
      * Network/DNS failure after all retries exhausted (GEO-6.1).
      * TLS certificate verification failure (GEO-9.7).
      * HTTP 4xx/5xx response from NCBI FTP server (GEO-6.1).
      * Downloaded file size exceeds ``max_size_bytes`` (GEO-5.4).
      * SHA-256 mismatch with the pinned value (GEO-5.3).
      * MD5 mismatch with the pinned value (GEO-5.3).
      * Partial download (``.part`` file detected and deleted)
        (GEO-6.5).
      * Circuit breaker tripped (5 consecutive failures)
        (GEO-6.10).
      * Timeout exceeded ``cfg.timeout_seconds`` (GEO-6.2).

    Fixes: GEO-1.4 (actual HTTP download), GEO-5.3 (checksum verify),
           GEO-5.4 (size guard), GEO-6.1 (retry with backoff),
           GEO-6.2 (timeout), GEO-6.5 (atomic write),
           GEO-6.10 (circuit breaker), GEO-9.7 (TLS).
    """


class GeoDownloadRequiredError(DrugOSDataError):
    """Raised when the GEO file is absent and auto-download is disabled.

    Distinct from ``GeoDownloadError`` (download was attempted and
    failed): this exception means the operator has not authorised
    automatic download (``GEO_AUTO_DOWNLOAD=0``, the default), and the
    file is not present on disk.

    The exception message instructs the operator to either:
      (a) download the file manually from the NCBI URL provided in
          ``context["manual_download_url"]`` and place it at
          ``context["expected_path"]``, OR
      (b) set ``GEO_AUTO_DOWNLOAD=1`` to enable automatic download.

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Phase 0.5):
      * First run with default ``GEO_AUTO_DOWNLOAD=0`` and no file
        present.
      * File was deleted between runs.
      * Wrong ``series_id`` requested (file for a different series is
        on disk).

    Fixes: GEO-1.5 (return contract), GEO-2.9 (encapsulate retrieval),
           GEO-4.4 (raise not return RAW_DIR), Phase 0.5 (return
           contract).
    """


class GeoParseError(DrugOSDataError, FileNotFoundError):
    """Raised when the downloaded GEO SOFT file cannot be parsed.

    Subclasses BOTH ``DrugOSDataError`` AND ``FileNotFoundError`` (multiple
    inheritance) so existing ``except FileNotFoundError`` blocks in callers
    continue to work (master prompt Rule R4 — backward compat). This is
    critical because the v0 ``parse_geo_series`` returned ``[]`` on a
    missing file; callers may have relied on FileNotFoundError-style
    handling.

    Distinct from ``GeoDownloadError`` (transport problem) and
    ``GeoDataQualityError`` (content problem): this exception means the
    file is present but its FORMAT is wrong.

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Domain 5 / 6):
      * File does not exist at the expected path (Phase 0.6).
      * Path is a directory, not a file (GEO-2.4).
      * File is not valid gzip (magic bytes mismatch) (GEO-5.3).
      * File is not valid UTF-8 even in degraded mode.
      * SOFT header missing ``^SERIES = <GSE...>`` line (GEO-5.7).
      * SOFT file schema version does not match config (GEO-2.10).
      * SOFT file ``!Series_status`` is ``Withdrawn`` or ``Superseded``
        (GEO-5.13).
      * Malformed-line count exceeds 1% of total lines (GEO-5.7).

    Fixes: GEO-2.4 (filepath default), GEO-2.10 (schema version check),
           GEO-5.3 (checksum), GEO-5.7 (SOFT schema validation),
           GEO-5.13 (withdrawn series), Phase 0.6 (raise not return []).
    """


class GeoDataQualityError(DrugOSDataError):
    """Raised when parsed GEO data fails a data-quality guard.

    Distinct from ``GeoParseError`` (a *format* problem): this exception
    means the file parsed fine but the *content* is wrong, OR the loader
    produced zero records when records were expected.

    Typical causes (per GEO_LOADER_MASTER_REPAIR_PROMPT.md Domain 5):
      * ``parse_geo_series`` produced zero records (Phase 0.6).
      * Record count < 0.5 * ``expected_record_count`` (GEO-5.5).
      * File size exceeds ``max_size_bytes`` (GEO-5.4).
      * Duplicate ``(probe_id, sample_id)`` pairs in raw records
        (GEO-5.1, GEO-5.11).
      * NaN in expression matrix when ``nan_strategy="drop"`` and the
        entire file is NaN (GEO-5.9).
      * Expression value is negative in raw_counts space (GEO-5.1).
      * ``sample_taxid`` is not a positive int (GEO-5.1).
      * ``series_id`` does not match ``GSE\\d+`` (GEO-5.1).
      * ``sample_id`` does not match ``GSM\\d+`` (GEO-5.1).
      * ``platform_id`` does not match ``GPL\\d+`` (GEO-5.1).
      * UBERON URI does not match the expected pattern (GEO-5.1).
      * ``gene_id`` does not resolve in
        ``VERIFIED_UNIPROT_GENE_CROSSWALK`` (GEO-5.1, referential
        integrity).
      * ``geo_to_edge_records`` received an empty ``records`` iterable
        (Phase 0.6).
      * Tissue cannot be mapped to UBERON and
        ``tissue_uberon_required=True`` (GEO-3.3).
      * Probe cannot be resolved to a gene ID (GEO-3.4) — dead-lettered
        by default; raises only if ALL probes fail.
      * Expression unit is unknown and cannot be normalized (GEO-3.9).
      * Memory budget exceeded (GEO-8.6).

    Fixes: GEO-3.3 (tissue mapping), GEO-3.4 (probe resolution),
           GEO-3.9 (unit normalization), GEO-5.1 (data quality checks),
           GEO-5.4 (size guard), GEO-5.5 (record count),
           GEO-5.9 (NaN handling), GEO-5.11 (deduplication),
           GEO-8.6 (memory budget), Phase 0.6 (raise not return []).
    """


class GeoCriticalError(DrugOSDataError):
    """Raised when GEO produces zero records AND ``GEO_REQUIRED=1``.

    This is the patient-safety-critical exception. It signals that the KG
    will lack the entire ``Protein→expressed_in→Anatomy`` modality, which
    means the Graph Transformer cannot learn tissue-specificity, which
    means a clinician may be handed a "high-confidence" repurposing
    candidate that targets a protein absent from the disease tissue.

    The pipeline's ``ON_SOURCE_FAILURE`` policy
    (``config.py:2144`` — ``DRUGOS_ON_SOURCE_FAILURE`` env var) governs
    the global behavior. When ``GEO_REQUIRED=1`` is set (operator opts
    into hard-fail mode), this exception MUST propagate to the pipeline
    root and terminate the run with a non-zero exit code.

    Distinct from ``GeoDataQualityError`` (raised on the same condition
    when ``GEO_REQUIRED=0``): this exception is reserved for the
    patient-safety-critical case where the operator has explicitly
    declared GEO as required.

    Typical causes:
      * ``parse_geo_series`` produced zero records AND
        ``GEO_REQUIRED=1`` (Phase 0.1, GUARD 3.10, GUARD 6.11,
        GUARD 7.11, GUARD 9.10, GUARD 10.11, GUARD 11.12, GUARD 15.12,
        GUARD 16.11).
      * ``download_geo`` failed AND ``GEO_REQUIRED=1`` (GEO-6.8).
      * Record count < 0.5 * expected AND ``GEO_REQUIRED=1`` (GEO-5.5).

    Fixes: GEO-5.5 (record count critical), GEO-6.8 (graceful
           degradation), Phase 0.1 (patient-safety GUARD findings).
    """


class GeoNotImplementedError(DrugOSDataError):
    """Raised when a GEO code path is genuinely not implemented.

    Distinct from ``GeoParseError`` (file format problem) and
    ``GeoDataQualityError`` (content problem): this exception means the
    *feature* is not yet supported, NOT that the data is bad.

    Per master prompt Rule R5, this exception replaces the v0 stub
    behavior of ``return []`` with ``logger.warning("not yet fully
    implemented")``. Silent stubs are FORBIDDEN; unimplemented code paths
    MUST raise.

    Typical causes:
      * ``parse_geo_series(format="miniml")`` — MINiML format support
        is planned for v1.1.0 (GEO-3.12).
      * ``parse_geo_series(format="series_matrix")`` — Series Matrix
        format support is planned for v1.1.0 (GEO-3.12).
      * ``GeoConfig(encrypt_outputs=True)`` — output encryption requires
        the ``cryptography`` package (GEO-9.8).
      * ``GeoConfig(verify_tls=False)`` — TLS verification is mandatory;
        disabling it raises ``GeoSecurityError``, NOT this exception
        (this is documented for clarity).

    Fixes: GEO-3.12 (MINiML/Series Matrix), GEO-9.8 (output encryption),
           Phase 0.6 (raise not return []), R5 (no silent stubs).
    """


# =============================================================================
# Entity-Resolver exceptions
# =============================================================================
# Added by entity_resolver v1.1.0 institutional-grade audit fix
# (ENTITY_RESOLVER_FIX_PROMPT.md -- Block A.1 / Section 0.3 / Block H).
#
# These five exception types cover the resolver's distinct failure modes:
#   * ResolverError             -- base class (catch-all for resolver bugs)
#   * ResolverConfigurationError -- bad config / bad constructor args
#   * ResolverConflictError      -- two mappings claim the same alias but
#                                   cannot be merged (e.g. same InChIKey
#                                   pointing to two different drugbank_ids
#                                   with conflicting metadata)
#   * ResolverDataQualityError   -- input data is malformed (missing columns,
#                                   bad types, schema drift). Distinct from
#                                   ConflictError because the resolver can
#                                   often recover (dead-letter + continue)
#                                   from data-quality issues but cannot
#                                   recover from conflicts.
#   * ResolverProvenanceError    -- an EntityMapping was constructed without
#                                   the mandatory Provenance block. This is
#                                   a regulatory non-compliance error
#                                   (D16-015): every mapping MUST be
#                                   traceable to its source, version,
#                                   license, and input checksum.
#
# All five subclass DrugOSDataError so callers can write
# ``except DrugOSDataError`` to catch any pipeline failure.
# =============================================================================


class ResolverError(DrugOSDataError):
    """Base class for all entity-resolver errors.

    Catch this when you want to handle any resolver failure (config,
    conflict, data-quality, or provenance) in one block. For finer
    granularity, catch the specific sub-types.

    Attributes
    ----------
    context : dict
        Structured key/value pairs (e.g. ``entity_type``, ``canonical_id``,
        ``id_system``, ``external_id_prefix``). Always present, possibly
        empty.

    Examples
    --------
    >>> try:
    ...     raise ResolverError("boom", context={"entity_type": "Compound"})
    ... except ResolverError as e:
    ...     print(e.context["entity_type"])
    Compound
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class ResolverConfigurationError(ResolverError):
    """Raised when the resolver is mis-configured.

    Typical causes:
      * ``EntityMapping(confidence=1.5)`` -- confidence must be in [0, 1]
        (D2-004 / D3-010).
      * ``EntityType.from_str("Foo")`` -- unknown entity type (D2-017).
      * ``EntityResolver(thresholds={"edge_dedup_early_reduction_threshold": -1})``
        -- negative threshold.
      * ``save_mappings(fmt="msgpack")`` -- unsupported serialization format.

    Fixes: D2-004, D2-017, D2-018, D4-006, D6-014, D9-007, D12-008.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class ResolverConflictError(ResolverError):
    """Raised when two EntityMappings claim the same canonical_id or alias
    but cannot be merged.

    This is distinct from ``ResolverDataQualityError``: a conflict means the
    data is well-formed but the resolver cannot decide which record is
    authoritative. The caller (often ``run_pipeline.py``) MUST either:

      1. Dead-letter BOTH records and continue (the safe choice).
      2. Halt the pipeline and require a human to triage.

    The default behaviour in ``EntityResolver`` is option 1 (dead-letter
    and continue) so the rest of the pipeline can make progress. The
    conflict is logged at WARNING and counted in
    ``stats["conflicts_detected"]``.

    Typical causes:
      * Two DrugBank records with the same InChIKey but different names
        and different ``atc_codes`` -- cannot pick a winner.
      * ``EntityMapping.merge(other)`` where ``self.canonical_id !=
        other.canonical_id`` (programmer error).

    Fixes: D3-015, D3-016, D5-004, D5-005, D2-011.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class ResolverDataQualityError(ResolverError):
    """Raised when input data is malformed and cannot be processed safely.

    The resolver can often recover from data-quality issues by dead-
    lettering the offending record and continuing. However, schema drift
    (e.g. an entire DataFrame missing a required column) is fatal: this
    exception is raised to halt the pipeline rather than silently emit
    zero records.

    Typical causes:
      * ``drug_records`` is missing the ``drugbank_id`` field entirely
        (D14-017 -- schema drift guard).
      * DRKG DataFrame missing ``head_type`` / ``head_id`` / ``tail_type``
        / ``tail_id`` columns (D6-001).
      * An alias value is neither ``str`` nor ``list[str]`` (D5-013).

    Fixes: D5-013, D5-017, D5-020, D5-021, D6-001, D14-017.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class ResolverProvenanceError(ResolverError):
    """Raised when an EntityMapping is constructed without a Provenance block.

    Provenance is MANDATORY (D16-015). Every mapping must carry:
      * ``_source``             -- e.g. "DrugBank", "DRKG", "UniProt"
      * ``_source_version``     -- e.g. "DrugBank 5.1.10"
      * ``_parsed_at``          -- ISO-8601 UTC timestamp
      * ``_parser_version``     -- e.g. "drugbank_parser:2.3.0"
      * ``_input_checksum``     -- SHA-256 of source record
      * ``_license``            -- e.g. "CC BY-NC 4.0"
      * ``_attribution``        -- citation string

    Without these, a downstream auditor cannot answer the question
    "where did this mapping come from?" -- which is a regulatory
    non-compliance (GDPR Article 5(2), FDA 21 CFR Part 11 audit trail).

    Typical causes:
      * ``EntityMapping(canonical_type=..., canonical_id=..., provenance=None)``
        (D16-015).
      * ``Provenance(_source="")`` -- empty source field (D16-003).
      * Loading a JSON file where the ``provenance`` block is missing
        (D7-010 / D7-012).

    Fixes: D16-001, D16-003, D16-004, D16-010, D16-011, D16-012,
           D16-013, D16-014, D16-015.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


# =============================================================================
# Evaluation exceptions — added by evaluation.py v2.0 audit fix
# (MASTER_REPAIR_PROMPT_evaluation.md — Domains 5, 6, 7, 9).
#
# Five exception types for the evaluation module. All subclass
# DrugOSDataError so callers can write ``except DrugOSDataError``
# to catch any pipeline failure. Each carries a structured
# ``context`` dict for serialisation.
#
#   * EvaluationError            -- base for all evaluation errors (D6-003).
#   * EvaluationInputError       -- bad/empty/NaN/Inf inputs (D5-001, E3-002).
#   * EvaluationIntegrityError   -- AUC out of range, data leakage,
#                                  sklearn/manual disagreement (D3, D5, D7).
#   * EvaluationReproducibilityError -- non-deterministic results (D7-001).
#   * EvaluationSecurityError    -- authorisation denied, PII in logs (D9).
# =============================================================================


class EvaluationError(DrugOSDataError):
    """Base class for all evaluation-pipeline errors.

    Subclasses DrugOSDataError so callers can write
    ``except DrugOSDataError`` to catch any pipeline failure while
    still letting unrelated bugs propagate.

    Carries a structured ``context`` dict (inherited from
    DrugOSDataError) so the dead-letter writer and structured logger
    can serialise it.

    Typical causes:
      * Any unexpected error inside compute_auc, precision_at_k,
        recall_at_k, mean_reciprocal_rank, hits_at_k, or
        evaluate_link_prediction (D6-003).

    Fixes: E6-003.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class EvaluationInputError(EvaluationError):
    """Raised when evaluation inputs fail validation.

    Distinct from EvaluationIntegrityError: this means the inputs
    are malformed (empty, wrong type, NaN/Inf, invalid k value)
    rather than the computation producing a wrong result.

    Typical causes:
      * Empty pos_scores or neg_scores (D5-001, E3-002).
      * NaN or Inf in score arrays (E3-003).
      * k < 1 (E10-002).
      * total_positives <= 0 in recall_at_k (E2-002).
      * Input is not 1-dimensional (E5-001).

    Fixes: E5-001, E3-002, E3-003, E10-002, E2-002.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class EvaluationIntegrityError(EvaluationError):
    """Raised when evaluation computation produces an invalid result.

    This means the inputs were valid but the OUTPUT is wrong --
    e.g. AUC outside [0, 1], sklearn and manual paths disagree,
    or positive/negative score arrays appear identical (data
    leakage).

    Typical causes:
      * Computed AUC is outside [0, 1] due to floating-point error
        (E6-002).
      * sklearn and manual AUC disagree beyond tolerance (E7-001).
      * Positive and negative score arrays have >50% overlap
        (E5-002 -- likely copy-paste bug).
      * Triple IDs appear in both positive and negative sets
        (E5-005).

    Fixes: E6-002, E7-001, E5-002, E5-005.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class EvaluationReproducibilityError(EvaluationError):
    """Raised when evaluation results are not reproducible.

    This is a CRITICAL error for clinical-grade runs. If the same
    input produces different outputs across runs, the evaluation
    cannot be trusted for model validation decisions.

    Typical causes:
      * sklearn and manual AUC paths produce different results
        (E7-001).
      * Random seed not fixed for stochastic components (E7-004).

    Fixes: E7-001, E7-004.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class EvaluationSecurityError(EvaluationError):
    """Raised when an evaluation security check fails.

    This is a guard-rail exception, not a full RBAC system.
    Production deployments MUST set DRUGOS_EVAL_USER and
    DRUGOS_EVAL_ROLE env vars, and the API layer (FastAPI, Phase 5)
    must propagate these from the authenticated session.

    Typical causes:
      * Entity ID has an unexpected type (string when int expected,
        or vice versa) without hashing enabled (E9-001).
      * DRUGOS_EVAL_ROLE is set to "read_only" and a compute
        operation is attempted (E9-004).

    Fixes: E9-001, E9-004.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


# ─── TransE Model Exceptions ─────────────────────────────────────────────
# Added by transe_model.py v2.2.1 institutional-grade repair.
# 16-domain forensic repair: 308 issues.
# These exceptions are specific to the TransE baseline training pipeline
# (Phase 3, Week 2) and are raised by drugos_graph/transe_model.py.

class TransETrainingError(DrugOSDataError):
    """Raised when a TransE training run fails.

    This is the BASE class for all TransE training errors. It captures
    the epoch, batch, and loss context so that the operator can identify
    exactly WHERE in the training loop the failure occurred.

    Typical causes:
      * NaN loss detected during training (R6.2).
      * Empty training triples provided (C4.10).
      * AUC below enforced threshold (I15.14).
      * Gradient explosion despite clipping (R6.3).

    Fixes: A1.10, R6.1, R6.2, R6.3, C4.10.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class TransEPredictionError(DrugOSDataError):
    """Raised when TransE prediction fails or produces invalid output.

    Typical causes:
      * Empty drug or disease index lists (D5.1).
      * Invalid relation index (D5.8).
      * Model not in eval mode when predict_drug_candidates called.
      * Contraindicated drug not properly filtered (K3.10).

    Fixes: A1.10, D5.1, D5.8, K3.10.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class TransEInitError(DrugOSDataError):
    """Raised when TransE model initialization fails.

    Typical causes:
      * Invalid embedding dimension (<=0 or too large for GPU).
      * num_entities or num_relations <= 0.
      * Pretrained embeddings dimension mismatch (I15.16).

    Fixes: A1.10, D2.3, I15.16.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class CheckpointIntegrityError(DrugOSDataError):
    """Raised when a saved TransE checkpoint fails integrity verification.

    Typical causes:
      * SHA-256 audit hash mismatch (file tampered or corrupted).
      * Missing required checkpoint keys (L16.1).
      * Schema version incompatibility (L16.2).
      * Config hash mismatch between checkpoint and caller (I7.10).

    Fixes: I7.8, I7.9, I7.10, L16.1, L16.2.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class DataLeakageError(DrugOSDataError):
    """Raised when data leakage is detected between train and test sets.

    In the TransE pipeline, this can occur when:
      * Negative samples overlap with held-out positive test triples (K3.2).
      * Validation triples appear in the training set (K3.6).
      * Known-triple filtering is not applied (K3.3).

    Fixes: K3.2, K3.3, K3.6, D5.11.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)


class UnknownLabelError(DrugOSDataError):
    """Raised when a node/edge label has no entry in ID_PATTERNS.

    v9 ROOT FIX (audit F7.8): the previous ``_validate_id`` returned True
    for any label not present in ID_PATTERNS — silently disabling
    validation for typo'd labels like 'MedDRATerm' (missing underscore)
    or 'Compoud' (misspelled Compound). Every ID was accepted. Now the
    function raises this exception so the caller MUST either fix the
    label or register the new label's pattern in ID_PATTERNS. Fail-closed
    is the only safe default for biomedical ID validation.

    Fixes: F7.8, D-15.
    """

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, context=context)
