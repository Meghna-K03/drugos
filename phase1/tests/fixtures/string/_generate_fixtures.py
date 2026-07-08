"""Generate STRING test fixtures (sample .gz files) for the test suite.

Generates four fixture files under tests/fixtures/string/:
  1. 9606.protein.links.v12.0.txt.gz
  2. 9606.protein.aliases.v12.0.txt.gz
  3. 9606.protein.links.detailed.v12.0.txt.gz
  4. 9606.protein.links.v12.0.corrupt.txt.gz

The fixtures are intentionally small (10–20 rows each) but exercise every
scientific-correctness edge case documented in the STRING pipeline fix
prompt:
  - Mix of human (9606.*) + 1 mouse (10090.*) for organism validation
  - Mix of UniProt_AC + BLAST_UniProt_AC for source filtering
  - Mix of canonical + isoform UniProt accessions
  - Mix of valid + invalid UniProt accessions
  - Lowercase UniProt entries (for uppercase-normalization test)
  - NaN combined_score row (for quarantine-not-fillna-0 test)
  - Self-interaction / homodimer row (for homodimer-deadletter test)
  - Multiple STRING ENSP pairs mapping to the same UniProt pair
    (for dedup-strategy test)

Run::

    python3 tests/fixtures/string/_generate_fixtures.py
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent


def write_links_fixture() -> Path:
    """Generate 9606.protein.links.v12.0.txt.gz."""
    path = FIXTURES_DIR / "9606.protein.links.v12.0.txt.gz"
    # Sci: STRING links file is space-separated, gzipped.
    # Columns: protein1, protein2, combined_score.
    rows = [
        # header
        "protein1 protein2 combined_score",
        # 1. Valid human pair, high score (kept after filter ≥ 400).
        "9606.ENSP00000000233 9606.ENSP00000000412 900",
        # 2. Valid human pair, low score (dropped by ≥ 400 filter).
        "9606.ENSP00000000456 9606.ENSP00000000567 100",
        # 3. Mouse contamination (wrong taxon — should be quarantined).
        "10090.ENSMUSP00000000001 9606.ENSP00000000233 800",
        # 4. Pair that maps (via aliases) to two UniProt accessions — for dedup test.
        "9606.ENSP00000000233 9606.ENSP00000000999 950",
        # 5. Same biological pair as #4 but different STRING IDs (isoforms of P53).
        #    Should collapse to one UniProt pair (P23219-P04637) with max score.
        "9606.ENSP00000001000 9606.ENSP00000000999 700",
        # 6. Self-interaction / homodimer — should be deadlettered (DB constraint).
        "9606.ENSP00000000233 9606.ENSP00000000233 999",
        # 7. NaN combined_score (encoded as empty string) — should be quarantined, NOT 0.
        "9606.ENSP00000001234 9606.ENSP00000001345 ",
        # 8. Very high score pair.
        "9606.ENSP00000001400 9606.ENSP00000001500 1000",
        # 9. Score exactly at threshold (400) — should be kept (>=).
        "9606.ENSP00000001600 9606.ENSP00000001700 400",
        # 10. Another mouse contamination (both proteins mouse).
        "10090.ENSMUSP00000000001 10090.ENSMUSP00000000002 950",
    ]
    payload = "\n".join(rows) + "\n"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def write_aliases_fixture() -> Path:
    """Generate 9606.protein.aliases.v12.0.txt.gz."""
    path = FIXTURES_DIR / "9606.protein.aliases.v12.0.txt.gz"
    # Sci: STRING aliases file is tab-separated, gzipped.
    # Columns: #string_protein_id, alias, source.
    rows = [
        # header (note leading '#')
        "#string_protein_id\talias\tsource",
        # Curated UniProt_AC entries (use these — <1% error).
        "9606.ENSP00000000233\tP69905\tUniProt_AC",          # HBA1
        "9606.ENSP00000000412\tP68871\tUniProt_AC",          # HBB
        "9606.ENSP00000000456\tP04637\tUniProt_AC",          # TP53 (canonical)
        "9606.ENSP00000000567\tQ9H0A2\tUniProt_AC",          # RPRD1A
        "9606.ENSP00000000999\tP23219\tUniProt_AC",          # COX1
        "9606.ENSP00000001000\tP23219\tUniProt_AC",          # isoform of P23219 — collapses with row above
        "9606.ENSP00000001234\tP05067\tUniProt_AC",          # APP
        "9606.ENSP00000001345\tP01023\tUniProt_AC",          # A2M
        "9606.ENSP00000001400\tP00533\tUniProt_AC",          # EGFR
        "9606.ENSP00000001500\tP04626\tUniProt_AC",          # ERBB2 (HER2)
        "9606.ENSP00000001600\tP01133\tUniProt_AC",          # EGF
        "9606.ENSP00000001700\tP01375\tUniProt_AC",          # TNF
        # BLAST_UniProt_AC entries (DO NOT USE — ~5–10% error).
        "9606.ENSP00000000233\tQ12345\tBLAST_UniProt_AC",    # should be excluded
        "9606.ENSP00000000412\tQ67890\tBLAST_UniProt_AC",
        # Lowercase UniProt entry — should be uppercased by the pipeline.
        "9606.ENSP00000001800\tp01116\tUniProt_AC",          # KRAS lowercase
        # Isoform accession — should be separated from canonical.
        "9606.ENSP00000001900\tP04637-2\tUniProt_AC",        # TP53 isoform 2
        # Invalid UniProt accession — should be excluded by canonical pattern.
        "9606.ENSP00000002000\tABCDEF\tUniProt_AC",          # not canonical
        "9606.ENSP00000002100\tP1234X\tUniProt_AC",          # not canonical
        # Non-UniProt source — should be ignored entirely.
        "9606.ENSP00000000233\tHBA1\tGene_Name",
        # 10-char UniProt accession (newer format) — should be accepted.
        "9606.ENSP00000002200\tA0A024RBG1\tUniProt_AC",      # valid 10-char
    ]
    payload = "\n".join(rows) + "\n"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def write_detailed_fixture() -> Path:
    """Generate 9606.protein.links.detailed.v12.0.txt.gz."""
    path = FIXTURES_DIR / "9606.protein.links.detailed.v12.0.txt.gz"
    # Sci: STRING detailed file has 7 sub-scores: neighborhood, fusion,
    # cooccurrence, coexpression, experimental, database, textmining.
    rows = [
        # header
        "protein1 protein2 neighborhood fusion cooccurrence coexpression experimental database textmining combined_score",
        # Pair matching the links fixture (canonical-ordered).
        "9606.ENSP00000000233 9606.ENSP00000000412 0 0 800 0 900 0 850 900",
        # Pair from links fixture, also canonical-ordered.
        "9606.ENSP00000000233 9606.ENSP00000000999 100 0 0 600 950 0 0 950",
        # A pair with reversed protein1/protein2 (test canonical ordering in merge).
        "9606.ENSP00000001500 9606.ENSP00000001400 50 0 0 700 1000 0 0 1000",
        # Pair at threshold.
        "9606.ENSP00000001600 9606.ENSP00000001700 0 0 0 0 400 0 0 400",
    ]
    payload = "\n".join(rows) + "\n"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def write_corrupt_fixture() -> Path:
    """Generate 9606.protein.links.v12.0.corrupt.txt.gz — bad magic bytes."""
    path = FIXTURES_DIR / "9606.protein.links.v12.0.corrupt.txt.gz"
    # Write a non-gzip file with .gz extension so magic-byte check fails.
    path.write_bytes(b"NOT A GZIP FILE - this is plain text.\n")
    return path


def main() -> None:
    paths = [
        write_links_fixture(),
        write_aliases_fixture(),
        write_detailed_fixture(),
        write_corrupt_fixture(),
    ]
    for p in paths:
        size = p.stat().st_size
        print(f"  {p.relative_to(FIXTURES_DIR)}  ({size} bytes)")


if __name__ == "__main__":
    main()
