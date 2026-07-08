"""
Automatic data downloader for the Autonomous Drug Repurposing Platform.

This module enables FULLY AUTOMATIC download of all biomedical data sources
without requiring:
  - DATABASE_URL (no PostgreSQL needed)
  - DRUGBANK_XML_PATH (no DrugBank license needed)
  - OMIM_API_KEY (uses OpenTargets as alternative)
  - DISGENET_API_KEY (uses OpenTargets as alternative)

Usage:
    python -m pipelines.download_all                    # download everything
    python -m pipelines.download_all --source chembl    # download one source
    python -m pipelines.download_all --list             # list sources
    python -m pipelines.download_all --process          # download + process to CSVs

What it downloads (ALL FREE, no login, no API key):
  1. ChEMBL — 2,996 FDA-approved drugs + 5,000 activities + 333 targets
     URL: https://www.ebi.ac.uk/chembl/api/data/
  2. UniProt — 20,432 reviewed human proteins
     URL: https://rest.uniprot.org/uniprotkb/
  3. STRING — 50,000 high-confidence human PPIs (filtered from 13.7M)
     URL: https://stringdb-downloads.org/
  4. PubChem — 96 compound enrichments via PUG-REST
     URL: https://pubchem.ncbi.nlm.nih.gov/rest/pug/
  5. OpenTargets — 540 gene-disease associations (replaces OMIM/DisGeNET)
     URL: https://api.platform.opentargets.org/

DrugBank solution: DrugBank academic downloads are PAUSED since May 2026.
This module uses ChEMBL as the primary drug source (DRUGOS_USE_CHEMBL_AS_PRIMARY=1,
the default). No DrugBank license required.

When DrugBank resumes: set DRUGBANK_XML_PATH + DRUGOS_USE_CHEMBL_AS_PRIMARY=0
and the master DAG will automatically route to run_drugbank.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Output directory — same as phase1/processed_data
HERE = Path(__file__).resolve().parent
PHASE1_ROOT = HERE.parent
PROCESSED_DATA_DIR = PHASE1_ROOT / "processed_data"
RAW_DATA_DIR = PHASE1_ROOT / "data" / "raw_downloads"

PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Utility functions
# ============================================================================

def _fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch JSON from a URL with error handling."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _fetch_text(url: str, timeout: int = 60) -> str:
    """Fetch text content from a URL."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _download_file(url: str, dest: Path, timeout: int = 300) -> bool:
    """Download a file to dest. Returns True on success."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"    ERROR downloading {url}: {e}")
        return False


def _write_csv(path: Path, rows: list, fieldnames: list) -> int:
    """Write rows to a CSV file. Returns number of rows written."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def _write_csv_gzip(path: Path, rows: list, fieldnames: list) -> int:
    """Write rows to a gzipped CSV file. Returns number of rows written."""
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


# ============================================================================
# 1. ChEMBL Downloader
# ============================================================================

