# DRKG Type → Neo4j Label Dictionary

> Fixes audit issue 13.6 — data dictionary for the label mapping.

This document is the canonical reference for every DRKG type name, its
Neo4j storage label, its source ontology, example IDs, and notes on
deprecation or aliasing. When in doubt about which type to use, consult
this file.

## Conventions

- **DRKG types** use natural English spelling with spaces:
  `"Side Effect"`, not `"Side_Effect"`.
- **Neo4j labels** are PascalCase: `SideEffect`, `MedDRATerm`.
- **Abbreviations** preserved as-is: `Atc`, `Tax` (DRKG convention).
- **Case aliases**: `ATC`/`Atc`/`atc` → `Atc`; `TAX`/`Tax`/`tax` → `Tax`.
  The canonical (first-seen) spelling wins in the reverse map.
- **Deprecated types** emit `DeprecationWarning` and have a replacement.
  Do NOT use deprecated types in new code.
- **Storage labels** are always CamelCase for RDF/JSON-LD compatibility.
  Semantic DRKG types may use underscores (e.g., `MedDRA_Term`); storage
  labels do not (e.g., `MedDRATerm`).

## The 17 canonical types

| DRKG Type | Neo4j Label | Source | Ontology | Ontology Version | Example ID | Notes |
|-----------|-------------|--------|----------|------------------|------------|-------|
| Compound | Compound | DRKG + ChEMBL + DrugBank | ChEMBL/DrugBank | ChEMBL_34 | DB00001 | Small-molecule drug |
| Disease | Disease | DRKG + DisGeNET + OMIM | UMLS | 2024AA | C0018799 | Medical condition |
| Gene | Gene | DRKG + NCBI Gene | NCBI Gene | current | 1956 | Protein-coding gene (DRKG uses Gene for gene+protein) |
| Protein | Protein | UniProt (not in DRKG) | UniProt | 2024_05 | P04626 | Protein product; only when UniProt loaded |
| Anatomy | Anatomy | DRKG + Uberon | Uberon | 2024_05 | UBERON:0002370 | Anatomical structure |
| Pharmacologic Class | PharmacologicClass | DRKG | DrugBank | current | DBCAT000500 | Drug mechanism class |
| Side Effect | SideEffect | SIDER (legacy) | MedDRA | 26.0 | 10000001 | **DEPRECATED** — use MedDRA_Term |
| Symptom | Symptom | DRKG | UMLS | 2024AA | C0231216 | Clinical symptom |
| Pathway | Pathway | DRKG + KEGG + Reactome | KEGG/Reactome | v28 | hsa00010 | Biochemical pathway |
| Biological Process | BiologicalProcess | DRKG + GO | GO | 2024_05 | GO:0008150 | GO biological process |
| Molecular Function | MolecularFunction | DRKG + GO | GO | 2024_05 | GO:0003674 | GO molecular function |
| Cellular Component | CellularComponent | DRKG + GO | GO | 2024_05 | GO:0005575 | GO cellular component |
| Taxonomy | Taxonomy | DRKG + NCBI Taxonomy | NCBI Taxonomy | current | 9606 | Organism |
| Gene Expression | GeneExpression | DRKG + GTEx | GTEx | v8 | GTEX-1117F | Tissue-specific gene expression |
| Atc | Atc | DRKG + DrugBank | WHO ATC | 2024 | L01AA01 | WHO ATC classification (case-aliased with ATC) |
| Tax | Tax | DRKG | NCBI Taxonomy | current | 9606 | Alternative taxonomy type (case-aliased with TAX) |
| MedDRA_Term | MedDRATerm | SIDER (canonical) | MedDRA | 26.0 | 10000001 | Adverse event term — replaces Side Effect |

## Case Aliases (issue 3.7)

The following case aliases are documented and intentional. The reverse
map (`NEO4J_LABEL_TO_DRKG_NODE_TYPE`) returns the canonical (first-seen)
spelling.

| Alias | Canonical | Reason |
|-------|-----------|--------|
| `ATC` | `Atc` | WHO standard uses all-caps; DRKG uses mixed case. Both map to Neo4j label `Atc`. |
| `TAX` | `Tax` | Alternative spelling; DRKG uses mixed case. Both map to Neo4j label `Tax`. |

## Deprecated Types (issue 3.6, 14.6)

The following types are deprecated and emit `DeprecationWarning` when
passed to `drkg_node_type_to_neo4j_label`. They will be removed in v2.0.

| Deprecated Type | Replacement | Removal Target |
|-----------------|-------------|----------------|
| `Side Effect` | `MedDRA_Term` | v2.0 |

## Legacy Label Aliases (issue 15.4)

The following Neo4j labels are recognized as aliases of canonical labels.
When `drkg_node_type_to_neo4j_label` receives one of these as input, it
returns the canonical storage label.

| Legacy Label | Canonical Label | Reason |
|--------------|-----------------|--------|
| `SideEffect` | `MedDRATerm` | v0.x → v1.0 rename (post MedDRA_Term migration) |
| `Side_Effect` | `MedDRATerm` | Fallback artifact from sanitization |
| `MedDRA_Term` | `MedDRATerm` | Semantic type → storage label |

## Source Ontologies Reference

| Ontology | Version | Maintainer | URL |
|----------|---------|-----------|-----|
| UMLS | 2024AA | NLM | https://www.nlm.nih.gov/research/umls/ |
| MedDRA | 26.0 | ICH | https://www.meddra.org/ |
| NCBI Gene | current | NCBI | https://www.ncbi.nlm.nih.gov/gene/ |
| NCBI Taxonomy | current | NCBI | https://www.ncbi.nlm.nih.gov/taxonomy |
| UniProt | 2024_05 | UniProt Consortium | https://www.uniprot.org/ |
| ChEMBL | 34 | EMBL-EBI | https://www.ebi.ac.uk/chembl/ |
| DrugBank | 5.1.12 | DrugBank | https://go.drugbank.com/ |
| GO | 2024_05 | GO Consortium | http://geneontology.org/ |
| KEGG | current | Kanehisa Labs | https://www.genome.jp/kegg/ |
| Reactome | v28 | OICR | https://reactome.org/ |
| Uberon | 2024_05 | OBO Foundry | http://obofoundry.org/ontology/uberon.html |
| GTEx | v8 | GTEx Consortium | https://gtexportal.org/ |
| WHO ATC | 2024 | WHO | https://www.who.int/tools/atc-ddd-toolkit/atc-classification |

## Patient Safety Notes

The single most important entry in this dictionary is `MedDRA_Term` →
`MedDRATerm`. Without this entry:

1. SIDER's `('Compound', 'causes_adverse_event', 'MedDRA_Term')` edges
   would create nodes under a fallback label (e.g., `MedDRA_Term`).
2. The canonical query
   `MATCH (:Compound)-[:causes_adverse_event]->(:MedDRATerm)` would
   return zero results.
3. The RL safety ranker would see zero adverse events for every drug.
4. Every drug would be ranked as 'green' (safe) — including drugs with
   known severe adverse events.

This is why `MedDRA_Term` is marked with `# PATIENT SAFETY:` comments
in `utils.py` and `label_map.yaml`. Do not remove or rename this entry
without a migration plan.
