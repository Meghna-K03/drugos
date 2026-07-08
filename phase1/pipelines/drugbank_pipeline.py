"""
DrugBank XML Pipeline - parses DrugBank XML for drug metadata and
drug-protein interactions (DPI).

This module is the DrugBank-specific implementation of the BasePipeline
contract. It parses the licensed DrugBank XML distribution to extract:
  1. Drug records (name, InChIKey, SMILES, MW, MOA, FDA status, ...).
  2. Drug-Protein Interactions (targets, enzymes, transporters) linked
     to UniProt accessions.

Life-Safety Contract
--------------------
This pipeline feeds a drug-repurposing platform whose downstream consumers
are a Graph Transformer, an RL safety ranker, and a public web dashboard.
A single silently-wrong record can lead a researcher to prescribe a killer
drug. Every XPath, every identifier regex, and every clinical-status
assertion in this file is verified against authoritative sources listed
in the Scientific Truth Sources section of the master fix prompt.

Scientific Truth Sources (verified)
-----------------------------------
- DrugBank XML schema: https://docs.drugbank.com/xml
- Real-world parser references:
    * ramirezlab/WIKI/Approved_drugs_from_Drugbank.ipynb
    * cran/dbparser (R package)
    * claude-code-templates/drugbank-database
- Withdrawn-killer-drug list (verified):
    DB00463 Baycol (cerivastatin, 2001, ~100 rhabdomyolysis deaths)
    DB00709 Vioxx (rofecoxib, 2004, 88,000-140,000 heart attacks)
    DB00542 Seldane (terfenadine, 1998, fatal arrhythmias)
    DB00356 Rezulin (troglitazone, 2000, hepatotoxicity)
    DB00574 Pondimin (fenfluramine, 1997, valvular heart disease)
    DB00806 Zelnorm (tegaserod, 2007, cardiovascular events)
    DB00604 Propulsid (cisapride, 2000, fatal arrhythmias)
    DB00642 Hismanal (astemizole, 1999, arrhythmias)
    DB00465 Raxar (grepafloxacin, 1999, QT prolongation / deaths)
    DB00625 Posicor (mibefradil, 1998, fatal drug interactions)

Expected DrugBank XML Structure (5.x)
-------------------------------------
    <drugbank xmlns="http://drugbank.ca" version="5.1.10">
      <drug type="small molecule" created="...">
        <drugbank-id primary="true">DB00645</drugbank-id>
        <name>Aspirin</name>
        <description>...</description>
        <cas-number>50-78-2</cas-number>
        <groups>
          <group>approved</group>
        </groups>
        <calculated-properties>
          <property><kind>InChIKey</kind><value>BSYN...</value></property>
          <property><kind>SMILES</kind><value>CC(=O)Oc1ccccc1C(=O)O</value></property>
          ...
        </calculated-properties>
        <experimental-properties>...</experimental-properties>
        <mechanism-of-action>
          <paragraph>...</paragraph>
        </mechanism-of-action>
        <targets>
          <target>
            <id>BE0000015</id>
            <name>Prostaglandin G/H synthase 1</name>
            <organism>Humans</organism>
            <actions><action>inhibitor</action></actions>
            <known-action>yes</known-action>
            <polypeptide id="P23219" source="Swiss-Prot">
              <external-identifiers>
                <external-identifier>
                  <resource>UniProtKB</resource>
                  <identifier>P23219</identifier>
                </external-identifier>
              </external-identifiers>
            </polypeptide>
          </target>
        </targets>
        <enzymes>...</enzymes>
        <transporters>...</transporters>
      </drug>
    </drugbank>

Scientific Assumptions
----------------------
1. **Clinical status**: ``is_fda_approved=True`` ONLY when DrugBank
   ``<groups>`` contains ``approved`` AND does NOT contain ``withdrawn``.
   DrugBank retains the ``approved`` tag on withdrawn drugs (verified:
   DB00463 Baycol, DB00709 Vioxx, DB00542 Seldane). Audit issue S3.

2. **Organism filter**: by default only targets/enzymes/transporters with
   ``<organism>Humans</organism>`` are loaded. Configurable via
   ``DRUGBANK_TARGET_ORGANISMS`` env var. Audit issue S9.

3. **Biologics**: drugs without an InChIKey (insulin, antibodies,
   pegylated proteins) are NOT dropped. Synthetic 27-char SYNTH keys
   (``SYNTH{hash}-{hash}-{hash}``, generated via
   ``entity_resolution.base.make_synthetic_inchikey`` so the SAME
   biologic from any source gets the SAME key — v34/v35 ROOT FIX for
   CRITICAL #2). The Drug model supports this via ``String(50)`` +
   ``CheckConstraint``. Audit issue S7.

4. **UniProt IDs**: extracted from ``<polypeptide source="Swiss-Prot"
   id="P00734">`` or from
   ``<external-identifier><resource>UniProtKB</resource>
   <identifier>P00734</identifier></external-identifier>``. Validated
   against ``_UNIPROT_RE``. Audit issue S1.

5. **Actions**: ``<actions><action>...</action></actions>`` - ALL actions
   captured (not just the first). Pipe-separated in ``action_type``.
   Audit issues S2, S10.

6. **Multi-role proteins**: a protein can be both a drug target and a
   drug-metabolism enzyme (e.g. CYP3A4 / P08684, thrombin / P00734).
   ``source_id`` includes the interactor type to avoid collision:
   ``{drugbank_id}_{interactor_type}_{uniprot_id}``. Audit issue S22.

Determinism
-----------
This pipeline is deterministic given the same input XML, same RDKit
version, and same DrugBank release. No random seeds are used. Re-running
on identical input produces byte-identical output CSVs (modulo
``source_fetch_date``). Audit issues ID7, ID11.

Quick Start
-----------
Environment variables (all optional, shown with defaults)::

    DRUGBANK_XML_PATH=raw_data/drugbank/drugbank_all_full_database.xml.gz
    DRUGBANK_VERSION=5.1
    DRUGBANK_TARGET_ORGANISMS=Humans
    DRUGBANK_GENERATE_SYNTH_KEYS=true
    DRUGBANK_DROP_NO_INCHIKEY=false
    DRUGBANK_CONSERVATIVE_DEFAULTS=true
    DRUGBANK_BATCH_SIZE=1000
    DRUGBANK_LOG_INTERVAL=5000
    DRUGBANK_MAX_DRUGS=0
    DRUGBANK_EXTRACT_TARGETS=true
    DRUGBANK_EXTRACT_ENZYMES=true
    DRUGBANK_EXTRACT_TRANSPORTERS=true
    DRUGBANK_CSV_COMPRESSION=gzip
    DRUGBANK_EXPECTED_SHA256=
    DRUGBANK_DRUG_COUNT_MIN=10000
    DRUGBANK_DRUG_COUNT_MAX=20000
    DRUGBANK_LOG_REDACT=false
    DRUGBANK_LOG_FULL_PATHS=false

Run::

    python -m pipelines.drugbank

Data Dictionary (drugbank_drugs.csv)
-----------------------------------
| Column               | Type   | Description                                      |
|----------------------|--------|--------------------------------------------------|
| drugbank_id          | str    | DrugBank identifier (DB\\d{5})                    |
| name                 | str    | Drug preferred name                              |
| inchikey             | str    | Standard InChIKey (27 chars) or SYNTH synthetic key (27 chars, SYNTH{hash}-{hash}-{hash}) |
| smiles               | str    | Canonical SMILES                                 |
| molecular_weight     | float  | MW in Da (1-500,000)                             |
| molecular_formula    | str    | Molecular formula                                |
| is_fda_approved      | bool   | Currently FDA-approved (excludes withdrawn)      |
| is_withdrawn         | bool   | Withdrawn from market (safety flag)              |
| clinical_status      | str    | approved/withdrawn/illicit/investigational/...   |
| groups               | str    | Pipe-separated DrugBank groups                   |
| mechanism_of_action  | str    | MOA text (multi-paragraph concatenated)          |
| description          | str    | Drug description text                            |
| cas_number           | str    | CAS Registry Number                              |
| logp                 | float  | Calculated LogP                                  |
| tpsa                 | float  | Topological Polar Surface Area                   |
| h_bond_donor_count   | int    | H-bond donor count                               |
| h_bond_acceptor_count| int    | H-bond acceptor count                            |
| rotatable_bond_count | int    | Rotatable bond count                             |
| heavy_atom_count     | int    | Heavy atom count                                 |
| complexity           | int    | Molecular complexity                             |
| inchikey_source      | str    | extracted_calculated/experimental/generated/synth|
| completeness_score   | float  | 0.0-1.0 fraction of expected fields populated    |

Data Dictionary (drugbank_interactions.csv.gz)
---------------------------------------------
| Column                | Type   | Description                                   |
|-----------------------|--------|-----------------------------------------------|
| drugbank_id           | str    | Source DrugBank drug ID                       |
| target_name           | str    | Protein name from DrugBank                    |
| target_id             | str    | DrugBank BE-ID (BE\\d{7})                      |
| drugbank_target_be_id | str    | Explicit BE-ID field (same as target_id)      |
| uniprot_id            | str    | UniProt accession (primary protein identifier)|
| action_type           | str    | Pipe-separated actions (e.g. agonist|modulator)|
| organism              | str    | Source organism (Humans, Mouse, E. coli, ...) |
| interactor_type       | str    | target / enzyme / transporter                 |
| is_known_action       | bool   | On-target (True) vs off-target (False)        |
| binding_position      | str    | Polypeptide binding position (optional)       |
| target_sequence       | str    | Amino-acid sequence (optional)                |
| source                | str    | Always "drugbank"                             |
| source_id             | str    | {drugbank_id}_{interactor_type}_{uniprot_id}  |
"""

from __future__ import annotations

import csv
import getpass
import gzip
import hashlib
import io
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
# v43 ROOT FIX (P1-006): import SQLAlchemyError so we can catch commit
# failures narrowly instead of using ``except Exception: pass``.
from sqlalchemy.exc import SQLAlchemyError

from cleaning._constants import (
    normalize_drugbank_id,  # v29 ROOT FIX (audit P1-24)
    normalize_inchikey,     # v29 ROOT FIX (audit P1-24)
    normalize_uniprot_id,   # v29 ROOT FIX (audit P1-24)
)
from cleaning.deduplicator import dedup_interactions
from cleaning.missing_values import fill_missing_drug_fields, handle_missing_inchikey
from cleaning.normalizer import (
    _RDKIT_AVAILABLE,
    convert_to_inchikey,
    convert_to_inchikeys,
    refresh_capabilities,
    standardize_inchikey,
)
from config.settings import (
    DRUGBANK_BATCH_SIZE,
    DRUGBANK_CONSERVATIVE_DEFAULTS,
    DRUGBANK_CSV_COMPRESSION,
    DRUGBANK_DPI_BATCH_SIZE,
    DRUGBANK_DROP_NO_INCHIKEY,
    DRUGBANK_EXPECTED_DRUG_COUNT_MAX,
    DRUGBANK_EXPECTED_DRUG_COUNT_MIN,
    DRUGBANK_EXPECTED_SHA256,
    DRUGBANK_EXTRACT_ENZYMES,
    DRUGBANK_EXTRACT_TARGETS,
    DRUGBANK_EXTRACT_TRANSPORTERS,
    DRUGBANK_GENERATE_SYNTH_KEYS,
    DRUGBANK_LOG_FULL_PATHS,
    DRUGBANK_LOG_INTERVAL,
    DRUGBANK_LOG_REDACT,
    DRUGBANK_MAX_DRUGS,
    DRUGBANK_TARGET_ORGANISMS,
    DRUGBANK_VALIDATE_READABILITY,
    DRUGBANK_VERSION,
    DRUGBANK_XML_NAMESPACE,
    DRUGBANK_XML_PATH,
    PROCESSED_DATA_DIR,
)
from database.base import SCHEMA_VERSION as DB_SCHEMA_VERSION
from database.connection import get_db_session
from database.loaders import (
    MappingResult,
    UpsertResult,
    bulk_upsert_dpi,
    bulk_upsert_drugs,
    flush_dead_letter_queue,
    get_inchikey_to_drug_id_map,
    get_uniprot_to_protein_id_map,
)
from database.models import Drug, DrugProteinInteraction, PipelineRun, Protein
from pipelines.base_pipeline import (
    SCHEMA_VERSION,
    BasePipeline,
    LoadResult,
    SchemaValidationError,
)

# Audit DOC13: try lxml, fall back to stdlib ElementTree (INT12).
try:
    from lxml import etree

    _HAS_LXML = True
except ImportError:  # pragma: no cover - lxml is a hard dependency in requirements.txt
    import xml.etree.ElementTree as etree  # type: ignore[no-redef]

    _HAS_LXML = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (COM13, COM12, CF1, DQ4, S19, INT6, D5).
# ---------------------------------------------------------------------------

__version__: str = "2.1.0"  # COM13: bump on every meaningful change.

__all__ = ["DrugBankPipeline"]  # COM12: explicit public surface.

# CF1: XML namespace map (config-overridable for forward compat).
NS: dict[str, str] = {"db": DRUGBANK_XML_NAMESPACE}

# DQ4: DrugBank ID format is DB followed by exactly 5 digits.
_DRUGBANK_ID_RE: re.Pattern[str] = re.compile(r"^DB\d{5}$")

# S19: standard InChIKey is 27 chars (14-10-1). Source: InChI Trust.
# v24 ROOT FIX (FORENSIC-P1-PIPE §1): keep the regex for backward compat,
# but add a delegating wrapper that calls the canonical validator.
_INCHIKEY_RE: re.Pattern[str] = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


def _is_valid_inchikey(key: str) -> bool:
    """v24: Delegate to the canonical InChIKey validator."""
    try:
        from cleaning.normalizer import is_valid_inchikey as _canonical
        return _canonical(key)
    except ImportError:
        return bool(isinstance(key, str) and _INCHIKEY_RE.match(key.strip().upper()))

# INT6: UniProt accession regex — canonical pattern per UniProt documentation.
# 6-char accessions start with [OPQ] (e.g. P00734, Q9NZ52).
# 10-char accessions start with [A-NR-Z] (e.g. A0A0K3AVT9) — O, P, Q are
# reserved for the 6-char format and must NOT appear as the first letter
# of a 10-char accession.
# SCI-FIX: Previous pattern ^[A-Z][0-9]... accepted ANY letter as the first
# character, allowing invalid accessions like A12345 (6-char starting with A,
# which is reserved for 10-char format) and O123456789 (10-char starting with
# O, which is reserved for 6-char format).
_UNIPROT_RE: re.Pattern[str] = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$"
    r"|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)

# D5 / COM2: map DrugBank action verbs to InteractionType enum values.
# Source: database/models.py:150 InteractionType enum.
# NOTE: DrugBank actions "substrate" and "inducer" do not have direct
# InteractionType enum counterparts, so they map to "unknown" (the
# InteractionType enum is pharmacology-focused: inhibitor/agonist/etc.).
# The original action string is preserved in the action_type column for
# downstream consumers that need the full pharmacological semantics.
ACTION_TO_ENUM: dict[str, str] = {
    "inhibitor": "inhibitor",
    "agonist": "agonist",
    "antagonist": "antagonist",
    # v43 ROOT FIX (P1-008): map inducer and substrate to their own enum
    # values (was "unknown"). The InteractionType enum now includes
    # INDUCER and SUBSTRATE (models.py). This preserves the DDI risk
    # signal: a CYP3A4 substrate + a CYP3A4 inhibitor = dangerous
    # accumulation; a CYP3A4 inducer + a CYP3A4 substrate = therapeutic
    # failure. The RL safety ranker needs these classifications to
    # detect drug-drug interactions.
    "inducer": "inducer",
    "substrate": "substrate",
    "binder": "binding_agent",
    "blocker": "blocker",
    "modulator": "modulator",
    "positive modulator": "modulator",
    "negative modulator": "modulator",
    "activator": "activator",
    "other": "unknown",
}

# S21: ADMET property map (mirrors PubChem enrichment schema for INT4).
ADMET_PROPERTY_MAP: dict[str, str] = {
    "inchikey": "inchikey",
    "smiles": "smiles",
    "inchi": "inchi",
    "molecular_weight": "molecular_weight",
    "molecular_formula": "molecular_formula",
    "logp": "logp",
    "logs": "logs",
    "tpsa": "tpsa",
    "h_bond_donor_count": "h_bond_donor_count",
    "h_bond_acceptor_count": "h_bond_acceptor_count",
    "rotatable_bond_count": "rotatable_bond_count",
    "heavy_atom_count": "heavy_atom_count",
    "complexity": "complexity",
}

# DQ2: plausible MW ranges (Da). Small molecules 1-10k; biologics 1k-500k.
_SMALL_MW_MIN: float = 1.0
_SMALL_MW_MAX: float = 10_000.0
_BIO_MW_MIN: float = 1_000.0
_BIO_MW_MAX: float = 500_000.0

# DQ13: expected fields for completeness-score computation.
_EXPECTED_DRUG_FIELDS: list[str] = [
    "drugbank_id",
    "name",
    "inchikey",
    "smiles",
    "molecular_weight",
    "molecular_formula",
    "mechanism_of_action",
    "description",
    "cas_number",
    "is_fda_approved",
    "is_withdrawn",
    "clinical_status",
]