def download_chembl(max_drugs: int = 2996, max_activities: int = 5000) -> dict:
    """Download ChEMBL FDA-approved drugs + activities + targets.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("DOWNLOADING ChEMBL (FDA-approved drugs + bioactivities)")
    print("=" * 70)
    print(f"  Target: {max_drugs} drugs, {max_activities} activities")
    print(f"  URL: https://www.ebi.ac.uk/chembl/api/data/")
    print(f"  License: CC BY-SA 3.0 (free, no login, no API key)")

    chembl_dir = RAW_DATA_DIR / "chembl"
    chembl_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Download approved drugs list
    print("\n  [1/4] Downloading approved drugs list...")
    drugs = []
    offset = 0
    page_size = 1000
    while offset < max_drugs:
        url = (
            f"https://www.ebi.ac.uk/chembl/api/data/drug.json"
            f"?max_phase=4&limit={page_size}&offset={offset}"
        )
        try:
            data = _fetch_json(url)
            page_drugs = data.get("drugs", [])
            if not page_drugs:
                break
            drugs.extend(page_drugs)
            print(f"    Page {offset // page_size + 1}: +{len(page_drugs)} drugs (total: {len(drugs)})")
            offset += page_size
            time.sleep(0.3)
        except Exception as e:
            print(f"    ERROR on page {offset}: {e}")
            break

    # Save drug metadata
    drugs_file = chembl_dir / "approved_drugs.jsonl"
    with open(drugs_file, "w") as f:
        for drug in drugs:
            f.write(json.dumps(drug) + "\n")
    print(f"  Saved {len(drugs)} drug metadata records to {drugs_file.name}")

    # Step 2: Download molecule structures (InChIKey, SMILES, MW) via batch API
    print("\n  [2/4] Downloading molecule structures (InChIKey, SMILES, MW)...")
    chembl_ids = [d.get("molecule_chembl_id") for d in drugs if d.get("molecule_chembl_id")]
    structures = []
    batch_size = 20
    total_batches = (len(chembl_ids) + batch_size - 1) // batch_size

    # Check for existing progress (resume support)
    structures_file = chembl_dir / "molecule_structures.jsonl"
    existing_ids = set()
    if structures_file.exists():
        with open(structures_file) as f:
            for line in f:
                if line.strip():
                    try:
                        rec = json.loads(line)
                        if rec.get("chembl_id"):
                            existing_ids.add(rec["chembl_id"])
                    except json.JSONDecodeError:
                        pass
        print(f"    Resuming: {len(existing_ids)} structures already downloaded")

    remaining_ids = [cid for cid in chembl_ids if cid not in existing_ids]
    print(f"    Fetching {len(remaining_ids)} molecule structures in {len(remaining_ids)//batch_size + 1} batches...")

    with open(structures_file, "a") as out_f:
        for i in range(0, len(remaining_ids), batch_size):
            batch = remaining_ids[i:i + batch_size]
            batch_num = i // batch_size + 1
            ids_param = ";".join(batch)
            url = f"https://www.ebi.ac.uk/chembl/api/data/molecule/set/{ids_param}.json"
            try:
                data = _fetch_json(url, timeout=60)
                for m in data.get("molecules", []):
                    props = m.get("molecule_properties", {}) or {}
                    structs = m.get("molecule_structures", {}) or {}
                    record = {
                        "chembl_id": m.get("molecule_chembl_id"),
                        "name": m.get("pref_name"),
                        "inchikey": structs.get("standard_inchi_key"),
                        "smiles": structs.get("canonical_smiles"),
                        "molecular_weight": props.get("full_mwt"),
                        "molecular_formula": props.get("full_formula"),
                        "max_phase": m.get("max_phase"),
                        "first_approval": m.get("first_approval"),
                        "withdrawn_flag": m.get("withdrawn_flag"),
                        "black_box_warning": m.get("black_box_warning"),
                        "atc_codes": [a.get("code") for a in (m.get("atc_classification") or [])],
                    }
                    out_f.write(json.dumps(record) + "\n")
                    structures.append(record)
            except Exception as e:
                print(f"    Batch {batch_num} error: {e}")
            if batch_num % 10 == 0:
                print(f"    Batch {batch_num}/{total_batches}: {len(structures)} new structures")
                time.sleep(0.3)

    # Reload all structures from file
    structures = []
    with open(structures_file) as f:
        for line in f:
            if line.strip():
                try:
                    structures.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    print(f"  Total molecule structures: {len(structures)}")
    print(f"    With InChIKey: {sum(1 for r in structures if r.get('inchikey'))}")
    print(f"    With first_approval: {sum(1 for r in structures if r.get('first_approval'))}")

    # Step 3: Download activities (drug-target interactions)
    print(f"\n  [3/4] Downloading {max_activities} bioactivities...")
    activities = []
    offset = 0
    page_size = 1000
    pages_needed = (max_activities + page_size - 1) // page_size
    for page in range(pages_needed):
        url = (
            f"https://www.ebi.ac.uk/chembl/api/data/activity.json"
            f"?molecule__max_phase=4&standard_type=IC50"
            f"&standard_value__isnull=false&limit={page_size}&offset={offset}"
        )
        try:
            data = _fetch_json(url)
            page_acts = data.get("activities", [])
            if not page_acts:
                break
            for a in page_acts:
                activities.append({
                    "activity_id": a.get("activity_id"),
                    "molecule_chembl_id": a.get("molecule_chembl_id"),
                    "target_chembl_id": a.get("target_chembl_id"),
                    "target_pref_name": a.get("target_pref_name"),
                    "standard_type": a.get("standard_type"),
                    "standard_value": a.get("standard_value"),
                    "standard_units": a.get("standard_units"),
                    "pchembl_value": a.get("pchembl_value"),
                    "action_type": a.get("action_type"),
                    "assay_type": a.get("assay_type"),
                })
            print(f"    Page {page + 1}/{pages_needed}: +{len(page_acts)} activities (total: {len(activities)})")
            offset += page_size
            time.sleep(0.3)
        except Exception as e:
            print(f"    ERROR on page {page + 1}: {e}")
            break

    activities_file = chembl_dir / "activities.jsonl"
    with open(activities_file, "w") as f:
        for a in activities:
            f.write(json.dumps(a) + "\n")
    print(f"  Saved {len(activities)} activities to {activities_file.name}")

    # Step 4: Download target → UniProt mappings
    print(f"\n  [4/4] Downloading target → UniProt mappings...")
    target_ids = list({a.get("target_chembl_id") for a in activities if a.get("target_chembl_id")})
    print(f"    {len(target_ids)} unique targets to fetch")
    targets = []
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        batch_num = i // batch_size + 1
        ids_param = ";".join(batch)
        url = f"https://www.ebi.ac.uk/chembl/api/data/target/set/{ids_param}.json"
        try:
            data = _fetch_json(url, timeout=30)
            for t in data.get("targets", []):
                comps = t.get("target_components", []) or []
                uniprot_accessions = []
                gene_symbol = ""
                for c in comps:
                    acc = c.get("accession")
                    if acc:
                        if re.match(r'^[OPQ][0-9][A-Z0-9]{3}[0-9]', acc) or \
                           re.match(r'^[A-NR-Z][0-9][A-Z0-9]{3}[0-9]', acc):
                            if acc not in uniprot_accessions:
                                uniprot_accessions.append(acc)
                    for syn in (c.get("target_component_synonyms") or []):
                        if syn.get("syn_type") == "GENE_SYMBOL":
                            gene_symbol = syn.get("component_synonym", "")
                            break
                targets.append({
                    "target_chembl_id": t.get("target_chembl_id"),
                    "target_type": t.get("target_type"),
                    "pref_name": t.get("pref_name"),
                    "organism": t.get("organism"),
                    "uniprot_accessions": uniprot_accessions,
                    "gene_symbol": gene_symbol,
                })
        except Exception as e:
            print(f"    Batch {batch_num} error: {e}")
        if batch_num % 5 == 0:
            print(f"    Batch {batch_num}: {len(targets)} targets")
            time.sleep(0.3)

    targets_file = chembl_dir / "targets_uniprot.jsonl"
    with open(targets_file, "w") as f:
        for t in targets:
            f.write(json.dumps(t) + "\n")
    print(f"  Saved {len(targets)} targets ({sum(1 for t in targets if t.get('uniprot_accessions'))} with UniProt)")

    return {
        "drugs": len(drugs),
        "structures": len(structures),
        "activities": len(activities),
        "targets": len(targets),
    }


# ============================================================================
# 2. UniProt Downloader
# ============================================================================

def download_uniprot() -> dict:
    """Download UniProt reviewed human proteins via REST API.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("DOWNLOADING UniProt (reviewed human proteins)")
    print("=" * 70)
    print(f"  URL: https://rest.uniprot.org/uniprotkb/stream")
    print(f"  License: CC BY 4.0 (free, no login, no API key)")

    uniprot_dir = RAW_DATA_DIR / "uniprot"
    uniprot_dir.mkdir(parents=True, exist_ok=True)

    # Use the stream endpoint to get ALL reviewed human proteins at once
    url = (
        "https://rest.uniprot.org/uniprotkb/stream"
        "?query=reviewed:true+AND+organism_id:9606"
        "&format=tsv"
        "&fields=accession,id,gene_names,protein_name,organism_name,length,sequence"
    )
    print(f"  Fetching all reviewed human proteins...")
    try:
        text = _fetch_text(url, timeout=120)
        output_file = uniprot_dir / "human_proteins.tsv"
        with open(output_file, "w") as f:
            f.write(text)
        line_count = text.count("\n")
        print(f"  Saved {line_count} proteins to {output_file.name}")
        return {"proteins": line_count}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"proteins": 0, "error": str(e)}


