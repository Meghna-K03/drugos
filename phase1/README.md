# Autonomous Drug Repurposing Platform — Phase 1 Week 1

<!-- FIX AUDIT-37: Test count below needs updating after all audit fixes are applied.
     The current number (95) may not reflect the new tests added for v7 audit fixes. -->

## Data Ingestion & Automation Pipeline

**Team Cosmic · VentureLab**

---

## Overview

This is the **complete, production-ready Phase 1 data ingestion and automation pipeline** for the Autonomous Drug Repurposing Platform. It is a Python-based ETL (Extract, Transform, Load) system that:

1. **Downloads** raw data from 6 free public biomedical databases + DrugBank (requires license)
2. **Cleans & normalizes** each dataset to a common schema
3. **Resolves entities** (same drug/protein appearing under different names across sources)
4. **Loads** the unified, cleaned data into a PostgreSQL staging database
5. **Automates** all of the above via Apache Airflow DAGs so the entire pipeline re-runs on schedule without manual intervention

The output of this phase feeds directly into Phase 2 (Neo4j Knowledge Graph Construction).

---

## Project Structure

```
drug_repurposing/
├── dags/                          # Apache Airflow DAG definitions
│   ├── master_pipeline_dag.py     # Master DAG orchestrating all 7 sources
│   ├── chembl_dag.py              # Individual source DAGs
│   ├── drugbank_dag.py
│   ├── uniprot_dag.py
│   ├── string_dag.py
│   ├── disgenet_dag.py
│   ├── omim_dag.py
│   └── pubchem_dag.py
│
├── pipelines/                     # Core ETL logic (called by DAGs)
│   ├── base_pipeline.py           # Abstract base class
│   ├── chembl_pipeline.py
│   ├── drugbank_pipeline.py
│   ├── uniprot_pipeline.py
│   ├── string_pipeline.py
│   ├── disgenet_pipeline.py
│   ├── omim_pipeline.py
│   └── pubchem_pipeline.py
│
├── cleaning/                      # Data cleaning & normalization
│   ├── normalizer.py              # InChIKey conversion, ID standardization
│   ├── deduplicator.py            # Duplicate removal logic
│   └── missing_values.py          # Missing value handlers
│
├── entity_resolution/             # Cross-database entity matching
│   ├── drug_resolver.py           # Match drugs across ChEMBL, DrugBank, PubChem
│   ├── protein_resolver.py        # Match proteins across UniProt, STRING, ChEMBL
│   └── resolver_utils.py          # Shared fuzzy match & mapping utilities
│
├── database/                      # PostgreSQL staging DB layer
│   ├── models.py                  # SQLAlchemy ORM table definitions
│   ├── connection.py              # DB connection manager
│   ├── loaders.py                 # Bulk upsert helpers
│   └── migrations/
│       └── 001_initial_schema.sql # Full schema SQL
│
├── config/
│   ├── settings.py                # All config: DB URL, API keys, file paths
│   └── .env.example               # Template for environment variables
│
├── tests/
│   ├── conftest.py                # Shared fixtures
│   ├── db_helpers.py              # SQLite test helpers
│   ├── test_chembl_pipeline.py    # 40+ tests for cleaning & pipeline
│   ├── test_entity_resolution.py  # 20+ tests for entity resolution
│   └── test_db_loaders.py         # 23+ tests for DB operations
│
├── docker-compose.yml             # PostgreSQL + Airflow
├── requirements.txt
├── Makefile
└── README.md
```

---

## Quick Start

### 1. Start Infrastructure

```bash
docker-compose up -d
```

This starts:
- **PostgreSQL 15** on port 5432 (user: `cosmic`, password: `cosmic`, db: `drug_repurposing`)
- **Airflow Webserver** on port 8080 (admin/admin)
- **Airflow Scheduler**

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Initialize Database

```bash
python -c "from database.connection import init_db; init_db()"
```

### 4. Run the Pipeline

