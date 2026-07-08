"""
SQLite-compatible database test helpers.

These replicate the logic of ``database.loaders`` but use the SQLite dialect
so they work with the in-memory test database.  They are the authoritative
test versions — every loader test calls these instead of the PostgreSQL-
specific originals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from database.models import (
    Drug,
    DrugProteinInteraction,
    EntityMapping,
    GeneDiseaseAssociation,
    Protein,
    ProteinProteinInteraction,
)


# ---- internal helpers ----

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _df_to_dicts(df: pd.DataFrame) -> List[dict]:
    """Convert a DataFrame to a list of dicts, coercing NaN → None."""
    return df.where(df.notna(), None).to_dict(orient="records")


def _chunked(iterable: list, size: int):
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def _add_timestamps(records: List[dict], table) -> List[dict]:
    """Ensure every record dict has ``created_at`` / ``updated_at`` only if
    those columns actually exist in *table*."""
    now = _now()
    existing_cols = {c.name for c in table.columns}
    for rec in records:
        if "created_at" in existing_cols:
            rec.setdefault("created_at", now)
        if "updated_at" in existing_cols:
            rec.setdefault("updated_at", now)
    return records


# ---- Drugs ----

def sqlite_bulk_upsert_drugs(session: Session, df: pd.DataFrame, batch_size: int = 1000) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_drugs``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "name", "chembl_id", "drugbank_id", "pubchem_cid",
        "molecular_formula", "molecular_weight", "smiles",
        "is_fda_approved", "max_phase", "drug_type", "mechanism_of_action",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, Drug.__table__)
        stmt = sqlite_insert(Drug.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(index_elements=["inchikey"], set_=update_dict)
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- Proteins ----

def sqlite_bulk_upsert_proteins(session: Session, df: pd.DataFrame, batch_size: int = 1000) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_proteins``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "gene_symbol", "gene_name", "protein_name", "organism",
        "sequence", "function_desc", "string_id",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, Protein.__table__)
        stmt = sqlite_insert(Protein.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(index_elements=["uniprot_id"], set_=update_dict)
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- Drug-Protein Interactions ----

def sqlite_bulk_upsert_dpi(session: Session, df: pd.DataFrame, batch_size: int = 1000) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_dpi``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "interaction_type", "activity_value",
        "activity_type", "activity_units", "confidence_score",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, DrugProteinInteraction.__table__)
        stmt = sqlite_insert(DrugProteinInteraction.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["drug_id", "protein_id", "source", "source_id"],
            set_=update_dict,
        )
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- Protein-Protein Interactions ----

def sqlite_bulk_upsert_ppi(session: Session, df: pd.DataFrame, batch_size: int = 1000) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_ppi``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "combined_score", "experimental_score",
        "database_score", "textmining_score", "source",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, ProteinProteinInteraction.__table__)
        stmt = sqlite_insert(ProteinProteinInteraction.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["protein_a_id", "protein_b_id"],
            set_=update_dict,
        )
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- Gene-Disease Associations ----

def sqlite_bulk_upsert_gda(session: Session, df: pd.DataFrame, batch_size: int = 1000) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_gda``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "uniprot_id", "disease_name", "association_type",
        "score", "pmid_list",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, GeneDiseaseAssociation.__table__)
        stmt = sqlite_insert(GeneDiseaseAssociation.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["gene_symbol", "disease_id", "source"],
            set_=update_dict,
        )
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- Entity Mapping ----

def sqlite_bulk_upsert_entity_mapping(
    session: Session, df: pd.DataFrame, batch_size: int = 1000
) -> int:
    """SQLite-compatible reimplementation of ``bulk_upsert_entity_mapping``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    updatable_cols = [
        "canonical_name", "chembl_id", "drugbank_id", "pubchem_cid",
        "uniprot_id", "string_id", "match_confidence", "match_method",
    ]
    for chunk in _chunked(records, batch_size):
        _add_timestamps(chunk, EntityMapping.__table__)
        stmt = sqlite_insert(EntityMapping.__table__).values(chunk)
        update_dict = {
            col: stmt.excluded[col] for col in updatable_cols if col in chunk[0]
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["canonical_inchikey"],
            set_=update_dict,
        )
        session.execute(stmt)
        total += len(chunk)
    session.commit()
    return total


# ---- PubChem drug update ----

def sqlite_bulk_update_drugs_from_pubchem(
    session: Session, df: pd.DataFrame, batch_size: int = 1000
) -> int:
    """SQLite-compatible reimplementation of ``bulk_update_drugs_from_pubchem``."""
    if df.empty:
        return 0
    records = _df_to_dicts(df)
    total = 0
    update_sql = text(
        """
        UPDATE drugs
        SET pubchem_cid       = :pubchem_cid,
            molecular_formula = COALESCE(:molecular_formula, drugs.molecular_formula),
            molecular_weight  = COALESCE(:molecular_weight, drugs.molecular_weight),
            smiles            = COALESCE(:smiles, drugs.smiles)
        WHERE inchikey = :inchikey
          AND pubchem_cid IS NULL
    """
    )
    for chunk in _chunked(records, batch_size):
        session.execute(update_sql, chunk)
        total += len(chunk)
    session.commit()
    return total


# ---- Lookup maps ----

def get_inchikey_to_drug_id_map(session: Session) -> Dict[str, int]:
    """Return ``{inchikey: drug.id}`` for all drugs in the session."""
    result = session.execute(text("SELECT id, inchikey FROM drugs"))
    return {row.inchikey: row.id for row in result}


def get_uniprot_to_protein_id_map(session: Session) -> Dict[str, int]:
    """Return ``{uniprot_id: protein.id}`` for all proteins in the session."""
    result = session.execute(text("SELECT id, uniprot_id FROM proteins"))
    return {row.uniprot_id: row.id for row in result}