# ============================================================================
# 3. STRING Downloader
# ============================================================================

def download_string(max_ppis: int = 50000, min_score: int = 700) -> dict:
    """Download STRING human protein-protein interactions.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("DOWNLOADING STRING (human protein-protein interactions)")
    print("=" * 70)
    print(f"  URL: https://stringdb-downloads.org/")
    print(f"  License: CC BY 4.0 (free, no login, no API key)")
    print(f"  Filter: combined_score >= {min_score}, max {max_ppis} interactions")

    string_dir = RAW_DATA_DIR / "string"
    string_dir.mkdir(parents=True, exist_ok=True)

    # Download the full human PPI file
    url = "https://stringdb-downloads.org/download/protein.links.full.v12.0/9606.protein.links.full.v12.0.txt.gz"
    dest = string_dir / "9606.protein.links.full.v12.0.txt.gz"

    if dest.exists() and dest.stat().st_size > 1000000:
        print(f"  File already exists ({dest.stat().st_size // 1024 // 1024} MB), skipping download")
    else:
        print(f"  Downloading {url}...")
        if not _download_file(url, dest, timeout=300):
            return {"ppis": 0, "error": "download failed"}

    # Filter to high-confidence PPIs
    print(f"  Filtering to combined_score >= {min_score} (max {max_ppis})...")
    ppis = []
    with gzip.open(dest, "rt") as f:
        reader = csv.DictReader(f, delimiter=" ")
        for row in reader:
            score = int(row.get("combined_score", 0))
            if score < min_score:
                continue
            p1 = row.get("protein1", "").replace("9606.", "")
            p2 = row.get("protein2", "").replace("9606.", "")
            if not p1 or not p2:
                continue
            ppis.append({
                "uniprot_ac_a": p1,
                "uniprot_ac_b": p2,
                "score": score,
                "combined_score": score,
            })
            if len(ppis) >= max_ppis:
                break

    print(f"  Filtered to {len(ppis)} high-confidence PPIs")
    return {"ppis": len(ppis)}


# ============================================================================
# 4. PubChem Downloader
# ============================================================================

def download_pubchem(max_compounds: int = 100) -> dict:
    """Download PubChem compound enrichments via PUG-REST.

    Uses InChIKeys from ChEMBL structures to look up PubChem CIDs and properties.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("DOWNLOADING PubChem (compound enrichments via PUG-REST)")
    print("=" * 70)
    print(f"  URL: https://pubchem.ncbi.nlm.nih.gov/rest/pug/")
    print(f"  License: Public domain (free, no login, no API key)")
    print(f"  Target: {max_compounds} compound enrichments")

    pubchem_dir = RAW_DATA_DIR / "pubchem"
    pubchem_dir.mkdir(parents=True, exist_ok=True)

    # Load InChIKeys from ChEMBL structures
    chembl_structures_file = RAW_DATA_DIR / "chembl" / "molecule_structures.jsonl"
    if not chembl_structures_file.exists():
        print(f"  ERROR: ChEMBL structures not found. Run download_chembl() first.")
        return {"enrichments": 0, "error": "no chembl structures"}

    inchikeys = []
    with open(chembl_structures_file) as f:
        for line in f:
            if line.strip():
                try:
                    rec = json.loads(line)
                    ik = rec.get("inchikey")
                    if ik:
                        inchikeys.append((rec.get("chembl_id"), ik))
                except json.JSONDecodeError:
                    pass

    print(f"  Found {len(inchikeys)} InChIKeys from ChEMBL")
    sample = inchikeys[:max_compounds]
    print(f"  Fetching PubChem data for {len(sample)} compounds...")

    enrichments = []
    for i, (chembl_id, ik) in enumerate(sample):
        try:
            # Step 1: get CID from InChIKey
            url1 = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/{urllib.parse.quote(ik)}/cids/JSON"
            data1 = _fetch_json(url1, timeout=15)
            cids = data1.get("IdentifierList", {}).get("CID", [])
            if not cids:
                continue
            cid = cids[0]
            # Step 2: get properties
            url2 = (
                f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}"
                f"/property/CanonicalSMILES,IsomericSMILES,MolecularWeight,"
                f"XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,"
                f"RotatableBondCount/JSON"
            )
            data2 = _fetch_json(url2, timeout=15)
            props = data2.get("PropertyTable", {}).get("Properties", [{}])[0]
            enrichments.append({
                "inchikey": ik,
                "pubchem_cid": cid,
                "chembl_id": chembl_id,
                "canonical_smiles": props.get("CanonicalSMILES"),
                "isomeric_smiles": props.get("IsomericSMILES"),
                "molecular_weight": props.get("MolecularWeight"),
                "xlogp": props.get("XLogP"),
                "tpsa": props.get("TPSA"),
                "h_bond_donor_count": props.get("HBondDonorCount"),
                "h_bond_acceptor_count": props.get("HBondAcceptorCount"),
                "rotatable_bond_count": props.get("RotatableBondCount"),
            })
        except Exception:
            pass  # skip failures
        if (i + 1) % 20 == 0:
            print(f"    {i + 1}/{len(sample)}: {len(enrichments)} enriched")
            time.sleep(0.5)  # PubChem rate limit: 5 req/sec

    # Save enrichments
    enrichments_file = pubchem_dir / "enrichment.jsonl"
    with open(enrichments_file, "w") as f:
        for r in enrichments:
            f.write(json.dumps(r) + "\n")
    print(f"  Saved {len(enrichments)} PubChem enrichments to {enrichments_file.name}")
    return {"enrichments": len(enrichments)}