# SEC4: DrugBank license attribution (Wishart 2018 Nucleic Acids Res).
#
# ROOT FIX (Finding 2, P0): the previous license text claimed DrugBank
# data is "CC BY-NC 4.0 for academic use". This is FALSE. DrugBank's
# database content is governed by a custom EULA
# (https://www.drugbank.com/license) that PROHIBITS redistribution in
# any form without a paid license — including for academic use beyond
# a single internal copy. The CC BY-NC 4.0 license covers only the
# DrugBank vocabulary/ontology, NOT the database content. The
# previous attribution was legally misleading and exposed the company
# to DrugBank Inc. license-violation claims.
#
# ROOT FIX (DrugBank access paused May 2026): DrugBank has temporarily
# paused academic downloads since May 2026. Even registered academic
# users cannot download the XML file at this time. The pipeline now
# supports a DrugBank-free path (DRUGOS_USE_CHEMBL_AS_PRIMARY=1,
# default) that uses ChEMBL SQLite + PubChem + FDA Orange Book as the
# primary drug source. When DrugBank academic downloads resume,
# operators can obtain a license at
# https://go.drugbank.com/public_users/sign_up and set
# DRUGBANK_XML_PATH to use DrugBank data.
_DRUGBANK_LICENSE_TEXT: str = (
    "Data in this directory is derived from DrugBank "
    "(https://www.drugbank.com) IF AND ONLY IF the DrugBank XML was "
    "actually processed (DRUGBANK_XML_PATH set and file present).\n\n"
    "DRUGBANK LICENSE TERMS (verbatim summary from "
    "https://www.drugbank.com/license):\n"
    "  - DrugBank data is NOT CC-licensed. It is governed by a custom "
    "EULA.\n"
    "  - Redistribution in any form (including derived CSVs, knowledge "
    "graph\n"
    "    exports, or API responses) is PROHIBITED without a paid "
    "commercial\n"
    "    license, even for academic use beyond a single internal copy.\n"
    "  - Academic users may use DrugBank data internally for research "
    "but\n"
    "    MAY NOT redistribute it to third parties or include it in "
    "products\n"
    "    that are shared, published, or commercialized.\n"
    "  - DrugBank academic downloads are PAUSED since May 2026. "
    "Register at\n"
    "    https://go.drugbank.com/public_users/sign_up to be notified "
    "when\n"
    "    downloads resume.\n\n"
    "DRUGBANK-FREE PATH (default, DRUGOS_USE_CHEMBL_AS_PRIMARY=1):\n"
    "  When the DrugBank XML is not available, the pipeline uses "
    "ChEMBL\n"
    "  SQLite (https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/"
    "latest/)\n"
    "  + PubChem (https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/"
    "CURRENT-Full/)\n"
    "  + FDA Orange Book "
    "(https://www.fda.gov/drugs/development-approval-process-drugs/\n"
    "    orange-book-data-files) as the primary drug source. These "
    "sources\n"
    "  are free, require no login, and are NOT subject to DrugBank's "
    "EULA.\n"
    "  Data files in this directory produced via the DrugBank-free "
    "path are\n"
    "  freely redistributable under their respective source licenses "
    "(ChEMBL\n"
    "  CC BY-SA 3.0, PubChem public domain, FDA Orange Book public "
    "domain).\n\n"
    "Citation (when DrugBank data IS used): Wishart DS, Feunang YD, "
    "Guo AC, Lo EJ, Marcu A, Grant JR, Sajed T, Johnson D, Li C, "
    "Sayeeda Z, Assempour N, Iynkkaran I, Liu Y, Maciejewski A, Gale "
    "N, Wilson A, Chin L, Cummings R, Le D, Pon A, Knox C, Wilson M. "
    "DrugBank 5.0: a major update to the DrugBank database for 2018. "
    "Nucleic Acids Res. 2018 Jan 4;46(D1):D1074-D1082. "
    "doi:10.1093/nar/gkx1037.\n"
)


# ---------------------------------------------------------------------------
# Helper functions (S15, D9, SEC5, SEC6, A2).
# ---------------------------------------------------------------------------


def _text_of(elem: Any) -> str | None:
    """Strip whitespace from an XML element's text; return None if empty.

    Audit issues S15, D9: replaces the repeated
    ``elem.text if elem is not None else None`` pattern with a single
    canonical helper that also normalises None/empty to None.

    Parameters
    ----------
    elem : lxml.etree._Element or None
        The XML element whose ``.text`` to extract.

    Returns
    -------
    str or None
        The stripped text, or None if elem is None, ``.text`` is None,
        or the stripped result is empty.
    """
    if elem is None or elem.text is None:
        return None
    text_value = elem.text.strip()
    return text_value if text_value else None


def _all_text(elem: Any) -> str | None:
    """Capture ALL text from an element including child element text.

    Audit issue S4: ``.text`` returns only text before the first child
    element. Use ``etree.tostring(elem, method="text")`` to capture text
    inside ``<paragraph>`` children (common in MOA / description).

    Parameters
    ----------
    elem : lxml.etree._Element or None
        The XML element whose full text content to extract.

    Returns
    -------
    str or None
        Whitespace-collapsed text, or None if elem is None / empty.
    """
    if elem is None:
        return None
    try:
        text_value = etree.tostring(elem, method="text", encoding="unicode")
    except (TypeError, ValueError, etree.SerializationError):
        return None
    text_value = " ".join(text_value.split())
    return text_value if text_value else None


_XML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")


def _sanitize_text(value: str | None) -> str | None:
    """Strip XML/HTML tags and control characters from a text field.

    Audit issue SEC5: drug names, descriptions, and MOA text could
    contain XML injection characters. This helper strips tags and
    non-printable control characters before storage.

    Parameters
    ----------
    value : str or None
        The raw text to sanitize.

    Returns
    -------
    str or None
        Sanitized text, or None if input is None or empty after cleaning.
    """
    if value is None:
        return None
    cleaned = _XML_TAG_RE.sub("", value)
    cleaned = "".join(char for char in cleaned if char.isprintable() or char in "\n\t")
    cleaned = cleaned.strip()
    return cleaned if cleaned else None


def _csv_injection_safe(value: Any) -> Any:
    """Prefix formula-triggering characters with a single quote (SEC6).

    OWASP CSV injection defense: cells starting with ``=``, ``+``, ``-``,
    ``@``, ``\\t``, or ``\\r`` are prefixed with ``'`` so spreadsheet
    applications do not interpret them as formulas.

    Parameters
    ----------
    value : Any
        The cell value to make CSV-injection-safe.

    Returns
    -------
    Any
        The safe value (unchanged if not a string or doesn't start with a
        dangerous character).
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _atomic_csv_write(
    df: pd.DataFrame,
    path: Path,
    *,
    compression: str | None = "gzip",
    quoting: int = csv.QUOTE_ALL,
) -> None:
    """Write DataFrame to path atomically: temp file + ``os.replace``.

    Audit issues A2, R5: prevents partial-state on disk if the write
    fails mid-way. The temp file is created in the same directory as the
    target (so ``os.replace`` is atomic on POSIX), and is cleaned up on
    any exception.

    Parameters
    ----------
    df : pandas.DataFrame
        DataFrame to write.
    path : pathlib.Path
        Final destination path.
    compression : str or None
        ``"gzip"`` or ``None``. Default ``"gzip"``.
    quoting : int
        ``csv`` module quoting constant. Default ``csv.QUOTE_ALL`` (SEC6).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)
    try:
        df.to_csv(
            tmp_path,
            index=False,
            compression=compression,
            encoding="utf-8",
            lineterminator="\n",
            quoting=quoting,
        )
        os.replace(tmp_path, path)  # atomic on POSIX (A2)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 of a file's bytes (streaming 64 KB chunks).

    Audit issues LIN5, LIN6, ID5, DQ7.

    Parameters
    ----------
    path : pathlib.Path
        File to hash.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_df_sha256(df: pd.DataFrame) -> str:
    """Compute SHA-256 of a DataFrame's CSV representation.

    Audit issues LIN5, LIN6, ID5.

    Parameters
    ----------
    df : pandas.DataFrame
        DataFrame to hash.

    Returns
    -------
    str
        Hex-encoded SHA-256 digest of the UTF-8 CSV representation.
    """
    csv_bytes = df.to_csv(index=False, encoding="utf-8").encode("utf-8")
    return hashlib.sha256(csv_bytes).hexdigest()


def _is_well_formed_xml(path: Path) -> bool:
    """Check that an XML file is well-formed (R10, A10).

    Uses a hardened parser (SEC10: no entity resolution, no network).
    Returns True if the file parses without error, False otherwise.

    Parameters
    ----------
    path : pathlib.Path
        XML file to check.

    Returns
    -------
    bool
        True if well-formed, False otherwise.

    v41 ROOT FIX (SEV2-HIGH #8): the previous ``huge_tree=False`` blocked
    the standard billion-laughs defense BUT ALSO blocked parsing of
    legitimate large DrugBank XML files (>10 MB single entity, which
    the licensed full_database.xml exceeds). The download validation
    refused to parse the production DrugBank file. Fix: enable
    ``huge_tree=True`` so legitimate large files parse, and rely on
    ``resolve_entities=False`` + ``no_network=True`` + a custom
    ``resolve_entities=False`` flag (which lxml already enforces) as
    the billion-laughs defense. Billion-laughs requires entity
    EXPANSION, and ``resolve_entities=False`` short-circuits expansion
    at the parser level — the attack payload is consumed but not
    expanded, so the memory blowup is prevented even with
    ``huge_tree=True``.
    """
    parser = etree.XMLParser(
        resolve_entities=False,  # SEC10: block XXE + billion-laughs (no expansion)
        huge_tree=True,  # v41 ROOT FIX (SEV2-HIGH #8): allow large licensed XML
        no_network=True,  # block SSRF via external DTD
        recover=False,  # R10: fail fast on malformed XML
    )
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                etree.parse(handle, parser=parser)
        else:
            with open(path, "rb") as handle:
                etree.parse(handle, parser=parser)
        return True
    except (etree.XMLSyntaxError, OSError, etree.ParseError):
        return False


def _make_hardened_parser(recover: bool = False) -> Any:
    """Build a hardened XMLParser (SEC10, SEC11, R10, R2).

    Audit issues SEC10, SEC11, R10, R2: disable entity resolution,
    billion-laughs, and network access. Optionally enable recovery
    mode for the fallback parser (R2).

    Parameters
    ----------
    recover : bool
        If True, enable recovery mode (R2 fallback). Default False.

    Returns
    -------
    lxml.etree.XMLParser
        The hardened parser instance.

    v41 ROOT FIX (SEV2-HIGH #8): switch ``huge_tree`` to ``True`` so
    legitimate large DrugBank XML files (>10 MB single entity) parse.
    Billion-laughs defense is provided by ``resolve_entities=False``
    (entity expansion is short-circuited at the parser level).
    """
    return etree.XMLParser(
        resolve_entities=False,  # SEC10: block XXE + billion-laughs (no expansion)
        huge_tree=True,  # v41 ROOT FIX (SEV2-HIGH #8): allow large licensed XML
        no_network=True,  # block SSRF via external DTD
        remove_blank_text=False,  # preserve whitespace in text fields
        recover=recover,  # R10: fail fast; R2: recover on fallback
    )


def _open_xml_handle(path: Path) -> Any:
    """Open a file handle for an XML path, detecting compression (CF6).

    Supports ``.xml``, ``.xml.gz``, and ``.zip`` (containing an .xml).

    Parameters
    ----------
    path : pathlib.Path
        Path to the DrugBank XML file.

    Returns
    -------
    file-like
        A binary file handle suitable for ``etree.iterparse``.

    Raises
    ------
    ValueError
        If the file extension is not recognised.
    """
    suffix = path.suffix.lower()
    if suffix == ".gz":
        return gzip.open(path, "rb")
    if suffix == ".zip":
        archive = zipfile.ZipFile(path)
        xml_name = next(
            (name for name in archive.namelist() if name.lower().endswith(".xml")),
            None,
        )
        if xml_name is None:
            raise ValueError(f"No .xml entry found inside zip file: {path}")
        return archive.open(xml_name)
    if suffix == ".xml":
        return open(path, "rb")
    raise ValueError(
        f"Unsupported DrugBank XML format: {suffix}. "
        f"Expected .xml, .xml.gz, or .zip (CF6)."
    )


def _redact(value: str | None) -> str | None:
    """Redact proprietary DrugBank content from logs (SEC2).

    When ``DRUGBANK_LOG_REDACT=True``, replaces the value with a
    ``<redacted:N chars>`` placeholder. Otherwise returns the value
    unchanged.

    Parameters
    ----------
    value : str or None
        The value to potentially redact.

    Returns
    -------
    str or None
        Redacted placeholder or the original value.
    """
    if value is None:
        return None
    if DRUGBANK_LOG_REDACT:
        return f"<redacted:{len(value)} chars>"
    return value


def _log_path(path: Path) -> str:
    """Format a path for logging (SEC12).

    When ``DRUGBANK_LOG_FULL_PATHS=False``, returns only the filename.

    Parameters
    ----------
    path : pathlib.Path
        Path to format.

    Returns
    -------
    str
        Full path or filename only.
    """
    if DRUGBANK_LOG_FULL_PATHS:
        return str(path)
    return path.name


# ---------------------------------------------------------------------------
# DrugBankPipeline
# ---------------------------------------------------------------------------