```bash
# Run all pipelines
make download-all

# Or run individually
python -c "from pipelines.chembl_pipeline import ChEMBLPipeline; ChEMBLPipeline().run()"
python -c "from pipelines.uniprot_pipeline import UniProtPipeline; UniProtPipeline().run()"
```

### 5. Run Tests

```bash
pytest tests/ -v
```

---

## Data Sources

| Source | Description | Records | Key Fields |
|--------|-------------|---------|------------|
| **ChEMBL** | Drug-protein bioactivity | 50K+ activities | ChEMBL ID, InChIKey, SMILES, activity values |
| **DrugBank** | FDA-approved drug metadata | 11K+ drugs | DrugBank ID, mechanism of action, targets |
| **UniProt** | Protein sequences & functions | 18K+ human proteins | UniProt ID, gene name, sequence |
| **STRING** | Protein-protein interactions | 500K+ PPIs | Combined score, experimental score |
| **DisGeNET** | Gene-disease associations | 100K+ GDAs | Score, disease ID, gene symbol |
| **OMIM** | Genetic basis of diseases | 6K+ phenotypes | MIM number, phenotype, mapping key |
| **PubChem** | Structural data for compounds | Enriches existing drugs | PubChem CID, molecular formula |

---

## Database Schema

7 tables with proper indexes for graph traversal:

- **drugs** — Unified drug table (InChIKey as universal ID)
- **proteins** — Protein targets (UniProt ID as primary key)
- **drug_protein_interactions** — ChEMBL + DrugBank interactions
- **protein_protein_interactions** — STRING PPI network
- **gene_disease_associations** — DisGeNET + OMIM associations
- **entity_mapping** — Cross-database ID resolution
- **pipeline_runs** — ETL audit log

---

## Entity Resolution Strategy

Drugs are resolved across databases using a priority-based strategy:

1. **InChIKey exact match** (confidence: 1.0)
2. **InChIKey connectivity match** — first 14 chars (confidence: 0.9)
3. **Name normalization match** — lowercase, strip punctuation (confidence: 0.8)
4. **PubChem cross-reference API** (confidence: 0.7)

Proteins are resolved via:
1. **UniProt ID exact match** (confidence: 1.0)
2. **Gene symbol + organism match** (confidence: 0.85)
3. **Protein name fuzzy match** (confidence: 0.6)

---

## Airflow DAG

The master pipeline DAG runs weekly (Sunday 2 AM UTC) with:
- Proper dependency ordering
- DrugBank XML presence check (BranchPythonOperator)
- SLA enforcement (4 hours per task)
- Retry logic (2 retries, 30-minute delay)
- Email-on-failure notifications

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

```bash
# Required for some sources
DISGENET_API_KEY=           # Free from disgenet.org
OMIM_API_KEY=               # Free from omim.org/api

# DrugBank requires manual download
# Place the XML file at: raw_data/drugbank/drugbank_all_full_database.xml.gz
```

---

## Test Suite

**95 tests** covering all major modules:

- **Normalizer tests**: InChIKey conversion, validation, drug record standardization, activity unit normalization
- **Deduplication tests**: InChIKey dedup (most-complete row), interaction dedup (most potent)
- **Missing value tests**: InChIKey recovery, drug defaults, protein cleaning, GDA score validation
- **Entity resolution tests**: Exact match, connectivity match, name normalization, fuzzy matching, no false positives
- **Database tests**: Bulk upsert insert/update, batch processing, ORM relationships, schema validation
- **Pipeline audit tests**: Run logging on success and failure

---

## Notes

1. **DrugBank** requires manual download (login-gated). The pipeline fails gracefully with clear instructions if the XML file is missing.
2. **InChIKey is your universal key.** Every drug must have one before it enters the DB.
3. **All upserts use ON CONFLICT DO UPDATE** — the pipeline can re-run weekly without creating duplicates.
4. **STRING IDs must be mapped to UniProt IDs** using the aliases file. Do not hardcode mappings.
5. **Large datasets** are streamed/chunked to avoid memory issues.

---

*Team Cosmic · VentureLab · Phase 1 Week 1*