# ============================================================================
# 5. OpenTargets Downloader (free OMIM/DisGeNET alternative)
# ============================================================================

def download_opentargets(genes: list = None) -> dict:
    """Download gene-disease associations from OpenTargets GraphQL API.

    This is a FREE alternative to OMIM/DisGeNET that requires NO API key.
    When OMIM_API_KEY or DISGENET_API_KEY are set, those sources will be
    used IN ADDITION to OpenTargets.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("DOWNLOADING OpenTargets (gene-disease associations)")
    print("=" * 70)
    print(f"  URL: https://api.platform.opentargets.org/api/v4/graphql")
    print(f"  License: CC BY 4.0 (free, no login, no API key)")
    print(f"  Purpose: Free alternative to OMIM/DisGeNET (no API key needed)")

    ot_dir = RAW_DATA_DIR / "opentargets"
    ot_dir.mkdir(parents=True, exist_ok=True)

    # Default gene list: well-known drug targets that overlap with ChEMBL
    if genes is None:
        genes = [
            'ACE', 'ADRB1', 'ADRB2', 'AGTR1', 'AR', 'ESR1', 'PGR', 'GRIN2A',
            'DRD2', 'HMGCR', 'CYP3A4', 'CYP2D6', 'PTGS2', 'PTGS1', 'ACHE',
            'MAOA', 'SLC6A4', 'GABRA1', 'SCN5A', 'KCNH2', 'CFTR', 'EGFR',
            'ERBB2', 'BRAF', 'KRAS', 'TP53', 'BRCA1', 'TNF', 'IL6', 'INS',
            'LEP', 'MC4R', 'GLP1R', 'DPP4', 'SSTR2', 'ESR2', 'PDE5A',
            'NOS3', 'APOB', 'PCSK9', 'LDLR', 'FGFR1', 'PDGFRA', 'KIT',
            'ABL1', 'SRC', 'JAK2', 'MTOR', 'PIK3CA', 'AKT1',
        ]

    print(f"  Querying {len(genes)} drug-targetable genes for disease associations...")

    url = "https://api.platform.opentargets.org/api/v4/graphql"
    all_associations = []

    for gene in genes:
        # Step 1: search for the gene to get its Ensembl ID
        search_query = {
            "query": """
            query {
              search(queryString: "%s", entityNames: ["target"], page: {index: 0, size: 1}) {
                hits { id name entity }
              }
            }
            """ % gene
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(search_query).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            hits = data.get("data", {}).get("search", {}).get("hits", [])
            if not hits:
                continue
            ensg_id = hits[0].get("id")

            # Step 2: get disease associations
            assoc_query = {
                "query": """
                query {
                  target(ensemblId: "%s") {
                    approvedSymbol
                    associatedDiseases(page: {index: 0, size: 20}) {
                      rows {
                        score
                        disease { id name }
                      }
                    }
                  }
                }
                """ % ensg_id
            }
            req2 = urllib.request.Request(
                url,
                data=json.dumps(assoc_query).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                data2 = json.load(resp2)
            target = data2.get("data", {}).get("target", {})
            if target:
                gene_sym = target.get("approvedSymbol", gene)
                for row in target.get("associatedDiseases", {}).get("rows", []):
                    disease = row.get("disease", {})
                    all_associations.append({
                        "gene_symbol": gene_sym,
                        "disease_id": disease.get("id", ""),
                        "disease_name": disease.get("name", ""),
                        "score": row.get("score", 0.5),
                        "source": "OpenTargets",
                    })
        except Exception:
            pass  # skip failures

    # Save associations
    ot_file = ot_dir / "gene_disease_associations.jsonl"
    with open(ot_file, "w") as f:
        for a in all_associations:
            f.write(json.dumps(a) + "\n")
    print(f"  Saved {len(all_associations)} gene-disease associations to {ot_file.name}")
    print(f"    Genes covered: {len(set(a['gene_symbol'] for a in all_associations))}")
    print(f"    Diseases covered: {len(set(a['disease_id'] for a in all_associations))}")
    return {"associations": len(all_associations)}


# ============================================================================
# Processing: Convert raw downloads → Phase 1 CSV format
# ============================================================================

def _fix_disease_id(did: str) -> str:
    """Convert underscore ontology IDs to colon format (matches ID_PATTERNS)."""
    if did.startswith("MONDO_"):
        return "MONDO:" + did[6:]
    elif did.startswith("EFO_"):
        return "EFO:" + did[5:]
    elif did.startswith("Orphanet_"):
        return "Orphanet:" + did[9:]
    elif did.startswith("HP_"):
        return "HP:" + did[3:]
    return did


def process_to_csvs() -> dict:
    """Convert all raw downloads to Phase 1 CSV format in processed_data/.

    Returns dict with counts.
    """
    print("\n" + "=" * 70)
    print("PROCESSING RAW DOWNLOADS → PHASE 1 CSVs")
    print("=" * 70)

    counts = {}

    # Load raw data
    chembl_dir = RAW_DATA_DIR / "chembl"
    uniprot_dir = RAW_DATA_DIR / "uniprot"
    string_dir = RAW_DATA_DIR / "string"
    pubchem_dir = RAW_DATA_DIR / "pubchem"
    ot_dir = RAW_DATA_DIR / "opentargets"

    # --- drugbank_drugs.csv (from ChEMBL) ---
    print("\n  Processing drugbank_drugs.csv...")
    structures = []
    struct_file = chembl_dir / "molecule_structures.jsonl"
    if struct_file.exists():
        with open(struct_file) as f:
            for line in f:
                if line.strip():
                    try:
                        structures.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    struct_by_id = {r["chembl_id"]: r for r in structures}

    drugs = []
    drugs_file = chembl_dir / "approved_drugs.jsonl"
    if drugs_file.exists():
        with open(drugs_file) as f:
            for line in f:
                if line.strip():
                    try:
                        drugs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    drug_rows = []
    for drug in drugs:
        cid = drug.get("molecule_chembl_id")
        if not cid:
            continue
        struct = struct_by_id.get(cid, {})
        withdrawn_raw = drug.get("withdrawn_flag") or struct.get("withdrawn_flag")
        is_withdrawn = withdrawn_raw in (True, "1", 1, "True", "true")
        max_phase = drug.get("max_phase") or struct.get("max_phase")
        is_fda = False
        try:
            if float(max_phase) >= 4:
                is_fda = True
        except (TypeError, ValueError):
            pass
        first_approval = drug.get("first_approval") or struct.get("first_approval")
        atc_codes = drug.get("atc_classification") or struct.get("atc_codes") or []
        drug_rows.append({
            "drugbank_id": cid,
            "name": drug.get("pref_name") or struct.get("name") or "",
            "inchikey": struct.get("inchikey") or "",
            "smiles": struct.get("smiles") or "",
            "molecular_weight": struct.get("molecular_weight") or "",
            "molecular_formula": struct.get("molecular_formula") or "",
            "is_fda_approved": is_fda,
            "is_withdrawn": is_withdrawn,
            "clinical_status": "approved" if is_fda else ("withdrawn" if is_withdrawn else "investigational"),
            "groups": ";".join(filter(None, ["approved" if is_fda else None, "withdrawn" if is_withdrawn else None])),
            "mechanism_of_action": "",
            "cas_number": "",
            "chembl_id": cid,
            "pubchem_cid": "",
            "completeness_score": 0.5,
            "first_approval": first_approval or "",
            "max_phase": max_phase or "",
            "atc_code": atc_codes[0] if atc_codes else "",
            "black_box_warning": drug.get("black_box_warning", ""),
        })
    n = _write_csv(PROCESSED_DATA_DIR / "drugbank_drugs.csv", drug_rows, [
        "drugbank_id", "name", "inchikey", "smiles", "molecular_weight",
        "molecular_formula", "is_fda_approved", "is_withdrawn",
        "clinical_status", "groups", "mechanism_of_action", "cas_number",
        "chembl_id", "pubchem_cid", "completeness_score",
        "first_approval", "max_phase", "atc_code", "black_box_warning",
    ])
    counts["drugs"] = n
    print(f"    Wrote {n} drugs")

    # --- drugbank_interactions.csv.gz (from ChEMBL activities + targets) ---
    print("\n  Processing drugbank_interactions.csv.gz...")
    activities = []
    acts_file = chembl_dir / "activities.jsonl"
    if acts_file.exists():
        with open(acts_file) as f:
            for line in f:
                if line.strip():
                    try:
                        activities.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    targets = []
    targets_file = chembl_dir / "targets_uniprot.jsonl"
    if targets_file.exists():
        with open(targets_file) as f:
            for line in f:
                if line.strip():
                    try:
                        targets.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    target_by_id = {t["target_chembl_id"]: t for t in targets}

    inter_rows = []
    for act in activities:
        mol_id = act.get("molecule_chembl_id")
        tgt_id = act.get("target_chembl_id")
        if not mol_id or not tgt_id:
            continue
        tgt = target_by_id.get(tgt_id, {})
        uniprot_accessions = tgt.get("uniprot_accessions", [])
        if not uniprot_accessions:
            continue
        uniprot_id = uniprot_accessions[0]
        if not re.match(r'^[OPQ][0-9][A-Z0-9]{3}[0-9]', uniprot_id) and \
           not re.match(r'^[A-NR-Z][0-9][A-Z0-9]{3}[0-9]', uniprot_id):
            continue
        stype = (act.get("standard_type") or "").upper()
        if "IC50" in stype:
            action_type = "inhibitor"
        elif stype == "KI":
            action_type = "inhibitor"
        elif "EC50" in stype or "AC50" in stype:
            action_type = "unknown"  # ROOT FIX Finding 4
        elif "KD" in stype:
            action_type = "binder"
        elif "INHIB" in stype:
            action_type = "inhibitor"
        elif "ACTIV" in stype or "AGON" in stype:
            action_type = "activator"
        else:
            action_type = "unknown"
        inter_rows.append({
            "drugbank_id": mol_id,
            "target_name": tgt.get("pref_name", ""),
            "target_id": tgt_id,
            "drugbank_target_be_id": "",
            "uniprot_id": uniprot_id,
            "action_type": action_type,
            "organism": tgt.get("organism") or "Homo sapiens",
            "interactor_type": "protein",
            "is_known_action": "true" if act.get("pchembl_value") else "false",
            "binding_position": "",
            "target_sequence": "",
            "source": "chembl",
            "source_id": str(act.get("activity_id", "")),
            "standard_type": stype,
            "standard_value": act.get("standard_value", ""),
            "standard_units": act.get("standard_units", ""),
            "pchembl_value": act.get("pchembl_value", ""),
        })
    n = _write_csv_gzip(PROCESSED_DATA_DIR / "drugbank_interactions.csv.gz", inter_rows, [
        "drugbank_id", "target_name", "target_id", "drugbank_target_be_id",
        "uniprot_id", "action_type", "organism", "interactor_type",
        "is_known_action", "binding_position", "target_sequence",
        "source", "source_id", "standard_type", "standard_value",
        "standard_units", "pchembl_value",
    ])
    counts["interactions"] = n
    print(f"    Wrote {n} interactions")

    # --- uniprot_proteins.csv ---
    print("\n  Processing uniprot_proteins.csv...")
    protein_rows = []
    uniprot_file = uniprot_dir / "human_proteins.tsv"
    if uniprot_file.exists():
        with open(uniprot_file) as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                acc = row.get("Entry") or row.get("accession")
                if not acc:
                    continue
                gene_names = row.get("Gene Names") or ""
                gene_symbol = gene_names.split(" ")[0] if gene_names else ""
                protein_name = row.get("Protein names") or ""
                protein_name = re.sub(r"\{[^}]*\}", "", protein_name).strip()
                protein_rows.append({
                    "uniprot_ac": acc,
                    "accession": acc,
                    "name": protein_name,
                    "protein_name": protein_name,
                    "gene_name": gene_symbol,
                    "gene_symbol": gene_symbol,
                    "organism": row.get("Organism") or "Homo sapiens",
                    "length": row.get("Length") or "",
                    "sequence": "",
                })
    n = _write_csv(PROCESSED_DATA_DIR / "uniprot_proteins.csv", protein_rows, [
        "uniprot_ac", "accession", "name", "protein_name",
        "gene_name", "gene_symbol", "organism", "length", "sequence",
    ])
    counts["proteins"] = n
    print(f"    Wrote {n} proteins")

    # --- string_protein_protein_interactions.csv ---
    print("\n  Processing string_protein_protein_interactions.csv...")
    ppi_rows = []
    string_file = string_dir / "9606.protein.links.full.v12.0.txt.gz"
    if string_file.exists():
        with gzip.open(string_file, "rt") as f:
            reader = csv.DictReader(f, delimiter=" ")
            for row in reader:
                score = int(row.get("combined_score", 0))
                if score < 700:
                    continue
                p1 = row.get("protein1", "").replace("9606.", "")
                p2 = row.get("protein2", "").replace("9606.", "")
                if not p1 or not p2:
                    continue
                ppi_rows.append({
                    "uniprot_ac_a": p1,
                    "uniprot_ac_b": p2,
                    "score": score,
                    "combined_score": score,
                })
                if len(ppi_rows) >= 50000:
                    break
    n = _write_csv(PROCESSED_DATA_DIR / "string_protein_protein_interactions.csv", ppi_rows, [
        "uniprot_ac_a", "uniprot_ac_b", "score", "combined_score",
    ])
    counts["ppis"] = n
    print(f"    Wrote {n} PPIs")

    # --- pubchem_enrichment.csv ---
    print("\n  Processing pubchem_enrichment.csv...")
    pubchem_rows = []
    pubchem_file = pubchem_dir / "enrichment.jsonl"
    if pubchem_file.exists():
        with open(pubchem_file) as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                        pubchem_rows.append({
                            "inchikey": r.get("inchikey", ""),
                            "canonical_smiles": r.get("canonical_smiles", ""),
                            "isomeric_smiles": r.get("isomeric_smiles", ""),
                            "molecular_weight": r.get("molecular_weight", ""),
                            "xlogp": r.get("xlogp", ""),
                            "tpsa": r.get("tpsa", ""),
                            "h_bond_donor_count": r.get("h_bond_donor_count", ""),
                            "h_bond_acceptor_count": r.get("h_bond_acceptor_count", ""),
                            "rotatable_bond_count": r.get("rotatable_bond_count", ""),
                            "pubchem_cid": r.get("pubchem_cid", ""),
                        })
                    except json.JSONDecodeError:
                        pass
    n = _write_csv(PROCESSED_DATA_DIR / "pubchem_enrichment.csv", pubchem_rows, [
        "inchikey", "canonical_smiles", "isomeric_smiles",
        "molecular_weight", "xlogp", "tpsa",
        "h_bond_donor_count", "h_bond_acceptor_count",
        "rotatable_bond_count", "pubchem_cid",
    ])
    counts["pubchem"] = n
    print(f"    Wrote {n} PubChem enrichments")

    # --- chembl_drugs.csv ---
    print("\n  Processing chembl_drugs.csv...")
    chembl_drug_rows = []
    # Build uniprot → target map for the uniprot_accession field
    target_uniprot_map = {}
    for t in targets:
        for acc in (t.get("uniprot_accessions") or []):
            target_uniprot_map[t["target_chembl_id"]] = acc
    # Build drug → target map
    drug_target_map = {}
    for act in activities:
        mol_id = act.get("molecule_chembl_id")
        tgt_id = act.get("target_chembl_id")
        if mol_id and tgt_id and mol_id not in drug_target_map:
            uniprot = target_uniprot_map.get(tgt_id, "")
            if uniprot:
                drug_target_map[mol_id] = {
                    "uniprot_accession": uniprot,
                    "target_name": next((t.get("pref_name", "") for t in targets if t["target_chembl_id"] == tgt_id), ""),
                }
    for struct in structures:
        cid = struct.get("chembl_id")
        if not cid:
            continue
        tgt_info = drug_target_map.get(cid, {})
        chembl_drug_rows.append({
            "chembl_id": cid,
            "inchikey": struct.get("inchikey", ""),
            "smiles": struct.get("smiles", ""),
            "name": struct.get("name", ""),
            "uniprot_accession": tgt_info.get("uniprot_accession", ""),
            "target_name": tgt_info.get("target_name", ""),
            "molecular_weight": struct.get("molecular_weight", ""),
            "max_phase": struct.get("max_phase", ""),
            "first_approval": struct.get("first_approval", ""),
        })
    n = _write_csv(PROCESSED_DATA_DIR / "chembl_drugs.csv", chembl_drug_rows, [
        "chembl_id", "inchikey", "smiles", "name",
        "uniprot_accession", "target_name",
        "molecular_weight", "max_phase", "first_approval",
    ])
    counts["chembl_drugs"] = n
    print(f"    Wrote {n} ChEMBL drugs")

    # --- chembl_activities_clean.csv ---
    print("\n  Processing chembl_activities_clean.csv...")
    import math
    chembl_act_rows = []
    for act in activities:
        action_type = act.get("action_type", "")
        if action_type == "INHIBITOR":
            standard_relation = "<"
        elif action_type == "ACTIVATOR":
            standard_relation = ">"
        else:
            standard_relation = "="
        pchembl = act.get("pchembl_value")
        if not pchembl:
            try:
                val_nM = float(act.get("standard_value", 0))
                if val_nM > 0:
                    pchembl = round(9 - math.log10(val_nM), 2)
            except (ValueError, TypeError):
                pchembl = ""
        chembl_act_rows.append({
            "activity_id": act.get("activity_id", ""),
            "molecule_chembl_id": act.get("molecule_chembl_id", ""),
            "target_chembl_id": act.get("target_chembl_id", ""),
            "target_pref_name": act.get("target_pref_name", ""),
            "activity_type": act.get("standard_type", ""),
            "activity_value": act.get("standard_value", ""),
            "activity_units": act.get("standard_units", ""),
            "pchembl_value": pchembl,
            "standard_relation": standard_relation,
            "action_type": action_type,
            "organism": "Homo sapiens",
        })
    n = _write_csv(PROCESSED_DATA_DIR / "chembl_activities_clean.csv", chembl_act_rows, [
        "activity_id", "molecule_chembl_id", "target_chembl_id",
        "target_pref_name", "activity_type", "activity_value",
        "activity_units", "pchembl_value", "standard_relation",
        "action_type", "organism",
    ])
    counts["chembl_activities"] = n
    print(f"    Wrote {n} ChEMBL activities")

    # --- OpenTargets → OMIM + DisGeNET CSVs + derived treats edges ---
    print("\n  Processing OpenTargets → OMIM + DisGeNET + indications...")
    ot_associations = []
    ot_file = ot_dir / "gene_disease_associations.jsonl"
    if ot_file.exists():
        with open(ot_file) as f:
            for line in f:
                if line.strip():
                    try:
                        ot_associations.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    # Build gene → diseases map
    gene_to_diseases = {}
    for r in ot_associations:
        gs = r.get("gene_symbol", "")
        if gs:
            if gs not in gene_to_diseases:
                gene_to_diseases[gs] = []
            gene_to_diseases[gs].append({
                "disease_id": _fix_disease_id(r.get("disease_id", "")),
                "disease_name": r.get("disease_name", ""),
                "score": r.get("score", 0.5),
            })

    # Build uniprot → gene map from ChEMBL targets
    uniprot_to_gene = {}
    for t in targets:
        for acc in (t.get("uniprot_accessions") or []):
            uniprot_to_gene[acc] = t.get("gene_symbol", "")

    # Derive treats edges: drug → protein → gene → disease
    treats_edges = []
    seen_pairs = set()
    for inter in inter_rows:
        drug_id = inter.get("drugbank_id")
        uniprot_id = inter.get("uniprot_id")
        if not drug_id or not uniprot_id:
            continue
        gene_symbol = uniprot_to_gene.get(uniprot_id, "")
        if not gene_symbol:
            continue
        diseases = gene_to_diseases.get(gene_symbol, [])
        for dis in diseases:
            key = (drug_id, dis["disease_id"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            treats_edges.append({
                "drugbank_id": drug_id,
                "disease_id": dis["disease_id"],
                "disease_name": dis["disease_name"],
                "indication_type": "derived_multi_hop",
                "source": "derived_from_chembl_targets_x_opentargets_gda",
                "evidence": f"drug targets {uniprot_id} ({gene_symbol}) associated with disease",
                "uniprot_id": uniprot_id,
                "confidence": str(dis.get("score", 0.5)),
            })
    print(f"    Derived {len(treats_edges)} treats edges (multi-hop)")

    # Also keep the existing indications if present
    existing_indications = PROCESSED_DATA_DIR / "drugbank_indications.csv"
    if existing_indications.exists():
        with open(existing_indications) as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("drugbank_id"), row.get("disease_id"))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                row["source"] = row.get("source", "drugbank_indications")
                row.setdefault("evidence", "structured")
                row.setdefault("uniprot_id", "")
                row.setdefault("confidence", "0.8")
                treats_edges.append(row)

    n = _write_csv(PROCESSED_DATA_DIR / "drugbank_indications.csv", treats_edges, [
        "drugbank_id", "disease_id", "disease_name",
        "indication_type", "source", "evidence",
        "uniprot_id", "confidence",
    ])
    counts["indications"] = n
    print(f"    Wrote {n} indications")

    # Write OMIM CSV (OpenTargets + existing OMIM fixture)
    omim_rows = []
    # Load existing OMIM fixture if present
    existing_omim = PROCESSED_DATA_DIR / "omim_gene_disease_associations.csv"
    existing_omim_backup = PHASE1_ROOT / "processed_data_toy_backup" / "omim_gene_disease_associations.csv"
    omim_source = existing_omim if existing_omim.exists() else existing_omim_backup
    if omim_source.exists():
        with open(omim_source) as f:
            reader = csv.DictReader(f)
            for row in reader:
                omim_rows.append(row)
    # Add OpenTargets
    for r in ot_associations:
        did = _fix_disease_id(r.get("disease_id", ""))
        omim_rows.append({
            "phenotype_name": r.get("disease_name", ""),
            "phenotype_mim": "",
            "mapping_key": "3",
            "gene_symbols_raw": r.get("gene_symbol", ""),
            "gene_mim": "",
            "cyto_location": "",
            "association_modifier": "",
            "source_format": "opentargets",
            "source_line_number": "",
            "gene_symbol": r.get("gene_symbol", ""),
            "disease_id": did,
            "disease_name": r.get("disease_name", ""),
            "source": "OpenTargets",
            "canonical_disease_id": did,
            "score": r.get("score", 0.5),
            "association_type": "curated",
            "is_susceptibility": "False",
            "uniprot_id": "",
        })
    n = _write_csv(PROCESSED_DATA_DIR / "omim_gene_disease_associations.csv", omim_rows, [
        "phenotype_name", "phenotype_mim", "mapping_key", "gene_symbols_raw",
        "gene_mim", "cyto_location", "association_modifier", "source_format",
        "source_line_number", "gene_symbol", "disease_id", "disease_name",
        "source", "canonical_disease_id", "score", "association_type",
        "is_susceptibility", "uniprot_id",
    ])
    counts["omim_gda"] = n
    print(f"    Wrote {n} OMIM GDA rows")

    # Write DisGeNET CSV
    dis_rows = []
    for r in ot_associations:
        did = _fix_disease_id(r.get("disease_id", ""))
        dis_rows.append({
            "gene_id": "",
            "ncbi_gene_id": "",
            "gene_symbol": r.get("gene_symbol", ""),
            "disease_id": did,
            "disease_name": r.get("disease_name", ""),
            "score": r.get("score", 0.5),
            "source": "OpenTargets",
            "evidence": "opentargets_association_score",
            "pmid_list": "",
        })
    n = _write_csv(PROCESSED_DATA_DIR / "disgenet_gene_disease_associations.csv", dis_rows, [
        "gene_id", "ncbi_gene_id", "gene_symbol", "disease_id",
        "disease_name", "score", "source", "evidence", "pmid_list",
    ])
    counts["disgenet_gda"] = n
    print(f"    Wrote {n} DisGeNET GDA rows")

    return counts


# ============================================================================
# Main CLI
# ============================================================================

SOURCES = {
    "chembl": ("ChEMBL FDA-approved drugs + activities", download_chembl),
    "uniprot": ("UniProt reviewed human proteins", download_uniprot),
    "string": ("STRING human protein-protein interactions", download_string),
    "pubchem": ("PubChem compound enrichments", download_pubchem),
    "opentargets": ("OpenTargets gene-disease associations (free OMIM/DisGeNET alternative)", download_opentargets),
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Automatically download all biomedical data sources (FREE, no login, no API key).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true", help="List all available sources")
    parser.add_argument("--source", metavar="NAME", help="Download a specific source")
    parser.add_argument("--all", action="store_true", help="Download ALL sources (default)")
    parser.add_argument("--process", action="store_true",
                        help="After downloading, process raw data → Phase 1 CSVs")
    parser.add_argument("--max-drugs", type=int, default=2996, help="Max ChEMBL drugs to download")
    parser.add_argument("--max-activities", type=int, default=5000, help="Max ChEMBL activities")
    parser.add_argument("--max-ppis", type=int, default=50000, help="Max STRING PPIs")
    parser.add_argument("--max-pubchem", type=int, default=100, help="Max PubChem enrichments")
    args = parser.parse_args(argv)

    if args.list:
        print("=" * 70)
        print("AVAILABLE DATA SOURCES (all FREE, no login, no API key)")
        print("=" * 70)
        for name, (desc, _) in SOURCES.items():
            print(f"  {name:15s} — {desc}")
        print("\nDrugBank: PAUSED since May 2026. Using ChEMBL as primary (default).")
        print("OMIM/DisGeNET: Using OpenTargets as free alternative (no API key).")
        return 0

    # Default: download all
    if not args.source:
        args.all = True

    results = {}

    if args.source:
        if args.source not in SOURCES:
            print(f"ERROR: unknown source '{args.source}'")
            print(f"Available: {', '.join(SOURCES.keys())}")
            return 1
        desc, func = SOURCES[args.source]
        print(f"\nDownloading {args.source}: {desc}")
        # Pass appropriate args
        if args.source == "chembl":
            results[args.source] = func(max_drugs=args.max_drugs, max_activities=args.max_activities)
        elif args.source == "string":
            results[args.source] = func(max_ppis=args.max_ppis)
        elif args.source == "pubchem":
            results[args.source] = func(max_compounds=args.max_pubchem)
        else:
            results[args.source] = func()
    elif args.all:
        print("\n" + "=" * 70)
        print("DOWNLOADING ALL DATA SOURCES")
        print("=" * 70)
        print("All sources are FREE — no login, no API key required.")
        print("DrugBank: using ChEMBL as primary (DrugBank academic downloads paused since May 2026).")
        print("OMIM/DisGeNET: using OpenTargets as free alternative (no API key needed).")
        results["chembl"] = download_chembl(max_drugs=args.max_drugs, max_activities=args.max_activities)
        results["uniprot"] = download_uniprot()
        results["string"] = download_string(max_ppis=args.max_ppis)
        results["pubchem"] = download_pubchem(max_compounds=args.max_pubchem)
        results["opentargets"] = download_opentargets()

    if args.process:
        process_counts = process_to_csvs()
        results["processed"] = process_counts

    # Summary
    print("\n" + "=" * 70)
    print("DOWNLOAD SUMMARY")
    print("=" * 70)
    for source, counts in results.items():
        if isinstance(counts, dict):
            print(f"  {source}: {counts}")
    print("=" * 70)
    print(f"\nRaw data saved to: {RAW_DATA_DIR}")
    if args.process:
        print(f"Processed CSVs saved to: {PROCESSED_DATA_DIR}")
        print(f"\nTo run the pipeline with this real data:")
        print(f"  python run_unified.py --yes --skip-download")
    return 0


if __name__ == "__main__":
    sys.exit(main())