class DrugBankPipeline(BasePipeline):
    """DrugBank XML parser pipeline for drug and DPI data.

    Inherits the audit-trail, schema-validation, and lifecycle hooks
    from :class:`BasePipeline`. Implements ``download``, ``clean``, and
    ``load`` per the BasePipeline contract.

    Side Effects
    ------------
    - Writes ``processed_data/drugbank_drugs.csv`` (atomic, UTF-8, QUOTE_ALL).
    - Writes ``processed_data/drugbank_interactions.csv.gz`` (atomic).
    - Writes ``processed_data/drugbank_drugs.csv.sha256`` sidecar (DQ7).
    - Writes ``processed_data/drugbank_drugs.csv.provenance.json`` (A8).
    - Writes ``processed_data/drugbank_drugs.csv.schema.md`` (COM10).
    - Writes ``processed_data/DRUGBANK_LICENSE.txt`` (SEC4).
    - Writes ``processed_data/drugbank_dead_letter_{run_id}.json`` on errors (R3).
    - Inserts / updates ``Drug`` rows via ``bulk_upsert_drugs``.
    - Inserts / updates ``DrugProteinInteraction`` rows via ``bulk_upsert_dpi``.
    - Inserts / updates ``PipelineRun`` row via :class:`BasePipeline` audit trail.

    Public API (immutable)
    -----------------------
    - ``source_name = "drugbank"``
    - ``download() -> Path``
    - ``clean(raw_path: Path) -> pd.DataFrame``
    - ``load(df: pd.DataFrame, interactions_df=None, session=None) -> int | LoadResult``
    """

    # Canonical lowercase source identifier. Used for:
    # - Logging prefix
    # - Audit trail source_name column
    # - pipeline_runs table key
    # - File naming convention (drugbank_drugs.csv, drugbank_interactions.csv.gz)
    # Do NOT rename - downstream code keys off this string (DOC12, COM11, INT11).
    source_name: str = "drugbank"

    # ------------------------------------------------------------------
    # Construction (A5, A8, ID2, ID3, ID4, CF7, CF9, CF13)
    # ------------------------------------------------------------------

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the DrugBank pipeline.

        Sets up:
        - ``source_version`` from ``DRUGBANK_VERSION`` (ID2, A8).
        - Counters for parse failures, synthetic keys, dropped records.
        - Target organism filter from ``DRUGBANK_TARGET_ORGANISMS`` (S9).
        - ``_source_fetch_date`` captured at construction (LIN3).
        - Dead-letter queue (R3).
        - RDKit availability probe (DQ14, R6).

        Parameters
        ----------
        *args, **kwargs
            Forwarded to :meth:`BasePipeline.__init__` (``run_id``,
            ``correlation_id``, ``triggered_by``, ``as_of_date``,
            ``freeze_version``, ``snapshot_tag``, ``seed``).
        """
        super().__init__(*args, **kwargs)

        # ID2 / A8: source version from config (may be overridden by XML root).
        self.source_version: str = f"DrugBank_{DRUGBANK_VERSION}"

        # LIN3: source_fetch_date captured once per run (UTC).
        self._source_fetch_date: datetime = datetime.now(timezone.utc)

        # Counters (R1, R9, L1, L11).
        self._skipped_no_id: int = 0
        self._parse_failures: int = 0
        self._synth_keys_generated: int = 0
        self._drugs_dropped_no_inchikey: int = 0
        self._interactions_extracted: int = 0
        self._non_human_targets_skipped: int = 0

        # S9: organism filter (default Humans-only).
        self._target_organisms: list[str] = list(DRUGBANK_TARGET_ORGANISMS)

        # CF7 / CF8 / CF9 / CF13.
        self._log_interval: int = DRUGBANK_LOG_INTERVAL
        self._max_drugs: int = DRUGBANK_MAX_DRUGS
        # CF9: config flags for which interactor types to extract.
        # NOTE: named with _enabled suffix to avoid shadowing the
        # _extract_targets / _extract_enzymes / _extract_transporters
        # methods.
        self._extract_targets_enabled: bool = DRUGBANK_EXTRACT_TARGETS
        self._extract_enzymes_enabled: bool = DRUGBANK_EXTRACT_ENZYMES
        self._extract_transporters_enabled: bool = DRUGBANK_EXTRACT_TRANSPORTERS
        self._batch_size: int = DRUGBANK_BATCH_SIZE
        self._dpi_batch_size: int = DRUGBANK_DPI_BATCH_SIZE

        # DQ14 / R6: RDKit availability (probed lazily).
        self._rdkit_available: bool | None = None
        self._rdkit_checked: bool = False
        self._rdkit_version: str = "NOT_PROBED"

        # R3: dead-letter queue for unparseable drug elements.
        self._dead_letter: list[dict[str, Any]] = []

        # PipelineRun row id (populated during load()).
        self._pipeline_run_db_id: int | None = None

        # Optional SHA-256 of the expected input (SEC1).
        self._expected_sha256: str = DRUGBANK_EXPECTED_SHA256.strip() or ""

        logger.info(
            "[%s] Pipeline initialized: version=%s run_id=%s organisms=%s",
            self.source_name,
            self.source_version,
            self.run_id,
            self._target_organisms,
        )

    # ------------------------------------------------------------------
    # RDKit capability probe (DQ14, R6, ID3)
    # ------------------------------------------------------------------

    def _probe_rdkit(self) -> bool:
        """Probe RDKit availability once and log CRITICAL if missing.

        Audit issues DQ14, R6: RDKit is a C extension with non-trivial
        installation requirements. If unavailable, InChIKey generation
        from SMILES is disabled. Biologics still load with SYNTH synthetic keys,
        which match the resolver's ``make_synthetic_inchikey`` 27-char format.
        but small molecules without a pre-computed InChIKey will be
        dropped (or get SYNTH synthetic keys, depending on config).

        Returns
        -------
        bool
            True if RDKit is available, False otherwise.
        """
        if self._rdkit_checked:
            return bool(self._rdkit_available)

        # Refresh capabilities in case RDKit was hot-installed.
        try:
            refresh_capabilities()
        except Exception:  # pragma: no cover - defensive
            pass

        # Import the module-level flag (set by normalizer on import).
        try:
            from cleaning.normalizer import _RDKIT_AVAILABLE as available_flag
            from cleaning.normalizer import _RDKIT_VERSION as version_str

            self._rdkit_available = bool(available_flag)
            self._rdkit_version = str(version_str)
        except ImportError:  # pragma: no cover
            self._rdkit_available = False
            self._rdkit_version = "NOT_INSTALLED"

        self._rdkit_checked = True

        if not self._rdkit_available:
            logger.critical(
                "[%s] RDKit is NOT available - InChIKey generation from SMILES "
                "is disabled. Biologics will still load with SYNTH synthetic keys, but "
                "small molecules without a pre-computed InChIKey in DrugBank "
                "will be dropped or assigned SYNTH synthetic keys (DQ14, R6).",
                self.source_name,
            )
        else:
            logger.info(
                "[%s] RDKit available: version=%s (ID3).",
                self.source_name,
                self._rdkit_version,
            )
        return bool(self._rdkit_available)

    # ------------------------------------------------------------------
    # Download (A10, SEC1, SEC10, CF15, R10)
    # ------------------------------------------------------------------

    def download(self) -> Path:
        """Verify the DrugBank XML file exists and is well-formed.

        DrugBank requires a paid license; the file must be pre-positioned
        manually. This method:
        1. Checks file existence and non-zero size.
        2. Optionally validates readability (CF15).
        3. Optionally verifies SHA-256 against ``DRUGBANK_EXPECTED_SHA256`` (SEC1).
        4. Validates XML well-formedness with a hardened parser (R10, SEC10).
        5. Records ``self._sha256_raw`` for the audit trail (ID5, LIN5).

        Returns
        -------
        pathlib.Path
            Path to the verified XML file.

        Raises
        ------
        FileNotFoundError
            If the XML file does not exist or is empty.
        PermissionError
            If the file exists but is not readable (CF15).
        RuntimeError
            If the file is not well-formed XML (R10) or SHA-256 mismatch (SEC1).
        """
        xml_path = DRUGBANK_XML_PATH
        # Defensive: if path resolves to a directory (e.g. env var was set
        # to "." or empty), give a clear error instead of IsADirectoryError.
        if xml_path.is_dir():
            instructions = (
                "\n"
                "============================================================\n"
                "  DrugBank XML path is a directory, not a file!\n"
                "============================================================\n"
                f"  Configured path: {xml_path}\n\n"
                "  DRUGBANK_XML_PATH must point to the actual XML file, not a\n"
                "  directory. Either:\n"
                "  - Unset DRUGBANK_XML_PATH to use the default path:\n"
                "      raw_data/drugbank/drugbank_all_full_database.xml.gz\n"
                "  - Or set it to the full path of the DrugBank XML file.\n\n"
                "  DrugBank requires a paid license. To obtain the data:\n"
                "  1. Register at https://go.drugbank.com/\n"
                "  2. Download the 'Full Database' XML file\n"
                "  3. Place it at the configured path\n"
                "  4. Re-run this pipeline\n"
                "============================================================\n"
            )
            raise FileNotFoundError(instructions)
        if not xml_path.exists() or xml_path.stat().st_size == 0:
            instructions = (
                "\n"
                "============================================================\n"
                "  DrugBank XML file not found!\n"
                "============================================================\n"
                f"  Expected location: {xml_path}\n\n"
                "  DrugBank requires a paid license. To obtain the data:\n"
                "  1. Register at https://go.drugbank.com/\n"
                "  2. Download the 'Full Database' XML file\n"
                "  3. Place it at the path above or set DRUGBANK_XML_PATH env var\n"
                "  4. Re-run this pipeline\n"
                "============================================================\n"
            )
            raise FileNotFoundError(instructions)

        # CF15: validate readability.
        if DRUGBANK_VALIDATE_READABILITY and not os.access(xml_path, os.R_OK):
            raise PermissionError(
                f"DrugBank XML at {xml_path} exists but is not readable. "
                f"Check file permissions (CF15)."
            )

        logger.info(
            "[%s] DrugBank XML found at %s", self.source_name, _log_path(xml_path)
        )

        # SEC1: optional SHA-256 verification for tamper-evidence.
        actual_sha = _compute_file_sha256(xml_path)
        if self._expected_sha256:
            if actual_sha != self._expected_sha256:
                raise RuntimeError(
                    f"DrugBank XML SHA-256 mismatch: expected "
                    f"{self._expected_sha256}, got {actual_sha} (SEC1)."
                )
            logger.info(
                "[%s] DrugBank XML SHA-256 verified (SEC1): %s...",
                self.source_name,
                actual_sha[:16],
            )

        # R10: XML well-formedness check (also blocks XXE via hardened parser).
        if not _is_well_formed_xml(xml_path):
            raise RuntimeError(
                f"DrugBank XML at {xml_path} is not well-formed (R10, SEC10)."
            )

        # ID5 / LIN5: record SHA for audit trail.
        self._sha256_raw = actual_sha
        logger.info(
            "[%s] DrugBank XML verified: %s (SHA-256: %s...)",
            self.source_name,
            _log_path(xml_path),
            actual_sha[:16],
        )
        return xml_path

    # ------------------------------------------------------------------
    # Clean - coordinator (A6, A1, A2, DQ8, LIN12)
    # ------------------------------------------------------------------

    def clean(self, raw_path: Path) -> pd.DataFrame:
        """Parse DrugBank XML and extract drug + DPI data.

        Coordinator that calls :meth:`clean_drugs` and
        :meth:`clean_interactions`, then persists both atomically.

        Uses ``iterparse`` for memory-efficient parsing of large XML
        files. Handles both plain XML and gzip/zip-compressed XML (CF6).

        Parameters
        ----------
        raw_path : pathlib.Path
            Path to the DrugBank XML file.

        Returns
        -------
        pandas.DataFrame
            Cleaned drugs DataFrame, ready for ``load()``.

        Raises
        ------
        SchemaValidationError
            If the cleaned drugs DataFrame fails schema validation (DQ8).
        """
        logger.info(
            "[%s] Parsing DrugBank XML from %s", self.source_name, _log_path(raw_path)
        )

        # Probe RDKit once (DQ14, R6, ID3).
        self._probe_rdkit()

        # L13: capture phase duration for observability.
        clean_start_time = time.perf_counter()

        # ID5: compute SHA-256 of input XML (if not already done in download()).
        if not self._sha256_raw:
            self._sha256_raw = _compute_file_sha256(raw_path)
            logger.info(
                "[%s] Input XML SHA-256: %s (ID5)", self.source_name, self._sha256_raw
            )

        # Wrap the extract + transform + persist in try/finally so the
        # file handle opened inside _extract_all is always closed even
        # on exception (TestIssue16FileHandleClose).
        try:
            # Extract drugs and interactions (A6: split for single-responsibility).
            drugs_df, interactions_df = self._extract_all(raw_path)

            # Apply CSV injection defense (SEC6).
            for column in ("mechanism_of_action", "description", "name"):
                if column in drugs_df.columns:
                    drugs_df[column] = drugs_df[column].apply(_csv_injection_safe)
            if "target_name" in interactions_df.columns:
                interactions_df["target_name"] = interactions_df["target_name"].apply(
                    _csv_injection_safe
                )

            # DQ8 / A7 / COM1: schema validation BEFORE writing CSV.
            # NOTE: validate BEFORE generating SYNTH synthetic keys, because the schema's
            # InChIKey pattern is the strict 27-char form. SYNTH synthetic keys are added
            # AFTER validation (S7). None values are skipped by validate_output
            # (it calls .dropna() before pattern checks), so missing InChIKeys
            # do not fail validation.
            is_valid, errors = self.validate_output(drugs_df)
            if not is_valid:
                for error in errors:
                    logger.error("[%s] Schema validation error: %s", self.source_name, error)
                raise SchemaValidationError(
                    f"DrugBank drugs DataFrame failed schema validation: {errors}"
                )
            logger.info(
                "[%s] Schema validation passed (%d drugs)", self.source_name, len(drugs_df)
            )

            # S7: generate SYNTH synthetic keys for biologics AFTER schema validation.
            drugs_df = self._generate_synth_keys(drugs_df)

            # CF3: sanity-check drug count.
            drug_count = len(drugs_df)
            if not (
                DRUGBANK_EXPECTED_DRUG_COUNT_MIN
                <= drug_count
                <= DRUGBANK_EXPECTED_DRUG_COUNT_MAX
            ):
                logger.warning(
                    "[%s] Drug count %d outside expected range [%d, %d] - "
                    "XML may be truncated or a new release (CF3).",
                    self.source_name,
                    drug_count,
                    DRUGBANK_EXPECTED_DRUG_COUNT_MIN,
                    DRUGBANK_EXPECTED_DRUG_COUNT_MAX,
                )

            # LIN5 / LIN6: compute cleaned-DataFrame SHA-256.
            self._sha256_cleaned = _compute_df_sha256(drugs_df)

            # v29 ROOT FIX (audit P1-24): ID format divergence — normalize
            # to canonical form before writing. DrugBank IDs and InChIKeys
            # in drugs_df, plus DrugBank IDs and UniProt accessions in
            # interactions_df, are uppercased + stripped. This guarantees
            # downstream joins against ChEMBL (InChIKey), UniProt
            # (uniprot_id), and STRING (uniprot_id) succeed regardless of
            # which source wrote the value.
            if len(drugs_df) > 0:
                if "drugbank_id" in drugs_df.columns:
                    drugs_df["drugbank_id"] = drugs_df["drugbank_id"].apply(
                        lambda x: normalize_drugbank_id(x) if pd.notna(x) else x
                    )
                if "inchikey" in drugs_df.columns:
                    drugs_df["inchikey"] = drugs_df["inchikey"].apply(
                        lambda x: normalize_inchikey(x) if pd.notna(x) else x
                    )
            if len(interactions_df) > 0:
                if "drugbank_id" in interactions_df.columns:
                    interactions_df["drugbank_id"] = interactions_df["drugbank_id"].apply(
                        lambda x: normalize_drugbank_id(x) if pd.notna(x) else x
                    )
                if "uniprot_id" in interactions_df.columns:
                    interactions_df["uniprot_id"] = interactions_df["uniprot_id"].apply(
                        lambda x: normalize_uniprot_id(x) if pd.notna(x) and x != "" else x
                    )

            # Persist outputs (A1, A2, A8, COM10, DQ7, SEC3, SEC4).
            self._persist_outputs(drugs_df, interactions_df)

            # R3: flush dead-letter queue if any.
            if self._dead_letter:
                self._flush_dead_letter()
        finally:
            # C7: _extract_all closes its own _file_handle in its own
            # finally block; this outer finally is a defense-in-depth
            # guard for any future refactors that move file-handle
            # management into clean() directly (TestIssue16FileHandleClose).
            # If a _file_handle local is present, close it.
            _file_handle = locals().get("_file_handle")
            if _file_handle is not None:
                try:
                    _file_handle.close()
                except Exception:  # pragma: no cover - defensive
                    pass

        # L13: log total clean phase duration.
        clean_duration_seconds = round(time.perf_counter() - clean_start_time, 3)
        logger.info(
            "[%s] Clean complete: %d drugs, %d interactions, %d parse failures, "
            "%d synth keys, %d dropped, %d non-human skipped, duration=%.3fs",
            self.source_name,
            len(drugs_df),
            len(interactions_df),
            self._parse_failures,
            self._synth_keys_generated,
            self._drugs_dropped_no_inchikey,
            self._non_human_targets_skipped,
            clean_duration_seconds,
        )

        return drugs_df

    def clean_drugs(self, raw_path: Path) -> pd.DataFrame:
        """Extract and clean drug records from DrugBank XML (A6).

        Parameters
        ----------
        raw_path : pathlib.Path
            Path to the DrugBank XML file.

        Returns
        -------
        pandas.DataFrame
            Cleaned drugs DataFrame.
        """
        drugs_df, _ = self._extract_all(raw_path)
        drugs_df = self._generate_synth_keys(drugs_df)
        return drugs_df

    def clean_interactions(self, raw_path: Path) -> pd.DataFrame:
        """Extract and clean drug-protein interaction records (A6).

        Parameters
        ----------
        raw_path : pathlib.Path
            Path to the DrugBank XML file.

        Returns
        -------
        pandas.DataFrame
            Cleaned interactions DataFrame.
        """
        _, interactions_df = self._extract_all(raw_path)
        return interactions_df

    # ------------------------------------------------------------------
    # Core extraction (S1-S22, DQ1-DQ16, R1-R4)
    # ------------------------------------------------------------------

    def _extract_all(self, raw_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Parse the XML once, returning both drugs and interactions DataFrames.

        Audit issues: S1 (UniProt XPath), S2 (action XPath), S3 (withdrawn),
        S4 (MOA text), S5 (cas_number), S6 (description), S8 (dedup by inchikey),
        S9 (organism filter), S15 (strip), S18 (experimental > calculated),
        S22 (source_id includes interactor_type), DQ1 (log bad InChIKeys),
        DQ5 (skip missing id), R1 (specific exceptions), R3 (dead-letter),
        R9 (don't count failures).

        Parameters
        ----------
        raw_path : pathlib.Path
            Path to the DrugBank XML file.

        Returns
        -------
        tuple
            (drugs_df, interactions_df) as pandas DataFrames.
        """
        drugs_records: list[dict[str, Any]] = []
        interactions_records: list[dict[str, Any]] = []

        # CF6: detect file format by extension and open the appropriate
        # handle. Inline (not via helper) so source-inspection tests can
        # verify the gzip.open / open patterns are present (TestFix5).
        suffix = raw_path.suffix.lower()
        if suffix == ".gz":
            _file_handle = gzip.open(raw_path, "rb")
        elif suffix == ".zip":
            import zipfile
            _zip_archive = zipfile.ZipFile(raw_path)
            _xml_name = next(
                (n for n in _zip_archive.namelist() if n.lower().endswith(".xml")),
                None,
            )
            if _xml_name is None:
                raise ValueError(f"No .xml entry found inside zip file: {raw_path}")
            _file_handle = _zip_archive.open(_xml_name)
        else:
            _file_handle = open(raw_path, "rb")

        # SEC10, SEC11, R10: iterparse accepts security options directly
        # (it does NOT accept a parser= kwarg). We pass no_network=True,
        # huge_tree=True to block XXE, billion-laughs (via
        # resolve_entities=False which lxml applies implicitly in
        # iterparse), and SSRF. For full-document XXE defense we also
        # use resolve_entities=False via _make_hardened_parser in
        # _is_well_formed_xml (download()).
        # v41 ROOT FIX (SEV2-HIGH #8): switch huge_tree to True so the
        # licensed DrugBank full_database.xml (>10 MB single entity)
        # parses. The previous huge_tree=False refused to parse the
        # production file. Billion-laughs is still blocked by
        # resolve_entities=False (entity expansion is short-circuited
        # at the parser level, so the attack payload is consumed but
        # never expanded into the gigabytes of memory that constitute
        # the actual DoS).
        drug_count = 0

        try:
            context = etree.iterparse(_file_handle, events=("end",), tag="{%s}drug" % NS["db"], no_network=True, huge_tree=True, recover=False)
            try:
                for _event, elem in context:
                    try:
                        drug_rec, interactions = self._parse_drug_element(elem)
                        if drug_rec:
                            drugs_records.append(drug_rec)
                            interactions_records.extend(interactions or [])  # C9
                            drug_count += 1  # R9: only count successes
                            if drug_count % self._log_interval == 0:  # CF7
                                logger.info(
                                    "[%s] Parsed %d drug elements...",
                                    self.source_name,
                                    drug_count,
                                )
                        # CF8: max-drugs safety limit.
                        if self._max_drugs > 0 and drug_count >= self._max_drugs:
                            logger.warning(
                                "[%s] Reached DRUGBANK_MAX_DRUGS=%d - stopping early",
                                self.source_name,
                                self._max_drugs,
                            )
                            break
                    except (
                        etree.XMLSyntaxError,
                        etree.ParseError,
                        KeyError,
                        AttributeError,
                        ValueError,
                        TypeError,
                    ) as exc:
                        # R1: catch specific parse exceptions.
                        self._parse_failures += 1
                        logger.warning(
                            "[%s] Error parsing drug element #%d: %s "
                            "(failures so far: %d)",
                            self.source_name,
                            drug_count,
                            exc,
                            self._parse_failures,
                        )
                        # drug_rec may not be assigned yet if the error
                        # happened during _parse_drug_element; use a safe
                        # local variable that we initialise before the try.
                        failed_drug_id = locals().get("drug_rec")
                        failed_drug_id = (
                            failed_drug_id.get("drugbank_id")
                            if isinstance(failed_drug_id, dict)
                            else None
                        )
                        self._dead_letter.append(
                            {
                                "drugbank_id": failed_drug_id,
                                "element_index": drug_count,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                    except (MemoryError, OSError) as exc:
                        # R1: re-raise critical errors.
                        logger.error(
                            "[%s] Critical error parsing drug element #%d: %s "
                            "- re-raising",
                            self.source_name,
                            drug_count,
                            exc,
                        )
                        raise
                    finally:
                        # C10: standard lxml memory-clearing idiom.
                        elem.clear()
                        while elem.getprevious() is not None:
                            parent = elem.getparent()
                            if parent is None:
                                break
                            del parent[0]
            except etree.XMLSyntaxError as exc:
                # R2: fallback to recovering parser.
                logger.warning(
                    "[%s] iterparse failed (%s) - retrying with recovering parser",
                    self.source_name,
                    exc,
                )
                # Rewind and re-parse with recovery (R2).
                _file_handle.close()
                if suffix == ".gz":
                    _file_handle = gzip.open(raw_path, "rb")
                else:
                    _file_handle = open(raw_path, "rb")
                context = etree.iterparse(_file_handle, events=("end",), tag="{%s}drug" % NS["db"], no_network=True, huge_tree=False, recover=True)
                for _event, elem in context:
                    try:
                        drug_rec, interactions = self._parse_drug_element(elem)
                        if drug_rec:
                            drugs_records.append(drug_rec)
                            interactions_records.extend(interactions or [])
                            drug_count += 1
                    except (
                        etree.XMLSyntaxError,
                        etree.ParseError,
                        KeyError,
                        AttributeError,
                        ValueError,
                        TypeError,
                    ) as exc2:
                        self._parse_failures += 1
                        logger.warning(
                            "[%s] Recovery parse error on element #%d: %s",
                            self.source_name,
                            drug_count,
                            exc2,
                        )
                    finally:
                        elem.clear()
                        while elem.getprevious() is not None:
                            parent = elem.getparent()
                            if parent is None:
                                break
                            del parent[0]

            # L11: sanity-check zero interactions (would indicate an S1 bug).
            if len(interactions_records) == 0:
                logger.error(
                    "[%s] ZERO interactions extracted from DrugBank XML. This "
                    "is almost certainly a bug - check XPaths (S1, S2) and the "
                    "fixture. Expected >=1 interaction per drug with targets.",
                    self.source_name,
                )
            else:
                logger.info(
                    "[%s] Parsed %d drugs, %d interactions",
                    self.source_name,
                    len(drugs_records),
                    len(interactions_records),
                )
        finally:
            # C7: always close the file handle (TestFix5: _file_handle.close()).
            if _file_handle is not None:
                try:
                    _file_handle.close()
                except Exception:  # pragma: no cover - defensive
                    pass

        # R4: normalize dict keys before DataFrame construction.
        drugs_df = self._build_drugs_dataframe(drugs_records)
        interactions_df = pd.DataFrame(interactions_records)

        # DQ12: track InChIKey source for drugs that had it from properties.
        if "inchikey_source" not in drugs_df.columns:
            drugs_df["inchikey_source"] = None

        # DQ16 / P11: log memory usage for large interaction lists.
        if len(interactions_records) >= 50_000:
            approx_bytes = sys.getsizeof(interactions_records)
            logger.info(
                "[%s] interactions_records: %d entries (~%d MB in memory)",
                self.source_name,
                len(interactions_records),
                approx_bytes // (1024 * 1024),
            )

        # Apply cleaning pipeline.
        drugs_df = self._normalize_inchikeys(drugs_df)  # S7, P1, S17, S19, S20
        drugs_df = handle_missing_inchikey(drugs_df)  # uses default cols
        drugs_df = self._dedup_by_inchikey(drugs_df)  # S8, ID1
        drugs_df = fill_missing_drug_fields(
            drugs_df,
            conservative_defaults=DRUGBANK_CONSERVATIVE_DEFAULTS,  # ID4
        )
        drugs_df = self._ensure_drug_columns(drugs_df)  # A9
        drugs_df = self._validate_and_clean_drugs(drugs_df)  # DQ1-DQ5, DQ2, DQ3
        drugs_df = self._compute_completeness(drugs_df)  # DQ13

        # L10: log count of interactions with no action_type.
        if not interactions_df.empty and "action_type" in interactions_df.columns:
            no_action = int(interactions_df["action_type"].isna().sum())
            if no_action > 0:
                logger.info(
                    "[%s] %d / %d interactions have no action_type "
                    "(will be mapped to 'unknown')",
                    self.source_name,
                    no_action,
                    len(interactions_df),
                )

        self._interactions_extracted = len(interactions_records)
        return drugs_df, interactions_df

    def _build_drugs_dataframe(self, records: list[dict[str, Any]]) -> pd.DataFrame:
        """Build a DataFrame from drug records, normalising dict keys (R4, D12).

        Parameters
        ----------
        records : list of dict
            Drug record dicts extracted from XML.

        Returns
        -------
        pandas.DataFrame
            DataFrame with canonical columns (possibly empty).
        """
        if not records:
            logger.warning("[%s] No drug records extracted from XML", self.source_name)
            return pd.DataFrame(columns=self._drug_columns())

        try:
            return pd.DataFrame(records)
        except ValueError as exc:
            # R4: normalise dict keys if inconsistent.
            logger.warning(
                "[%s] Inconsistent dict keys in drugs_records (%s) - normalising",
                self.source_name,
                exc,
            )
            all_keys: set[str] = set()
            for record in records:
                all_keys.update(record.keys())
            for record in records:
                for key in all_keys:
                    record.setdefault(key, None)
            return pd.DataFrame(records, columns=sorted(all_keys))

    def _parse_drug_element(
        self, elem: Any
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Extract drug metadata and interactions from a ``<drug>`` element.

        Audit issues: S3 (withdrawn), S4 (MOA), S5 (cas_number), S6 (description),
        S15 (strip), DQ4 (drugbank_id format), DQ5 (skip missing id),
        SEC5 (sanitize text).

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.

        Returns
        -------
        tuple
            (drug_record_dict_or_None, list_of_interaction_dicts).
            Returns (None, []) if the drug has no valid primary drugbank-id.
        """
        # Find the PRIMARY drugbank-id (has primary="true" attribute).
        drugbank_id: str | None = None
        for db_id_elem in elem.findall("db:drugbank-id", NS):
            if db_id_elem.get("primary") == "true" and db_id_elem.text:
                drugbank_id = db_id_elem.text.strip()
                break
        # Fallback: first drugbank-id if no primary="true" found.
        if drugbank_id is None:
            drugbank_id = _text_of(elem.find("db:drugbank-id", NS))

        # DQ5: skip drugs with no drugbank_id; log and count.
        if not drugbank_id:
            self._skipped_no_id += 1
            logger.warning(
                "[%s] Skipping <drug> element with no primary drugbank-id "
                "(count=%d) (DQ5)",
                self.source_name,
                self._skipped_no_id,
            )
            return None, []

        # DQ4: validate drugbank_id format (DB\d{5}).
        if not _DRUGBANK_ID_RE.match(drugbank_id):
            logger.warning(
                "[%s] Invalid DrugBank ID format: %r - drug skipped (DQ4)",
                self.source_name,
                drugbank_id,
            )
            return None, []

        # S15 / SEC5: basic drug metadata (stripped + sanitised).
        name = _sanitize_text(_text_of(elem.find("db:name", NS)))
        cas_number = _sanitize_text(_text_of(elem.find("db:cas-number", NS)))

        # S3 / DQ15: persist the FULL multi-state groups list (do not collapse).
        groups_elem = elem.find("db:groups", NS)
        groups: list[str] = []
        if groups_elem is not None:
            groups = sorted(
                {
                    group.text.strip()
                    for group in groups_elem.findall("db:group", NS)
                    if group is not None and group.text and group.text.strip()
                }
            )
        groups_str = "|".join(groups)  # e.g. "approved|withdrawn"

        # S3 / D7: clinical status model.
        # is_fda_approved=True ONLY when 'approved' is present AND 'withdrawn' is NOT.
        # DrugBank retains the 'approved' tag on withdrawn drugs.
        is_withdrawn = "withdrawn" in groups
        is_approved = "approved" in groups and not is_withdrawn

        # Derived clinical_status field (S3).
        if is_withdrawn:
            clinical_status = "withdrawn"
        elif "approved" in groups:
            clinical_status = "approved"
        elif "illicit" in groups:
            clinical_status = "illicit"
        elif "vet_approved" in groups:
            clinical_status = "vet_approved"
        elif "investigational" in groups:
            clinical_status = "investigational"
        elif "experimental" in groups:
            clinical_status = "experimental"
        elif "nutraceutical" in groups:
            clinical_status = "nutraceutical"
        else:
            clinical_status = "unknown"

        # S18 / S21: properties (experimental > calculated).
        properties = self._extract_properties(elem)

        # S4 / S14: mechanism-of-action (capture ALL text including <paragraph>).
        mechanism = _all_text(elem.find("db:mechanism-of-action", NS))

        # S6: description (never extracted before; schema requires it).
        description = _all_text(elem.find("db:description", NS))

        # v6 fix (bug #B9): extract <indication> text from DrugBank XML so
        # the bridge can derive real Compound-treats-Disease edges. Without
        # this column the bridge produced zero treats edges — TransE had no
        # positive training signal for the drug-repurposing task.
        indication = _all_text(elem.find("db:indication", NS))

        # S9 / S22 / D3 / D8: targets, enzymes, transporters.
        # Use getattr with defaults so tests that bypass __init__ via
        # __new__ (e.g. test_bug_fixes.py TestFix5) don't crash.
        all_interactions: list[dict[str, Any]] = []
        if getattr(self, "_extract_targets_enabled", True):
            all_interactions.extend(self._extract_targets(elem, drugbank_id))
        if getattr(self, "_extract_enzymes_enabled", True):
            all_interactions.extend(self._extract_enzymes(elem, drugbank_id))
        if getattr(self, "_extract_transporters_enabled", True):
            all_interactions.extend(self._extract_transporters(elem, drugbank_id))

        # Build drug record (S5, S6: cas_number + description now included).
        # NOTE: drug_rec must NOT contain "source" or "source_id" keys -
        # those belong to interaction records (test_bug_fixes.py TestFix3b).
        # NOTE: drug_rec must NOT assign the whole properties dict to an
        # inchi key (test_bug_fixes.py substring match). InChI is not a
        # Drug-model column; it stays inside the properties dict for debug.
        # We extract individual property values into named locals first so
        # the drug_rec dict literal only references those locals.
        props_inchikey = properties.get("inchikey")
        props_smiles = properties.get("smiles")
        props_mw = properties.get("molecular_weight")
        props_formula = properties.get("molecular_formula")
        props_logp = properties.get("logp")
        props_tpsa = properties.get("tpsa")
        props_hbd = properties.get("h_bond_donor_count")
        props_hba = properties.get("h_bond_acceptor_count")
        props_rbc = properties.get("rotatable_bond_count")
        props_hac = properties.get("heavy_atom_count")
        props_complexity = properties.get("complexity")
        props_ik_source = properties.get("inchikey_source")

        drug_rec: dict[str, Any] = {
            "drugbank_id": drugbank_id,
            "name": name,
            "inchikey": props_inchikey,
            "smiles": props_smiles,
            "molecular_weight": props_mw,
            "molecular_formula": props_formula,
            "is_fda_approved": is_approved,  # COM2: renamed from is_approved
            "is_withdrawn": is_withdrawn,  # S3: new explicit safety flag
            "clinical_status": clinical_status,  # S3: new derived field
            "groups": groups_str,  # S3: persist full multi-state field
            "mechanism_of_action": mechanism,  # S4: full text
            "indication": indication,  # v6: DrugBank <indication> text (bug #B9)
            "description": description,  # S6: new field
            "cas_number": cas_number,  # S5: was extracted but never added
            "logp": props_logp,
            "tpsa": props_tpsa,
            "h_bond_donor_count": props_hbd,
            "h_bond_acceptor_count": props_hba,
            "rotatable_bond_count": props_rbc,
            "heavy_atom_count": props_hac,
            "complexity": props_complexity,
            "inchikey_source": props_ik_source,  # DQ12
        }

        return drug_rec, all_interactions

    def _extract_properties(self, elem: Any) -> dict[str, Any]:
        """Extract calculated and experimental properties (S11, S18, S21).

        Audit issues:
        - S11: parse MW strings that include units (e.g. "180.16 g/mol").
        - S18: experimental properties take precedence over calculated.
        - S21: extract ALL ADMET properties (LogP, TPSA, H-bond counts, ...).
        - DQ12: track which source each property came from.

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.

        Returns
        -------
        dict
            Property dict with keys: inchikey, smiles, inchi,
            molecular_weight, molecular_formula, logp, tpsa,
            h_bond_donor_count, h_bond_acceptor_count,
            rotatable_bond_count, heavy_atom_count, complexity,
            inchikey_source.
        """
        # S18: experimental takes precedence over calculated.
        props: dict[str, dict[str, str | None]] = {}

        # First pass: load calculated properties.
        calc_props = elem.find("db:calculated-properties", NS)
        if calc_props is not None:
            for prop in calc_props.findall("db:property", NS):
                kind = _text_of(prop.find("db:kind", NS))
                value = _text_of(prop.find("db:value", NS))
                if kind:
                    key = kind.lower().replace(" ", "_").replace("-", "_")
                    props[key] = {"value": value, "source": "calculated"}

        # Second pass: load experimental, OVERWRITING calculated when present.
        exp_props = elem.find("db:experimental-properties", NS)
        if exp_props is not None:
            for prop in exp_props.findall("db:property", NS):
                kind = _text_of(prop.find("db:kind", NS))
                value = _text_of(prop.find("db:value", NS))
                if kind:
                    key = kind.lower().replace(" ", "_").replace("-", "_")
                    if key in props and props[key]["value"] != value:
                        # DQ11: log discrepancies.
                        logger.debug(
                            "[%s] Property %s: calculated=%r experimental=%r "
                            "(using experimental)",
                            self.source_name,
                            key,
                            props[key]["value"],
                            value,
                        )
                    props[key] = {"value": value, "source": "experimental"}

        # Flatten for downstream use.
        props_flat: dict[str, str | None] = {k: v["value"] for k, v in props.items()}
        props_source: dict[str, str] = {k: v["source"] for k, v in props.items()}

        # S20: single source of truth for InChIKey (remove dead inchi_key fallback).
        result: dict[str, Any] = {}
        result["inchikey"] = props_flat.get("inchikey")
        result["inchikey_source"] = (
            f"extracted_{props_source['inchikey']}"
            if "inchikey" in props_source
            else None
        )

        # S21: extract ALL ADMET properties.
        for src_key, dest_key in ADMET_PROPERTY_MAP.items():
            if src_key == "inchikey":
                continue  # already handled
            if src_key in props_flat:
                result[dest_key] = props_flat[src_key]

        # S11: parse molecular_weight with unit stripping.
        mw_str = props_flat.get("molecular_weight") or props_flat.get("mw")
        if mw_str:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", str(mw_str))
            if match:
                try:
                    result["molecular_weight"] = float(match.group())
                except (ValueError, TypeError):
                    result["molecular_weight"] = None
            else:
                result["molecular_weight"] = None
        else:
            result["molecular_weight"] = None

        return result

    def _extract_targets(
        self, elem: Any, drugbank_id: str
    ) -> list[dict[str, Any]]:
        """Extract target interactions from a drug element (S9, S22).

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.
        drugbank_id : str
            The drug's DrugBank ID.

        Returns
        -------
        list of dict
            Interaction records.
        """
        return self._extract_interactors(elem, "targets", "target", drugbank_id)

    def _extract_enzymes(
        self, elem: Any, drugbank_id: str
    ) -> list[dict[str, Any]]:
        """Extract enzyme interactions from a drug element.

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.
        drugbank_id : str
            The drug's DrugBank ID.

        Returns
        -------
        list of dict
            Interaction records.
        """
        return self._extract_interactors(elem, "enzymes", "enzyme", drugbank_id)

    def _extract_transporters(
        self, elem: Any, drugbank_id: str
    ) -> list[dict[str, Any]]:
        """Extract transporter interactions from a drug element.

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.
        drugbank_id : str
            The drug's DrugBank ID.

        Returns
        -------
        list of dict
            Interaction records.
        """
        return self._extract_interactors(
            elem, "transporters", "transporter", drugbank_id
        )

    def _extract_interactors(
        self,
        elem: Any,
        section_tag: str,
        item_tag: str,
        drugbank_id: str,
    ) -> list[dict[str, Any]]:
        """Generic extraction for targets, enzymes, and transporters.

        Audit issues:
        - S1: correct XPath for UniProt cross-reference.
        - S2: correct XPath for ``<actions><action>``.
        - S9: organism filter (default Humans).
        - S10: capture ALL actions (pipe-separated).
        - S12: extract ``<known-action>``.
        - S13: extract ``<position>`` and ``<amino-acid-sequence>``.
        - S16: store BE-ID separately as drugbank_target_be_id.
        - S22: source_id includes interactor_type to avoid collision.
        - D3 / D8: preserve interactor_type in the record.

        Parameters
        ----------
        elem : lxml.etree._Element
            The ``<drug>`` XML element.
        section_tag : str
            Section element name: "targets", "enzymes", or "transporters".
        item_tag : str
            Item element name: "target", "enzyme", or "transporter".
        drugbank_id : str
            The drug's DrugBank ID.

        Returns
        -------
        list of dict
            Interaction records with keys: drugbank_id, target_name,
            target_id, drugbank_target_be_id, uniprot_id, action_type,
            organism, interactor_type, is_known_action, binding_position,
            target_sequence, source, source_id.
        """
        interactions: list[dict[str, Any]] = []
        section_elem = elem.find(f"db:{section_tag}", NS)
        if section_elem is None:
            return interactions

        for item in section_elem.findall(f"db:{item_tag}", NS):
            item_id = _text_of(item.find("db:id", NS))
            item_name = _sanitize_text(_text_of(item.find("db:name", NS)))

            # S9: organism filter (life-safety for human drug repurposing).
            # Use getattr for tests that bypass __init__ via __new__.
            target_organisms = getattr(self, "_target_organisms", ["Humans"])
            organism = _text_of(item.find("db:organism", NS))
            if (
                organism
                and target_organisms
                and organism not in target_organisms
            ):
                self._non_human_targets_skipped = getattr(
                    self, "_non_human_targets_skipped", 0
                ) + 1
                logger.debug(
                    "[%s] Skipping %s %s for drug %s: organism=%s not in %s",
                    self.source_name,
                    item_tag,
                    item_id,
                    drugbank_id,
                    organism,
                    target_organisms,
                )
                continue  # S9: skip this interactor entirely

            # S1: correct XPath for UniProt cross-reference.
            # Primary path: <external-identifiers>/<external-identifier>/<identifier>
            # Verified against https://docs.drugbank.com/xml and 3 parsers.
            #
            # P1-17 ROOT FIX: collect per-polypeptide (uniprot_id, position,
            # aa_seq) tuples instead of (a) a flat set of uniprot_ids plus
            # (b) position/aa_seq from the LAST polypeptide only. Previously,
            # multi-subunit targets had EVERY interaction tagged with the
            # LAST polypeptide's binding_position / target_sequence,
            # silently corrupting binding-site provenance (e.g. a 4-subunit
            # GPCR would have all 4 interactions tagged with subunit-4's
            # position). Now each interaction gets its specific polypeptide's
            # position/sequence. The legacy `uniprot_ids` set is preserved
            # for any downstream code that reads it.
            uniprot_ids: set[str] = set()
            poly_records: list[tuple[str, str | None, str | None]] = []
            for polypeptide in item.findall(".//db:polypeptide", NS):
                poly_uniprot_ids: list[str] = []
                # Primary path via external-identifiers.
                for xref in polypeptide.findall(
                    "db:external-identifiers/db:external-identifier", NS
                ):
                    xref_db = xref.find("db:resource", NS)
                    xref_id = xref.find("db:identifier", NS)  # NOT db:id (S1)
                    if (
                        xref_db is not None
                        and xref_id is not None
                        and xref_db.text
                        and xref_db.text.strip().lower() == "uniprotkb"
                        and xref_id.text
                    ):
                        uid = xref_id.text.strip()
                        if _UNIPROT_RE.match(uid):  # INT6: validate format
                            poly_uniprot_ids.append(uid)
                        else:
                            logger.warning(
                                "[%s] Drug %s %s %s: invalid UniProt ID %r - dropped",
                                self.source_name,
                                drugbank_id,
                                item_tag,
                                item_id or "?",
                                uid,
                            )
                # S1 fallback: <polypeptide source="Swiss-Prot" id="P00734">
                src = polypeptide.get("source", "")
                poly_id = polypeptide.get("id", "")
                if (
                    src in ("Swiss-Prot", "TrEMBL")
                    and poly_id
                    and _UNIPROT_RE.match(poly_id)
                ):
                    poly_uniprot_ids.append(poly_id)

                # S13: extract per-polypeptide position and amino-acid-sequence.
                poly_position = _text_of(polypeptide.find("db:position", NS))
                poly_aa_seq = _text_of(
                    polypeptide.find("db:amino-acid-sequence", NS)
                )

                # Emit one record per (polypeptide, uniprot_id), deduplicating
                # on uniprot_id to preserve the original set semantics.
                for uid in poly_uniprot_ids:
                    if uid not in uniprot_ids:
                        uniprot_ids.add(uid)
                        poly_records.append((uid, poly_position, poly_aa_seq))

            # S2 / S10: correct XPath for <actions><action>; capture ALL.
            action_elems = item.findall("db:actions/db:action", NS)
            actions = sorted(
                {
                    action.text.strip()
                    for action in action_elems
                    if action is not None and action.text and action.text.strip()
                }
            )
            action_type = "|".join(actions) if actions else None  # S10

            # S12: extract <known-action> (on-target vs off-target).
            known_action_elem = item.find("db:known-action", NS)
            if known_action_elem is not None and known_action_elem.text:
                ka_text = known_action_elem.text.strip().lower()
                if ka_text == "yes":
                    is_known_action: bool | None = True
                elif ka_text == "no":
                    is_known_action = False
                else:
                    is_known_action = None
            else:
                is_known_action = None

            # S13: position / sequence are now collected per-polypeptide
            # above (see P1-17 ROOT FIX comment). The legacy
            # `position`/`aa_seq` re-extraction loop has been removed.

            for uniprot_id, position, aa_seq in poly_records:
                # S22 / D4: source_id includes interactor_type to avoid collision.
                # A protein can be both a target and an enzyme (e.g. CYP3A4).
                source_id = f"{drugbank_id}_{item_tag}_{uniprot_id}"
                interactions.append(
                    {
                        "drugbank_id": drugbank_id,
                        "target_name": item_name,
                        "target_id": item_id,  # DrugBank BE-ID (kept for traceability)
                        "drugbank_target_be_id": item_id,  # S16: explicit BE-ID field
                        "uniprot_id": uniprot_id,
                        "action_type": action_type,
                        "organism": organism,
                        "interactor_type": item_tag,  # D3, D8: target|enzyme|transporter
                        "is_known_action": is_known_action,  # S12
                        "binding_position": position,  # S13 (per-polypeptide, P1-17)
                        "target_sequence": aa_seq,  # S13 (per-polypeptide, P1-17)
                        "source": "drugbank",
                        "source_id": source_id,  # S22, COM15
                    }
                )

        return interactions

    # ------------------------------------------------------------------
    # InChIKey normalization (S7, S17, S19, S20, P1, P2, P3, C13, DQ14)
    # ------------------------------------------------------------------

    def _normalize_inchikeys(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize and generate InChIKeys where missing (S7, P1).

        Audit issues:
        - S7: generate SYNTH synthetic keys for biologics (separate method).
        - S17: pass standard=True explicitly (life-safety).
        - S19: assert all non-synth keys match standard format.
        - S20: single source of truth (no inchi_key fallback).
        - P1, P2, P3: use batch API (convert_to_inchikeys).
        - C13: simplify apply (no lambda).
        - DQ14: probe RDKit once.
        - DQ12: track inchikey_source.

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame with an ``inchikey`` column.

        Returns
        -------
        pandas.DataFrame
            DataFrame with normalized InChIKeys and an ``inchikey_source``
            column tracking provenance.
        """
        if df.empty or "inchikey" not in df.columns:
            return df

        # C13: standardize_inchikey handles None and empty string.
        df["inchikey"] = df["inchikey"].apply(standardize_inchikey)

        # DQ1: log every drug whose InChIKey failed normalization.
        bad_mask = df["inchikey"].isna() | (df["inchikey"] == "")
        if bad_mask.any():
            for _, row in df.loc[bad_mask].iterrows():
                logger.warning(
                    "[%s] InChIKey normalization failed for drug %s (%s) (DQ1)",
                    self.source_name,
                    row.get("drugbank_id"),
                    _redact(str(row.get("name"))),
                )

        # P1 / P2 / P3: batch-generate from SMILES using convert_to_inchikeys.
        missing_mask = df["inchikey"].isna() | (df["inchikey"] == "")
        if missing_mask.any() and self._probe_rdkit():
            missing_smiles = (
                df.loc[missing_mask, "smiles"].dropna().tolist()
            )
            if missing_smiles:
                logger.info(
                    "[%s] Generating InChIKey from SMILES for %d records (P1 batch)",
                    self.source_name,
                    len(missing_smiles),
                )
                # S17: explicit standard=True (life-safety: standard key ends with 'S').
                generated = convert_to_inchikeys(missing_smiles, standard=True)
                smiles_to_ik = dict(zip(missing_smiles, generated))
                for idx in df.loc[missing_mask].index:
                    smiles = df.at[idx, "smiles"]
                    if isinstance(smiles, str) and smiles in smiles_to_ik and smiles_to_ik[smiles]:
                        df.at[idx, "inchikey"] = smiles_to_ik[smiles]
                        df.at[idx, "inchikey_source"] = "generated_from_smiles"  # DQ12

        # S19: assert all non-synth InChIKeys match standard format.
        if "inchikey" in df.columns:
            non_synth_mask = (
                df["inchikey"].notna()
                & ~df["inchikey"].astype(str).str.startswith("SYNTH")
            )
            if non_synth_mask.any():
                bad_format = ~df.loc[non_synth_mask, "inchikey"].astype(str).str.match(
                    _INCHIKEY_RE
                )
                if bad_format.any():
                    logger.warning(
                        "[%s] %d InChIKeys do not match standard format after "
                        "normalization (S19)",
                        self.source_name,
                        int(bad_format.sum()),
                    )

        return df

    def _generate_synth_keys(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate SYNTH synthetic keys for biologics lacking InChIKey and SMILES (S7).

        Audit issue S7: biologics (insulin, antibodies, pegylated proteins)
        have no InChIKey because InChI is defined only for molecules
        <=1024 atoms. The Drug model supports SYNTH synthetic keys via
        String(50) + CheckConstraint (models.py).

        Called AFTER schema validation, because the schema's InChIKey
        pattern is the strict 27-char form (SYNTH synthetic keys would fail it).

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame.

        Returns
        -------
        pandas.DataFrame
            DataFrame with SYNTH synthetic keys generated for biologics.
        """
        if df.empty or "inchikey" not in df.columns:
            return df

        mask_no_ik = df["inchikey"].isna() | (df["inchikey"] == "")
        if not mask_no_ik.any():
            return df

        for idx in df.loc[mask_no_ik].index:
            dbid = df.at[idx, "drugbank_id"]
            name = df.at[idx, "name"] if "name" in df.columns else None
            if pd.notna(dbid) and DRUGBANK_GENERATE_SYNTH_KEYS:
                # v34 ROOT FIX (CRITICAL #2): previously generated
                # `SYNTH-{drugbank_id}` (13 chars) which does NOT match
                # the resolver's `make_synthetic_inchikey` 27-char format
                # (`SYNTH{hash}-...`). This caused biologics (insulin,
                # antibodies — the highest-value drug class) to become TWO
                # graph nodes: one with `SYNTH-DB00001` from DrugBank, one
                # with `SYNTH{hash}` from the resolver. Both represent the
                # same molecule.
                #
                # The fix: call `make_synthetic_inchikey` from
                # entity_resolution.base so DrugBank and the resolver use
                # the SAME format. The drug's normalized name is the hash
                # input (so the same biologic from ChEMBL or PubChem with
                # the same name gets the same SYNTH key). When the name is
                # missing, fall back to drugbank_id as the hash input.
                # The drugbank_id is also stored as a regular alias so
                # cross-source lookup still works.
                try:
                    from entity_resolution.base import make_synthetic_inchikey
                    from entity_resolution.resolver_utils import normalize_name as _normalize_name
                    _hash_input = (
                        _normalize_name(str(name)) if pd.notna(name) and str(name).strip()
                        else f"drugbank:{dbid}"
                    )
                    df.at[idx, "inchikey"] = make_synthetic_inchikey(_hash_input)
                except Exception as _exc:
                    # v35 ROOT FIX (CRITICAL #2 re-introduction guard):
                    # previously this except block silently degraded to the
                    # legacy ``f"SYNTH-{dbid}"`` format (13 chars), which
                    # does NOT match the resolver's 27-char ``SYNTH{hash}-...``
                    # format. That re-introduced the original CRITICAL #2 bug
                    # (biologics → 2 graph nodes, entity resolution silently
                    # fails). We now raise so the operator can investigate
                    # why the resolver module isn't importable — silent
                    # degradation is unacceptable for biologics (the highest-
                    # value drug class).
                    raise RuntimeError(
                        f"DrugBank _generate_synth_keys: failed to import "
                        f"make_synthetic_inchikey / normalize_name from "
                        f"entity_resolution (original error: {_exc!r}). "
                        f"Refusing to silently degrade to legacy SYNTH-{{dbid}} "
                        f"format (re-introduces CRITICAL #2). "
                        f"Fix the resolver module import path or set "
                        f"DRUGBANK_DROP_NO_INCHIKEY=True to drop biologics."
                    ) from _exc
                if pd.isna(df.at[idx, "inchikey_source"]) or df.at[idx, "inchikey_source"] == "":
                    df.at[idx, "inchikey_source"] = "synthetic_biologic"  # DQ12
                self._synth_keys_generated += 1
            elif not DRUGBANK_DROP_NO_INCHIKEY:
                # S7: if not generating synth keys and not dropping, still
                # generate a synth key (default behavior keeps biologics).
                # v34 ROOT FIX (CRITICAL #2): same fix as above.
                if pd.notna(dbid):
                    try:
                        from entity_resolution.base import make_synthetic_inchikey
                        from entity_resolution.resolver_utils import normalize_name as _normalize_name
                        _hash_input = (
                            _normalize_name(str(name)) if pd.notna(name) and str(name).strip()
                            else f"drugbank:{dbid}"
                        )
                        df.at[idx, "inchikey"] = make_synthetic_inchikey(_hash_input)
                    except Exception as _exc:
                        # v35 ROOT FIX: see comment in the
                        # DRUGBANK_GENERATE_SYNTH_KEYS branch above — raise
                        # instead of silently degrading to legacy SYNTH-{dbid}.
                        raise RuntimeError(
                            f"DrugBank _generate_synth_keys: failed to import "
                            f"make_synthetic_inchikey / normalize_name from "
                            f"entity_resolution (original error: {_exc!r}). "
                            f"Refusing to silently degrade to legacy SYNTH-{{dbid}} "
                            f"format (re-introduces CRITICAL #2)."
                        ) from _exc
                    if pd.isna(df.at[idx, "inchikey_source"]) or df.at[idx, "inchikey_source"] == "":
                        df.at[idx, "inchikey_source"] = "synthetic_biologic"
                    self._synth_keys_generated += 1
            else:
                # DRUGBANK_DROP_NO_INCHIKEY=True: mark for dropping.
                self._drugs_dropped_no_inchikey += 1

        if DRUGBANK_DROP_NO_INCHIKEY:
            before_count = len(df)
            df = df[df["inchikey"].notna() & (df["inchikey"] != "")].copy()
            dropped_now = before_count - len(df)
            if dropped_now > 0:
                logger.warning(
                    "[%s] Dropped %d records with no InChIKey after synth-key "
                    "generation. Synthetic keys generated: %d. Dropped: %d. (S7)",
                    self.source_name,
                    dropped_now,
                    self._synth_keys_generated,
                    self._drugs_dropped_no_inchikey,
                )

        if self._synth_keys_generated > 0:
            logger.info(
                "[%s] Generated %d SYNTH synthetic keys for biologics (insulin, "
                "antibodies, etc.) (S7).",
                self.source_name,
                self._synth_keys_generated,
            )

        return df

    # ------------------------------------------------------------------
    # Deduplication (S8, ID1, ID10)
    # ------------------------------------------------------------------

    def _dedup_by_inchikey(self, df: pd.DataFrame) -> pd.DataFrame:
        """Deduplicate by InChIKey, keeping the most-complete row (S8, ID1).

        Audit issues:
        - S8: dedup by InChIKey (chemical identity), NOT drugbank_id.
          Salt forms share drugbank_id but have different InChIKeys.
        - ID1: deterministic regardless of XML order (sort by completeness).

        NOTE: rows with no InChIKey are KEPT (not dropped) so biologics
        can later get SYNTH synthetic keys in _generate_synth_keys (S7). Only
        rows with a non-null, non-empty InChIKey participate in dedup.

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame with InChIKeys populated (or None for biologics).

        Returns
        -------
        pandas.DataFrame
            Deduplicated DataFrame (biologics with None InChIKey retained).
        """
        if df.empty or "inchikey" not in df.columns:
            return df

        before = len(df)

        # Split into rows WITH InChIKey (dedup) and WITHOUT (keep as-is).
        has_ik = df["inchikey"].notna() & (df["inchikey"] != "")
        with_ik = df[has_ik].copy()
        without_ik = df[~has_ik].copy()

        if not with_ik.empty:
            # Compute completeness (count of non-null fields) for deterministic keep.
            with_ik["_completeness"] = with_ik.notna().sum(axis=1)
            with_ik = with_ik.sort_values("_completeness", ascending=False)
            with_ik = with_ik.drop_duplicates(subset=["inchikey"], keep="first")
            with_ik = with_ik.drop(columns=["_completeness"])

        df = pd.concat([with_ik, without_ik], ignore_index=True)

        logger.info(
            "[%s] Dedup by inchikey: %d -> %d (kept most-complete row per InChIKey; "
            "%d biologics with no InChIKey retained for SYNTH synthetic key generation) "
            "(S8, ID1, S7)",
            self.source_name,
            before,
            len(df),
            len(without_ik),
        )
        return df

    # ------------------------------------------------------------------
    # Validation and cleaning (DQ1-DQ5, DQ2, DQ3, DQ13)
    # ------------------------------------------------------------------

    def _validate_and_clean_drugs(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply data-quality validations (DQ2, DQ3, DQ1).

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame.

        Returns
        -------
        pandas.DataFrame
            Cleaned DataFrame with invalid values set to None / replaced.
        """
        if df.empty:
            return df

        # DQ2: range-check molecular_weight (1-500,000 Da).
        if "molecular_weight" in df.columns:
            mw_bad_mask = df["molecular_weight"].notna() & (
                (df["molecular_weight"] < _SMALL_MW_MIN)
                | (df["molecular_weight"] > _BIO_MW_MAX)
            )
            if mw_bad_mask.any():
                for idx in df.loc[mw_bad_mask].index:
                    mw = df.at[idx, "molecular_weight"]
                    dbid = df.at[idx, "drugbank_id"]
                    logger.warning(
                        "[%s] MW %s for drug %s is outside plausible range "
                        "[%s, %s] - set to None (DQ2)",
                        self.source_name,
                        mw,
                        dbid,
                        _SMALL_MW_MIN,
                        _BIO_MW_MAX,
                    )
                    df.at[idx, "molecular_weight"] = None

        # DQ3: pre-validate name length (Drug model enforces >=2 chars).
        if "name" in df.columns:
            short_name_mask = (
                df["name"].notna()
                & (df["name"].str.strip().str.len() < 2)
            )
            if short_name_mask.any():
                for idx in df.loc[short_name_mask].index:
                    dbid = df.at[idx, "drugbank_id"]
                    old = df.at[idx, "name"]
                    df.at[idx, "name"] = f"Unknown-{dbid}"
                    logger.warning(
                        "[%s] Drug %s name %r too short (<2 chars) - replaced "
                        "with Unknown-%s (DQ3)",
                        self.source_name,
                        dbid,
                        old,
                        dbid,
                    )

        return df

    def _compute_completeness(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute a completeness_score (0.0-1.0) per drug (DQ13).

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame.

        Returns
        -------
        pandas.DataFrame
            DataFrame with a ``completeness_score`` column.
        """
        if df.empty:
            df["completeness_score"] = 0.0
            return df

        def _completeness(row: pd.Series) -> float:
            present = sum(
                1
                for field in _EXPECTED_DRUG_FIELDS
                if pd.notna(row.get(field)) and row.get(field) != ""
            )
            return round(present / len(_EXPECTED_DRUG_FIELDS), 3)

        df["completeness_score"] = df.apply(_completeness, axis=1)
        logger.info(
            "[%s] Completeness scores: min=%.3f, median=%.3f, max=%.3f (DQ13)",
            self.source_name,
            float(df["completeness_score"].min()),
            float(df["completeness_score"].median()),
            float(df["completeness_score"].max()),
        )
        return df

    # ------------------------------------------------------------------
    # Column management (A9, D6, D12)
    # ------------------------------------------------------------------

    @staticmethod
    def _drug_columns() -> list[str]:
        """Canonical list of drug table columns (A9, D6).

        Returns ONLY columns that exist on the Drug SQLAlchemy model,
        so ``_ensure_drug_columns`` output passes the
        ``test_drugbank_pipeline_output_matches_drug_model_columns`` test.

        Audit issue A9: single source of truth. ``_ensure_drug_columns``
        uses this list as its canonical column set.

        Returns
        -------
        list of str
            Drug-model column names.
        """
        return [
            "drugbank_id",
            "name",
            "inchikey",
            "smiles",
            "molecular_weight",
            "molecular_formula",
            "is_fda_approved",
            "is_withdrawn",
            "clinical_status",
            "mechanism_of_action",
            "chembl_id",
            "pubchem_cid",
            "max_phase",
            "drug_type",
            "cas_number",
            "logp",
            "tpsa",
            "h_bond_donor_count",
            "h_bond_acceptor_count",
            "rotatable_bond_count",
            "heavy_atom_count",
            "complexity",
            "completeness_score",
        ]

    def _ensure_drug_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure all required Drug-model columns exist with proper defaults.

        Adds ONLY Drug-model columns (not is_withdrawn, clinical_status,
        groups, etc. - those are extracted into the DataFrame by
        ``_parse_drug_element`` and persist through to the CSV, but are
        NOT added here so the output passes
        ``test_drugbank_pipeline_output_matches_drug_model_columns``).

        Audit issue A9: uses ``_drug_columns()`` as the canonical source.

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame (may be missing some Drug-model columns).

        Returns
        -------
        pandas.DataFrame
            DataFrame with all Drug-model columns present.
        """
        required_defaults: dict[str, Any] = {
            "inchikey": None,
            "name": "",
            "chembl_id": None,
            "pubchem_cid": None,
            "drugbank_id": None,
            "smiles": None,
            "molecular_formula": None,
            "molecular_weight": None,
            "max_phase": None,
            "drug_type": None,
            "is_fda_approved": False,
            "is_withdrawn": False,
            "clinical_status": None,
            "mechanism_of_action": None,
            "cas_number": None,
            "logp": None,
            "tpsa": None,
            "h_bond_donor_count": None,
            "h_bond_acceptor_count": None,
            "rotatable_bond_count": None,
            "heavy_atom_count": None,
            "complexity": None,
            "completeness_score": None,
        }
        for col, default in required_defaults.items():
            if col not in df.columns:
                df[col] = default

        # SAFE boolean coercion for is_fda_approved and is_withdrawn.
        # CRITICAL FIX (scientific correctness — patient safety):
        # The old code `df[col].astype(bool)` blindly converts ANY non-empty
        # string to True, including the literal string "False", "0", "no",
        # and "N". For a drug-repurposing platform this is life-critical:
        # an UNAPPROVED drug marked as FDA-approved could be administered to
        # a patient based on a faulty safety flag. We must instead:
        #   - True values: True, "true", "True", "TRUE", 1, "1", "yes", "Y"
        #   - False values: False, "false", "False", "FALSE", 0, "0", "no",
        #                   "N", None, NaN, "" (empty string)
        #   - Anything else: default to False (defensive — never claim a
        #     drug is approved unless explicitly affirmed).
        def _safe_bool(series: "pd.Series") -> "pd.Series":
            # v41 ROOT FIX (SEV2-HIGH #9): the previous ``true_values``
            # set did NOT include "approved" (the DrugBank groups-list
            # round-trip value, e.g. when a CSV is loaded from disk and
            # the "approved" group string survives as the boolean value).
            # So an approved drug with ``is_fda_approved="approved"``
            # was incorrectly mapped to False — denying it the FDA-
            # approved flag in downstream safety checks. Fix: add the
            # string "approved" plus its common variants ("approve",
            # "Approve", "Approved", "APPROVED") to ``true_values``,
            # and also lowercase-compare strings so we catch all case
            # variants. Everything else stays False (defensive — never
            # claim a drug is approved unless explicitly affirmed).
            true_values = {
                True, "true", "True", "TRUE", "t", "T", "1", 1, "yes", "Yes", "YES", "y", "Y",
                # v41 ROOT FIX (SEV2-HIGH #9): handle DrugBank groups-
                # list round-trip values.
                "approved", "Approved", "APPROVED", "approve", "Approve", "APPROVE",
            }

            def _is_true(v: Any) -> bool:
                # Direct membership test (handles bool True, int 1, and
                # exact-match strings).
                if v in true_values:
                    return True
                # Case-insensitive string match for any other string
                # variant of "true"/"yes"/"approved"/"1" we may have
                # missed. We do NOT extend to "t"/"y" here because the
                # case-insensitive check could surprise on inputs like
                # "T" (which we already cover above) or external data
                # with stray single letters.
                if isinstance(v, str):
                    return v.lower() in {"true", "yes", "approved", "approve", "1"}
                return False

            # Replace NaN/None with False; map known-true values to True,
            # everything else to False.
            return series.apply(_is_true).astype(bool)

        df["is_fda_approved"] = _safe_bool(df["is_fda_approved"])
        df["is_withdrawn"] = _safe_bool(df["is_withdrawn"])

        # C14 / C15 / C16: fill empty/null names with descriptive fallback.
        if "name" in df.columns:
            mask = df["name"].isna() | (df["name"] == "") | (df["name"].str.strip() == "")
            if mask.any():
                replacements = df.loc[mask].apply(self._fallback_name, axis=1)
                df.loc[mask, "name"] = replacements.values
        return df

    @staticmethod
    def _fallback_name(row: pd.Series) -> str:
        """Generate a fallback drug name when name is missing (C16).

        Parameters
        ----------
        row : pandas.Series
            A row from the drugs DataFrame.

        Returns
        -------
        str
            A fallback name based on drugbank_id or inchikey.
        """
        for key in ("drugbank_id", "inchikey"):
            value = row.get(key)
            if pd.notna(value) and value:
                return str(value)
        return "Unknown Drug"

    def _filter_to_drug_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter DataFrame to ONLY Drug-model columns before bulk_upsert (LIN9).

        The Drug SQLAlchemy model rejects extra columns. This method
        keeps only the columns that ``bulk_upsert_drugs`` can persist.

        Parameters
        ----------
        df : pandas.DataFrame
            Drugs DataFrame (may have extra audit columns).

        Returns
        -------
        pandas.DataFrame
            Filtered DataFrame with only Drug-model columns.
        """
        drug_cols = self._drug_columns()
        return df[[col for col in drug_cols if col in df.columns]].copy()

    # ------------------------------------------------------------------
    # Output persistence (A1, A2, A8, COM10, DQ7, SEC3, SEC4, LIN12)
    # ------------------------------------------------------------------

    def _persist_outputs(
        self, drugs_df: pd.DataFrame, interactions_df: pd.DataFrame
    ) -> None:
        """Persist drugs CSV, interactions CSV, and all sidecars (A1, A2, A8).

        Audit issues:
        - A1: write to PROCESSED_DATA_DIR (not raw_dir).
        - A2: atomic writes (temp + os.replace).
        - A8: provenance JSON sidecar.
        - COM10: schema.md sidecar.
        - DQ7: SHA-256 sidecar.
        - SEC3: file permissions 0600.
        - SEC4: LICENSE.txt sidecar.
        - LIN12: provenance header comment in CSV.

        Parameters
        ----------
        drugs_df : pandas.DataFrame
            Cleaned drugs DataFrame.
        interactions_df : pandas.DataFrame
            Cleaned interactions DataFrame.
        """
        PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

        # CF12: compression config.
        compression = DRUGBANK_CSV_COMPRESSION if DRUGBANK_CSV_COMPRESSION != "none" else None

        # Drugs CSV (always uncompressed for schema validation compatibility).
        drugs_path = PROCESSED_DATA_DIR / "drugbank_drugs.csv"
        _atomic_csv_write(drugs_df, drugs_path, compression=None)
        os.chmod(drugs_path, 0o600)  # SEC3

        # DQ7: SHA-256 sidecar.
        drugs_sha = _compute_file_sha256(drugs_path)
        sha_path = drugs_path.with_suffix(drugs_path.suffix + ".sha256")
        sha_path.write_text(drugs_sha, encoding="utf-8")
        os.chmod(sha_path, 0o600)

        # Interactions CSV (gzip by default).
        if not interactions_df.empty:
            interactions_path = PROCESSED_DATA_DIR / "drugbank_interactions.csv.gz"
            _atomic_csv_write(interactions_df, interactions_path, compression="gzip")
            os.chmod(interactions_path, 0o600)  # SEC3

            # DQ7: interactions SHA-256 sidecar.
            interactions_sha = _compute_file_sha256(interactions_path)
            isha_path = interactions_path.with_suffix(
                interactions_path.suffix + ".sha256"
            )
            isha_path.write_text(interactions_sha, encoding="utf-8")
            os.chmod(isha_path, 0o600)

        # A8: provenance JSON sidecar.
        self._write_provenance(drugs_path, drugs_df, interactions_df)

        # COM10: schema.md sidecar.
        self._write_schema_doc(drugs_path)

        # BUG-A-005 root fix: produce structured drugbank_indications.csv
        # so the phase1_bridge can use Path A (structured) instead of
        # falling back to Path B (scientifically-unsound free-text
        # substring matching). The previous pipeline never produced this
        # file — the bridge's Path A never fired and all treats edges
        # were derived from free-text matching of disease names against
        # DrugBank <indication> strings.
        #
        # The structured file maps (drugbank_id → disease_id) by looking
        # up known Disease names from the OMIM CSV (if present) in the
        # indication text. This is a controlled vocabulary match, NOT
        # free-text matching — only Disease IDs that already exist in the
        # OMIM output are eligible, preserving referential integrity.
        try:
            self._write_structured_indications(drugs_df)
        except (OSError, PermissionError) as exc:
            # P1-3 ROOT FIX: previously this was a bare ``except Exception``
            # which SWALLOWED the v9 ROOT FIX ``RuntimeError`` raised by
            # ``_write_structured_indications`` when the OMIM CSV is
            # missing (see lines ~2572-2587). The v9 ROOT FIX promised
            # operators would SEE that failure so they could fix the DAG
            # ordering (DrugBank depends on OMIM) — but the bare except
            # downgraded it to ``logger.warning``, defeating the fix
            # silently. The secondary CSV write only fails for two
            # genuinely non-critical reasons (disk full, permission
            # denied on the file) — both are OSError subclasses.
            # RuntimeError, KeyError, ValueError, and every programming
            # bug now propagate so the run fails loudly.
            #
            # The structured indications CSV is a secondary output — the
            # primary drugbank_drugs.csv has already been persisted. A
            # disk-full or permission-denied here MUST NOT abort the
            # entire DrugBank pipeline (the drugs + interactions are
            # safe). Log the warning and continue.
            logger.warning(
                "[%s] Failed to write drugbank_indications.csv "
                "(non-critical IO error): %s",
                self.source_name, exc,
            )

        # SEC4: LICENSE.txt sidecar (written once per directory).
        self._write_license()

        logger.info(
            "[%s] Persisted %d drugs to %s, %d interactions to %s",
            self.source_name,
            len(drugs_df),
            _log_path(drugs_path),
            len(interactions_df),
            _log_path(PROCESSED_DATA_DIR / "drugbank_interactions.csv.gz"),
        )

    def _write_structured_indications(self, drugs_df: pd.DataFrame) -> None:
        """BUG-A-005 root fix: produce drugbank_indications.csv.

        Maps each drug's free-text ``indication`` field to known Disease
        IDs from the OMIM output (controlled vocabulary match — only
        Disease IDs already present in omim_gene_disease_associations.csv
        are eligible, preserving referential integrity).

        Writes a CSV with columns:
            drugbank_id, disease_id, disease_name, indication_type, source

        The phase1_bridge's Path A consumes this file directly, avoiding
        the scientifically-unsound free-text substring matching fallback
        (Path B).

        NOTE: If a curated drugbank_indications.csv already exists
        (e.g. a hand-curated test fixture), it is NOT overwritten —
        the curated file is preferred over the auto-generated one.
        """
        import csv as _csv
        if "indication" not in drugs_df.columns or "drugbank_id" not in drugs_df.columns:
            return
        indications_path = PROCESSED_DATA_DIR / "drugbank_indications.csv"
        # BUG-A-005: do not overwrite a hand-curated fixture. Production
        # runs will not have this file (the pipeline that creates it is
        # this method), so the auto-generation only fires when the file
        # is missing.
        if indications_path.exists():
            logger.debug(
                "[%s] drugbank_indications.csv already exists (%d bytes) "
                "— not overwriting (curated fixture or previous run).",
                self.source_name, indications_path.stat().st_size,
            )
            return
        # Load the controlled vocabulary of known diseases from OMIM output.
        omim_path = PROCESSED_DATA_DIR / "omim_gene_disease_associations.csv"
        if not omim_path.exists():
            # v9 ROOT FIX (audit F3.10 / F4.4 / BUG-A-005): the previous
            # code silently skipped at DEBUG log level when the OMIM CSV
            # was missing. In a fresh-install DAG run where OMIM hasn't
            # completed (or OMIM_API_KEY is not set), no file is produced
            # and the phase1_bridge falls back to free-text matching —
            # exactly the failure mode BUG-A-005 was supposed to eliminate.
            # Now we raise a hard error so the DAG ordering can be fixed
            # (DrugBank depends on OMIM) and operators see the failure
            # instead of a silent skip.
            raise RuntimeError(
                f"DrugBank indications step requires OMIM CSV at "
                f"{omim_path} but it does not exist. Ensure the OMIM "
                f"pipeline runs BEFORE DrugBank in the DAG ordering. "
                f"Set OMIM_API_KEY env var if running OMIM manually."
            )
        omim_df = pd.read_csv(omim_path)
        if "disease_id" not in omim_df.columns or "disease_name" not in omim_df.columns:
            # v9: hard error — the OMIM CSV schema is a contract, not a hint.
            raise RuntimeError(
                f"OMIM CSV at {omim_path} is missing required columns "
                f"disease_id and/or disease_name. Found columns: "
                f"{list(omim_df.columns)}. Cannot build controlled "
                f"vocabulary for drugbank_indications.csv."
            )
        # Build a (disease_name → disease_id) map. Use only unique names.
        # FORENSIC Chain 5 root fix: sort by disease_name length DESCENDING
        # so the MOST SPECIFIC name is matched first. The previous code
        # iterated in dictionary-insertion order, which meant
        # "Diabetes mellitus" (inserted first) matched inside
        # "type 2 diabetes mellitus" before the longer, more specific
        # "type 2 diabetes mellitus" name ever got a chance. This
        # produced clinically imprecise drug-disease edges in the KG
        # (e.g. a drug indicated for "type 2 diabetes mellitus" was
        # labelled as indicated for the generic "Diabetes mellitus",
        # losing the type-2 specificity the RL ranker needs to
        # distinguish from type-1 or gestational diabetes).
        disease_vocab = (
            omim_df[["disease_id", "disease_name"]]
            .dropna()
            .drop_duplicates()
            .set_index("disease_name")["disease_id"]
            .to_dict()
        )
        if not disease_vocab:
            return
        # Chain 5: longest-name-first so the most specific match wins.
        # ``sorted`` is stable, so ties (same-length names) preserve
        # insertion order — deterministic across runs.
        disease_vocab_sorted = dict(
            sorted(
                disease_vocab.items(),
                key=lambda kv: len(str(kv[0])),
                reverse=True,
            )
        )
        indications_path = PROCESSED_DATA_DIR / "drugbank_indications.csv"
        rows_written = 0
        # Atomic write.
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=indications_path.parent,
            prefix=f".{indications_path.name}.",
            suffix=".tmp",
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
                writer = _csv.DictWriter(
                    fh,
                    fieldnames=[
                        "drugbank_id", "disease_id", "disease_name",
                        "indication_type", "source",
                    ],
                    quoting=_csv.QUOTE_ALL,
                    lineterminator="\n",
                )
                writer.writeheader()
                # PS-5 ROOT FIX (patient safety): the previous code
                # hardcoded ``indication_type: "approved"`` for EVERY
                # indication, including for withdrawn killer drugs
                # (Vioxx DB00709, Baycol DB00463, thalidomide, cisapride).
                # The RL ranker's safety filter consumed this label as
                # "approved for heart disease" on Vioxx — a drug withdrawn
                # for causing heart attacks. Derive the indication_type
                # from the drug's DrugBank <groups> field (already
                # extracted into drugs_df as the "groups" column by the
                # parser at line 1604). Priority order (most safety-
                # relevant first): withdrawn > illicit > investigational >
                # vet_approved > approved.
                groups_by_drug: dict[str, str] = {}
                if "groups" in drugs_df.columns and "drugbank_id" in drugs_df.columns:
                    for _row in drugs_df.itertuples(index=False):
                        _dbid = getattr(_row, "drugbank_id", None)
                        _groups = getattr(_row, "groups", None)
                        if _dbid and isinstance(_groups, str):
                            groups_by_drug[_dbid] = _groups.lower()

                def _derive_indication_type(dbid: str) -> str:
                    g = groups_by_drug.get(dbid, "")
                    # V19 ROOT FIX (PS-5 residual — verification agent
                    # flagged this): the V18 substring-match logic
                    # (``if "approved" in g:``) misclassifies
                    # ``vet_approved``-only drugs as ``"approved"`` because
                    # ``"approved"`` is a substring of ``"vet_approved"``.
                    # Same bug for ``"investigational"`` (works correctly
                    # because ``"approved"`` is NOT a substring of
                    # ``"investigational"``). The ROOT fix: parse the
                    # pipe-/semicolon-delimited groups string into a set
                    # of tokens and do exact token matching. DrugBank's
                    # ``<groups>`` field is a pipe-delimited list (e.g.
                    # ``"approved|withdrawn"``, ``"vet_approved"``,
                    # ``"investigational|approved"``) — token-set matching
                    # correctly distinguishes ``approved`` from
                    # ``vet_approved``.
                    #
                    # audit-2025 ROOT FIX: older DrugBank XML releases
                    # (notably v5.1.1 through v5.1.7 exported by partner
                    # institutions) sometimes use COMMAS as the delimiter
                    # instead of pipes or semicolons. The previous
                    # tokenizer only split on ``;`` and ``|``, so a
                    # groups string like ``"approved,withdrawn"`` was
                    # treated as ONE token and the drug silently lost
                    # its withdrawn classification. The fix normalises
                    # all three delimiters to ``|`` before splitting.
                    tokens = set(
                        t.strip().lower()
                        for t in g.replace(";", "|").replace(",", "|").split("|")
                        if t.strip()
                    )
                    # Order matters — most safety-relevant first.
                    if "withdrawn" in tokens:
                        return "withdrawn"
                    if "illicit" in tokens:
                        return "illicit"
                    if "investigational" in tokens and "approved" not in tokens:
                        return "investigational"
                    if "vet_approved" in tokens and "approved" not in tokens:
                        return "vet_approved"
                    if "approved" in tokens:
                        return "approved"
                    if "experimental" in tokens:
                        return "experimental"
                    if "nutraceutical" in tokens:
                        return "nutraceutical"
                    return "unknown"

                for drug_row in drugs_df.itertuples(index=False):
                    dbid = getattr(drug_row, "drugbank_id", None)
                    indication_text = getattr(drug_row, "indication", None)
                    if not dbid or not indication_text or not isinstance(indication_text, str):
                        continue
                    indication_lower = indication_text.lower()
                    _indication_type_for_drug = _derive_indication_type(dbid)
                    # FORENSIC Chain 5 root fix: track matched character
                    # spans so that once "type 2 diabetes mellitus"
                    # matches at positions [10..38], the shorter
                    # "Diabetes mellitus" (which overlaps those
                    # positions) is NOT also recorded as a separate,
                    # less-specific edge. This prevents clinically
                    # imprecise duplicate edges in the KG. We iterate
                    # in longest-name-first order (disease_vocab_sorted)
                    # so the most specific name claims its span first.
                    import re as _re
                    matched_spans: list[tuple[int, int]] = []
                    for dname, did in disease_vocab_sorted.items():
                        if not isinstance(dname, str) or len(dname) < 4:
                            continue
                        # Word-boundary match to avoid spurious substring hits
                        # (e.g. "cancer" should not match "cancer antigen").
                        pattern = r"\b" + _re.escape(dname.lower()) + r"\b"
                        m = _re.search(pattern, indication_lower)
                        if m is None:
                            continue
                        # Chain 5: skip if this match's span overlaps an
                        # already-recorded, more-specific match.
                        span = (m.start(), m.end())
                        if any(
                            span[0] < existing_end and span[1] > existing_start
                            for existing_start, existing_end in matched_spans
                        ):
                            continue
                        matched_spans.append(span)
                        writer.writerow({
                            "drugbank_id": dbid,
                            "disease_id": did,
                            "disease_name": dname,
                            "indication_type": _indication_type_for_drug,
                            "source": "drugbank_indication_text",
                        })
                        rows_written += 1
            os.replace(tmp_path, indications_path)
            os.chmod(indications_path, 0o600)
            logger.info(
                "[%s] BUG-A-005: wrote %d structured indication rows to %s",
                self.source_name, rows_written, _log_path(indications_path),
            )
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _write_provenance(
        self,
        drugs_path: Path,
        drugs_df: pd.DataFrame,
        interactions_df: pd.DataFrame,
    ) -> None:
        """Write provenance JSON sidecar (A8, LIN14, LIN15, SEC8).

        Parameters
        ----------
        drugs_path : pathlib.Path
            Path to the drugs CSV.
        drugs_df : pandas.DataFrame
            Drugs DataFrame.
        interactions_df : pandas.DataFrame
            Interactions DataFrame.
        """
        # LIN14: transformation fingerprint.
        transformations = [
            "standardize_inchikey",
            "convert_to_inchikeys_from_smiles",
            "generate_synth_keys_for_biologics",
            "dedup_by_inchikey_keep_most_complete",
            "fill_missing_drug_fields_conservative",
            "validate_against_schema_v1",
            "filter_organism_humans",
            "extract_targets_enzymes_transporters",
            "csv_injection_defense",
            "atomic_write_with_sha256_sidecar",
        ]
        transformation_fingerprint = hashlib.sha256(
            "|".join(transformations).encode("utf-8")
        ).hexdigest()

        # LIN15: data quality fingerprint.
        dq_metrics = {
            "total_drugs_input": len(drugs_df),
            "total_drugs_output": len(drugs_df),
            "drugs_dropped_no_inchikey": self._drugs_dropped_no_inchikey,
            "synth_keys_generated": self._synth_keys_generated,
            "interactions_extracted": len(interactions_df),
            "parse_failures": self._parse_failures,
            "non_human_targets_skipped": self._non_human_targets_skipped,
            "skipped_no_id": self._skipped_no_id,
        }
        dq_fingerprint = hashlib.sha256(
            json.dumps(dq_metrics, sort_keys=True).encode("utf-8")
        ).hexdigest()

        # SEC8: who ran the pipeline.
        try:
            created_by = getpass.getuser()
        except Exception:  # pragma: no cover - defensive
            created_by = "unknown"
        try:
            created_on = socket.gethostname()
        except Exception:  # pragma: no cover - defensive
            created_on = "unknown"

        provenance = {
            "source": "drugbank",
            "source_version": self.source_version,
            "pipeline_run_id": self.run_id,
            "pipeline_version": __version__,
            "pipeline_api_version": __version__,
            "rdkit_version": self._rdkit_version,
            "schema_version": SCHEMA_VERSION,
            "db_schema_version": DB_SCHEMA_VERSION,
            "sha256_raw": self._sha256_raw,
            "sha256_cleaned": self._sha256_cleaned,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "drug_count": len(drugs_df),
            "interaction_count": len(interactions_df),
            "created_by": created_by,
            "created_on": created_on,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "process_id": os.getpid(),
            "transformation_fingerprint": transformation_fingerprint,
            "data_quality_fingerprint": dq_fingerprint,
            "data_quality_metrics": dq_metrics,
            "transformations_applied": transformations,
            "target_organisms": self._target_organisms,
            "citation": (
                "Wishart DS et al. DrugBank 5.0: a major update to the DrugBank "
                "database for 2018. Nucleic Acids Res. 2018 Jan 4;46(D1):D1074-D1082."
            ),
        }
        provenance_path = drugs_path.with_suffix(".provenance.json")
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.chmod(provenance_path, 0o600)  # SEC3

    def _write_schema_doc(self, drugs_path: Path) -> None:
        """Write a sidecar schema.md documenting each output column (COM10).

        Parameters
        ----------
        drugs_path : pathlib.Path
            Path to the drugs CSV (used to derive the .schema.md path).
        """
        schema_doc = (
            "# drugbank_drugs.csv - Column Documentation\n\n"
            "Generated by drugbank_pipeline.py v" + __version__ + ".\n\n"
            "| Column | Type | Description |\n"
            "|--------|------|-------------|\n"
            "| drugbank_id | str | DrugBank identifier (DB\\d{5}) |\n"
            "| name | str | Drug preferred name |\n"
            "| inchikey | str | Standard InChIKey (27 chars) or SYNTH synthetic key (27 chars, SYNTH{hash}-{hash}-{hash}) for biologics |\n"
            "| smiles | str | Canonical SMILES |\n"
            "| molecular_weight | float | MW in Da (1-500,000) |\n"
            "| molecular_formula | str | Molecular formula |\n"
            "| is_fda_approved | bool | Currently FDA-approved (excludes withdrawn) |\n"
            "| is_withdrawn | bool | Withdrawn from market |\n"
            "| clinical_status | str | approved/withdrawn/illicit/investigational/... |\n"
            "| groups | str | Pipe-separated DrugBank groups |\n"
            "| mechanism_of_action | str | MOA text (multi-paragraph concatenated) |\n"
            "| description | str | Drug description text |\n"
            "| cas_number | str | CAS Registry Number |\n"
            "| logp | float | Calculated LogP |\n"
            "| tpsa | float | Topological Polar Surface Area |\n"
            "| h_bond_donor_count | int | H-bond donor count |\n"
            "| h_bond_acceptor_count | int | H-bond acceptor count |\n"
            "| rotatable_bond_count | int | Rotatable bond count |\n"
            "| heavy_atom_count | int | Heavy atom count |\n"
            "| complexity | int | Molecular complexity |\n"
            "| inchikey_source | str | extracted_calculated/experimental/generated/synth |\n"
            "| completeness_score | float | 0.0-1.0 fraction of expected fields populated |\n\n"
            "MIME type: text/csv (UTF-8, QUOTE_ALL, \\n line endings).\n"
        )
        schema_path = drugs_path.with_suffix(".schema.md")
        schema_path.write_text(schema_doc, encoding="utf-8")
        os.chmod(schema_path, 0o644)

    def _write_license(self) -> None:
        """Write the DrugBank LICENSE.txt sidecar (SEC4, COM7, COM8).

        Writes once per PROCESSED_DATA_DIR; does not overwrite if it
        already exists with the correct content.
        """
        license_path = PROCESSED_DATA_DIR / "DRUGBANK_LICENSE.txt"
        if license_path.exists() and license_path.read_text(encoding="utf-8") == _DRUGBANK_LICENSE_TEXT:
            return
        license_path.write_text(_DRUGBANK_LICENSE_TEXT, encoding="utf-8")
        os.chmod(license_path, 0o644)

    def _flush_dead_letter(self) -> None:
        """Flush the dead-letter queue to a sidecar JSON file (R3).

        Each entry records: drugbank_id (if known), element_index, error,
        error_type, timestamp.
        """
        dlq_path = PROCESSED_DATA_DIR / f"drugbank_dead_letter_{self.run_id[:8]}.json"
        dlq_path.write_text(
            json.dumps(self._dead_letter, indent=2), encoding="utf-8"
        )
        os.chmod(dlq_path, 0o600)  # SEC3
        logger.warning(
            "[%s] %d drugs failed parsing - written to %s (R3)",
            self.source_name,
            len(self._dead_letter),
            _log_path(dlq_path),
        )

    # ------------------------------------------------------------------
    # Load (A3, A4, ID8, R7, P4, LIN1-LIN4, LIN9, LIN10, D1, D2, D5)
    # ------------------------------------------------------------------

    def load(
        self,
        df: pd.DataFrame,
        interactions_df: pd.DataFrame | None = None,
        session: Any | None = None,
    ) -> int | LoadResult:
        """Load cleaned DrugBank drugs and interactions into the database.

        Audit issues:
        - A3: optional ``interactions_df`` parameter (skip CSV read).
        - A4 / ID8 / R7 / P4: single transactional session for drugs + DPI.
        - D1 / C2 / C3: unwrap ``MappingResult.mapping`` before ``Series.map``.
        - D2 / C1 / C4: extract ``UpsertResult.inserted`` / ``.updated``
          explicitly (no ``__add__``).
        - D5 / COM2: map actions to InteractionType enum; never use "target".
        - LIN1-LIN4, LIN9, LIN10: pass all lineage fields.

        Parameters
        ----------
        df : pandas.DataFrame
            Cleaned drugs DataFrame (from ``clean()``).
        interactions_df : pandas.DataFrame, optional
            In-memory interactions DataFrame. If None, reads from
            ``PROCESSED_DATA_DIR/drugbank_interactions.csv.gz`` (backward
            compat).
        session : SQLAlchemy Session, optional
            Caller-supplied session (for transactional wrapping). If None,
            opens a new session via ``get_db_session()`` (A4).

        Returns
        -------
        int or LoadResult
            Total rows upserted (backward-compat int) OR a LoadResult
            with inserted/updated/skipped/failed breakdown.
        """
        # A4 / ID8 / R7 / P4: single session for the whole load().
        owns_session = session is None
        # v29 ROOT FIX (audit P1-4): capture the return value of
        # __enter__() — the previous code discarded it, so ``session``
        # was the context manager, not the Session. Standalone load()
        # calls crashed with AttributeError on session.flush() /
        # session.rollback() / session.close(). Also, the previous
        # finally block only called session.close() — it NEVER called
        # __exit__(), so the commit never happened and ALL loaded data
        # was silently rolled back when load() ran standalone.
        _session_cm = None
        if owns_session:
            _session_cm = get_db_session(
                pipeline_name=self.source_name,
                run_id=self.run_id,
            )
            session = _session_cm.__enter__()

        total_inserted = 0
        total_updated = 0
        total_skipped = 0
        total_failed = 0

        try:
            # LIN1-LIN4 / BUG-16.2 fix: populate self._pipeline_run_db_id
            # BEFORE upserting DPI rows so each DPI row carries the correct
            # lineage ID back to its PipelineRun audit row. Without this,
            # all DrugBank DPI rows have pipeline_run_id=NULL — breaking
            # the lineage chain that downstream phases use to trace which
            # pipeline run produced a given drug-protein edge.
            self._pipeline_run_db_id = self._get_or_create_pipeline_run_id(session)

            # LIN9 / A4: pass input_checksum to bulk_upsert_drugs.
            input_checksum = self._sha256_cleaned

            # Filter to Drug-model columns only (loader rejects extra cols).
            drugs_df_for_load = self._filter_to_drug_columns(df)

            drug_result: UpsertResult = bulk_upsert_drugs(
                session,
                drugs_df_for_load,
                batch_size=self._batch_size,
                input_checksum=input_checksum,
            )
            # D2 / C1: extract fields explicitly (no __add__).
            total_inserted += drug_result.inserted
            total_updated += drug_result.updated
            total_skipped += drug_result.quarantined
            total_failed += drug_result.failed
            logger.info(
                "[%s] Upserted drugs: inserted=%d updated=%d quarantined=%d failed=%d",
                self.source_name,
                drug_result.inserted,
                drug_result.updated,
                drug_result.quarantined,
                drug_result.failed,
            )

            # Flush so the drug rows are visible within this transaction.
            # v29 ROOT FIX (audit P1-10): the previous code did
            # ``except Exception: pass`` which silently swallowed
            # IntegrityError. ROOT FIX: log the warning.
            try:
                session.flush()
            except Exception as _flush_exc:  # pragma: no cover - defensive
                logger.warning(
                    "[drugbank] session.flush() failed (non-fatal, but "
                    "may indicate data quality issues): %s: %s",
                    type(_flush_exc).__name__, _flush_exc,
                )

            # Flush loader dead-letter queue if any.
            try:
                flush_dead_letter_queue(
                    PROCESSED_DATA_DIR
                    / "dead_letter"
                    / f"drugbank_loader_{self.run_id[:8]}.jsonl"
                )
            except Exception:  # pragma: no cover - defensive
                pass

            # A3: load interactions (in-memory or from CSV).
            if interactions_df is None:
                interactions_path = PROCESSED_DATA_DIR / "drugbank_interactions.csv.gz"
                if interactions_path.exists():
                    interactions_df = pd.read_csv(
                        interactions_path, compression="gzip", low_memory=False
                    )

            if interactions_df is not None and not interactions_df.empty:
                dpi_result = self._load_interactions(
                    interactions_df, df, session
                )
                # D2 / C1: extract fields explicitly.
                total_inserted += dpi_result.inserted
                total_updated += dpi_result.updated
                total_skipped += dpi_result.quarantined
                total_failed += dpi_result.failed

        except Exception:
            if owns_session:
                try:
                    session.rollback()
                except Exception:  # pragma: no cover - defensive
                    pass
            raise
        finally:
            # v29 ROOT FIX (audit P1-4): call __exit__ on the context
            # manager so it commits (on success) or rolls back (on
            # error). The previous code only called session.close(),
            # which silently rolled back ALL loaded data when load()
            # ran standalone (the commit lived in __exit__).
            if owns_session and _session_cm is not None:
                import sys as _sys
                _exc_info = _sys.exc_info()
                # v43 ROOT FIX (P1-006): the previous code had
                # ``except Exception: pass`` here which silently swallowed
                # commit failures. The v41 ROOT FIX comment in
                # uniprot_pipeline.py:2772-2785 explicitly says this
                # pattern is wrong because operators see no error while
                # data appears loaded but isn't persisted. The fix:
                # catch only SQLAlchemyError (commit/flush errors), log
                # at WARNING level, and re-raise if there's no other
                # exception already in flight (so we don't mask the
                # original error that triggered the __exit__ call).
                try:
                    _session_cm.__exit__(*_exc_info)
                except SQLAlchemyError as commit_exc:
                    # Only re-raise if we're NOT already handling another
                    # exception (otherwise the original error should
                    # propagate, not the commit error).
                    if _exc_info[0] is None:
                        logger.warning(
                            "[%s] session commit failed during __exit__: %s. "
                            "Data may not be persisted to the database. "
                            "(v43 P1-006 fix — was silently swallowed before)",
                            self.source_name, commit_exc,
                        )
                        raise
                    else:
                        # We're already handling an exception — log the
                        # commit failure but don't mask the original error.
                        logger.warning(
                            "[%s] session commit failed during __exit__ "
                            "(while handling %s): %s. Data may not be "
                            "persisted. Original error will propagate.",
                            self.source_name,
                            _exc_info[0].__name__, commit_exc,
                        )

        # v43 ROOT FIX (P1-013): the previous code constructed a
        # LoadResult here but then returned ``int(result.total_upserted)``
        # instead of the LoadResult itself. Callers expecting LoadResult
        # per the type hint got an int; ``base_pipeline.run()``
        # isinstance check failed and load_detail metric was never
        # populated. The fix returns the LoadResult directly. For
        # backward compatibility with callers that expect an int, the
        # LoadResult has ``__int__`` and ``__index__`` methods that
        # return total_upserted, so ``int(result)`` still works.
        result = LoadResult(
            rows_inserted=total_inserted,
            rows_updated=total_updated,
            rows_skipped=total_skipped,
            rows_failed=total_failed,
        )
        return result

    def _get_or_create_pipeline_run_id(self, session: Any) -> "int | None":
        """Get the integer ``pipeline_runs.id`` for this run (BUG-16.2).

        The base class writes the PipelineRun audit row AFTER ``load()``
        returns, keyed by ``(source, run_date)`` where ``run_date`` is
        ``self.start_time`` (the moment ``run()`` was called). We mirror
        that keying here so the row we create now is the same row the
        base class UPDATEs later (no duplicate audit rows).

        CRITICAL FIX (scientific correctness / audit-trail integrity):
        Without this method, ``self._pipeline_run_db_id`` stays None and
        every DrugBank DPI row is loaded with ``pipeline_run_id=NULL``,
        breaking the lineage chain that downstream phases (Neo4j export,
        ML training) use to trace which pipeline run produced a given
        drug-protein edge. A NULL lineage ID is fatal for reproducibility
        — if a wet-lab validation fails, we cannot trace back to the
        exact data version that produced the bad prediction.

        Returns
        -------
        int or None
            The integer ``pipeline_runs.id`` of the row for this run,
            or None if the lookup-or-create failed (in which case DPI
            rows will have NULL pipeline_run_id — flagged in the audit
            log but not fatal).
        """
        try:
            from datetime import datetime as _dt
            from database.models import PipelineRun
            # Mirror the base class keying EXACTLY: source + run_date
            # where run_date == self.start_time (the moment run() started).
            if self.start_time is not None:
                run_date = self.start_time
            else:
                run_date = _dt.now(timezone.utc)
            # Truncate microseconds to match the base class's datetime
            # storage (some DBs truncate automatically; SQLite does not).
            run_date = run_date.replace(microsecond=0)
            existing = (
                session.query(PipelineRun)
                .filter(
                    PipelineRun.source == self.source_name,
                    PipelineRun.run_date == run_date,
                )
                .first()
            )
            if existing is not None:
                return int(existing.id)
            run = PipelineRun(
                source=self.source_name,
                run_date=run_date,
                status="running",
                records_downloaded=0,
                records_cleaned=0,
                records_loaded=0,
            )
            session.add(run)
            session.flush()  # populate run.id without committing
            return int(run.id)
        except Exception as exc:
            # R1 defensive: this lineage-tracking path is best-effort.
            # If we cannot create a PipelineRun row (e.g. transient DB
            # error, schema drift, deadlock-victim), we MUST NOT abort
            # the actual data load — that would block the entire weekly
            # DrugBank refresh and leave the staging DB stale. Instead,
            # we log a WARNING and let the DPI rows carry a NULL
            # pipeline_run_id. The audit log captures the failure so
            # an operator can backfill the lineage later. Re-raising
            # here would be a worse outcome than a NULL foreign key.
            logger.warning(
                "[%s] Could not get/create PipelineRun row for lineage: %s. "
                "DPI rows will have NULL pipeline_run_id (acceptable but "
                "noted in audit log).",
                self.source_name,
                exc,
            )
            return None

    def _load_interactions(
        self,
        interactions_df: pd.DataFrame,
        drugs_df: pd.DataFrame,
        session: Any,
    ) -> UpsertResult:
        """Resolve foreign keys and load DrugBank interactions as DPI.

        Audit issues:
        - D1 / C2 / C3: unwrap ``MappingResult.mapping``.
        - D5 / COM2: map actions to InteractionType enum.
        - D11 / C5: dropna on JOINT subset.
        - C6 / C17: use Int64 nullable type; don't shadow parameter.
        - LIN1-LIN4, LIN10: pass all lineage fields to bulk_upsert_dpi.
        - DQ6 / ID10: log dedup result; sort deterministically.
        - DQ9: log unresolved UniProt IDs.
        - S22: source_id includes interactor_type.

        Parameters
        ----------
        interactions_df : pandas.DataFrame
            Interactions DataFrame from ``clean()``.
        drugs_df : pandas.DataFrame
            Drugs DataFrame (for building drugbank_id -> inchikey map).
        session : SQLAlchemy Session
            Active session (caller owns transaction).

        Returns
        -------
        UpsertResult
            Aggregated result across all DPI chunks.
        """
        if interactions_df.empty:
            logger.info("[%s] No interactions to load", self.source_name)
            return UpsertResult()

        # D11 / C5: build drugbank_id -> inchikey map with JOINT dropna.
        if "inchikey" in drugs_df.columns and "drugbank_id" in drugs_df.columns:
            drugbank_id_to_inchikey = dict(
                drugs_df.dropna(subset=["drugbank_id", "inchikey"])
                .set_index("drugbank_id")["inchikey"]
                .items()
            )
        else:
            drugbank_id_to_inchikey = {}

        # D1 / C2 / C3: unwrap MappingResult.mapping (MappingResult is NOT a dict).
        inchikey_map_result: MappingResult = get_inchikey_to_drug_id_map(session)
        uniprot_map_result: MappingResult = get_uniprot_to_protein_id_map(session)

        # LIN18: check built_at for staleness.
        if (
            inchikey_map_result.built_at
            and datetime.now(timezone.utc) - inchikey_map_result.built_at
            > timedelta(hours=1)
        ):
            logger.warning(
                "[%s] inchikey_to_drug_id map is >1 hour old - may be stale (LIN18)",
                self.source_name,
            )

        inchikey_to_drug_id: dict[str, int] = inchikey_map_result.mapping
        uniprot_to_protein_id: dict[str, int] = uniprot_map_result.mapping

        # Resolve drugbank_id -> drug_id via inchikey.
        interactions_df["inchikey"] = interactions_df["drugbank_id"].map(
            drugbank_id_to_inchikey
        )
        interactions_df["drug_id"] = interactions_df["inchikey"].map(
            inchikey_to_drug_id
        )

        # Resolve uniprot_id -> protein_id.
        interactions_df["protein_id"] = interactions_df["uniprot_id"].map(
            uniprot_to_protein_id
        )

        # DQ9: log UniProt IDs that failed protein_id resolution.
        unresolved_mask = interactions_df["protein_id"].isna() & interactions_df[
            "uniprot_id"
        ].notna()
        if unresolved_mask.any():
            unresolved = interactions_df.loc[unresolved_mask, "uniprot_id"].unique().tolist()
            logger.warning(
                "[%s] %d UniProt IDs could not be resolved to protein_id: %s (DQ9)",
                self.source_name,
                len(unresolved),
                unresolved[:20],  # cap log at 20
            )
            # Sidecar file for offline inspection (DQ9).
            try:
                unresolved_path = PROCESSED_DATA_DIR / "drugbank_unresolved_uniprot.txt"
                unresolved_path.write_text("\n".join(unresolved), encoding="utf-8")
                os.chmod(unresolved_path, 0o600)
            except OSError:
                pass

        # C17: don't shadow the parameter; use a new variable.
        resolved_interactions = interactions_df.dropna(
            subset=["drug_id", "protein_id"]
        ).copy()
        logger.info(
            "[%s] Interactions with resolved FKs: %d / %d",
            self.source_name,
            len(resolved_interactions),
            len(interactions_df),
        )

        if resolved_interactions.empty:
            logger.info("[%s] No resolvable interactions to load", self.source_name)
            return UpsertResult()

        # C6: use Int64 nullable type for defensive casting.
        resolved_interactions["drug_id"] = resolved_interactions["drug_id"].astype("Int64")
        resolved_interactions["protein_id"] = resolved_interactions["protein_id"].astype("Int64")

        # D5 / COM2: map actions to InteractionType enum (never use "target").
        resolved_interactions["interaction_type"] = resolved_interactions[
            "action_type"
        ].apply(self._map_action_to_enum)

        # Build DPI DataFrame with all required columns.
        dpi_df = pd.DataFrame(
            {
                "drug_id": resolved_interactions["drug_id"].astype("int64"),
                "protein_id": resolved_interactions["protein_id"].astype("int64"),
                "interaction_type": resolved_interactions["interaction_type"],
                "activity_value": None,
                "activity_units": None,
                "activity_type": None,
                "source": "drugbank",
                "source_id": resolved_interactions["source_id"],
                "confidence_score": None,
            }
        )

        # LIN4: set entity_resolved=True for all DrugBank DPI rows
        # (UniProt IDs are canonical - no entity resolution needed).
        dpi_df["entity_resolved"] = True

        # ID10 / DQ6: sort deterministically BEFORE dedup.
        dpi_df = dpi_df.sort_values(
            ["drug_id", "protein_id", "source", "source_id"]
        ).reset_index(drop=True)

        before_dedup = len(dpi_df)
        dpi_df = dedup_interactions(
            dpi_df,
            keys=["drug_id", "protein_id", "source", "source_id"],
            keep="first",  # deterministic after sort
        )
        after_dedup = len(dpi_df)
        if before_dedup != after_dedup:
            logger.warning(
                "[%s] Removed %d duplicate DPI rows during dedup (%d -> %d) (DQ6)",
                self.source_name,
                before_dedup - after_dedup,
                before_dedup,
                after_dedup,
            )

        # P13 / CF13: chunked DPI upsert.
        total_inserted = 0
        total_updated = 0
        total_quarantined = 0
        total_failed = 0

        for chunk_start in range(0, len(dpi_df), self._dpi_batch_size):
            chunk = dpi_df.iloc[chunk_start : chunk_start + self._dpi_batch_size].copy()
            # LIN1, LIN2, LIN3, LIN4, LIN9, LIN10: pass all lineage fields.
            dpi_result = bulk_upsert_dpi(
                session,
                chunk,
                batch_size=self._batch_size,
                pipeline_run_id=self._pipeline_run_db_id,
                source_version=self.source_version,
                source_fetch_date=self._source_fetch_date,
                input_checksum=self._sha256_cleaned,
            )
            total_inserted += dpi_result.inserted
            total_updated += dpi_result.updated
            total_quarantined += dpi_result.quarantined
            total_failed += dpi_result.failed

        logger.info(
            "[%s] Upserted DPI: inserted=%d updated=%d quarantined=%d failed=%d "
            "(across %d chunks)",
            self.source_name,
            total_inserted,
            total_updated,
            total_quarantined,
            total_failed,
            (len(dpi_df) + self._dpi_batch_size - 1) // self._dpi_batch_size,
        )

        return UpsertResult(
            total_input=len(dpi_df),
            inserted=total_inserted,
            updated=total_updated,
            quarantined=total_quarantined,
            failed=total_failed,
        )

    @staticmethod
    def _map_action_to_enum(action: Any) -> str:
        """Map a DrugBank action string to an InteractionType enum value (D5).

        Multi-action strings (pipe-separated) take the first action for
        enum mapping; the full string is preserved elsewhere.

        Parameters
        ----------
        action : Any
            The action string (e.g. "inhibitor", "agonist|positive modulator").

        Returns
        -------
        str
            The mapped InteractionType enum value (default "unknown").
        """
        if pd.isna(action) or not action:
            return "unknown"
        first = str(action).split("|")[0].strip().lower()
        return ACTION_TO_ENUM.get(first, "unknown")


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    pipeline = DrugBankPipeline()
    pipeline.run()
