/**
 * Core ML stubs — Knowledge Graph, Dataset Pipeline, RL Ranker.
 *
 * These three subsystems are intentionally NOT implemented here. They are
 * owned by the standalone ML services:
 *
 *   - Knowledge Graph: Neo4j graph database (Phase 2 of the build plan).
 *   - Dataset Pipeline: Apache Airflow ETL from ChEMBL/DrugBank/UniProt/STRING/
 *     DisGeNET/OMIM/PubChem (Phase 1).
 *   - RL Hypothesis Ranker: Stable-Baselines3 PPO agent (Phase 4).
 *
 * When the ML services are deployed they will register themselves with this
 * backend by setting environment variables pointing to their endpoints. Until
 * then, these stubs return a clear "service not yet deployed" response —
 * they NEVER return fabricated predictions, fabricated graph data, or
 * fabricated dataset statistics.
 *
 * Returning fake data here would be a serious scientific integrity violation.
 * The user explicitly stated: "no giving fake output... the outputs are
 * affecting humans i would be behind bars." We honor that constraint.
 */

export const ML_SERVICE_STATUS = {
  knowledgeGraph: {
    name: "Knowledge Graph Service",
    description:
      "Neo4j-backed multi-modal biomedical knowledge graph (drugs, proteins, " +
      "pathways, diseases, outcomes). Owned by Phase 2 of the build plan.",
    envVar: "KG_SERVICE_URL",
    deployedAt: null as Date | null,
  },
  dataset: {
    name: "Dataset Pipeline Service",
    description:
      "Apache Airflow ETL pipeline ingesting from ChEMBL, DrugBank, UniProt, " +
      "STRING, DisGeNET, OMIM, and PubChem. Owned by Phase 1 of the build plan.",
    envVar: "DATASET_SERVICE_URL",
    deployedAt: null as Date | null,
  },
  rl: {
    name: "RL Hypothesis Ranker",
    description:
      "Reinforcement learning agent (Stable-Baselines3 PPO) that ranks " +
      "drug-disease repurposing hypotheses by plausibility, safety, and " +
      "market opportunity. Owned by Phase 4 of the build plan.",
    envVar: "RL_SERVICE_URL",
    deployedAt: null as Date | null,
  },
} as const;

export interface MlServiceAvailability {
  available: boolean;
  service: string;
  description: string;
  reason: string;
}

export function checkKnowledgeGraphAvailability(): MlServiceAvailability {
  const url = process.env.KG_SERVICE_URL;
  if (!url) {
    return {
      available: false,
      service: ML_SERVICE_STATUS.knowledgeGraph.name,
      description: ML_SERVICE_STATUS.knowledgeGraph.description,
      reason:
        "KG_SERVICE_URL environment variable is not set. The standalone " +
        "Neo4j knowledge graph service has not been deployed yet. This " +
        "endpoint refuses to return fabricated graph data.",
    };
  }
  return {
    available: true,
    service: ML_SERVICE_STATUS.knowledgeGraph.name,
    description: ML_SERVICE_STATUS.knowledgeGraph.description,
    reason: `Knowledge graph service is configured at ${url}.`,
  };
}

export function checkDatasetAvailability(): MlServiceAvailability {
  const url = process.env.DATASET_SERVICE_URL;
  if (!url) {
    return {
      available: false,
      service: ML_SERVICE_STATUS.dataset.name,
      description: ML_SERVICE_STATUS.dataset.description,
      reason:
        "DATASET_SERVICE_URL environment variable is not set. The standalone " +
        "Apache Airflow dataset pipeline has not been deployed yet. This " +
        "endpoint refuses to return fabricated dataset statistics.",
    };
  }
  return {
    available: true,
    service: ML_SERVICE_STATUS.dataset.name,
    description: ML_SERVICE_STATUS.dataset.description,
    reason: `Dataset service is configured at ${url}.`,
  };
}

export function checkRlAvailability(): MlServiceAvailability {
  const url = process.env.RL_SERVICE_URL;
  if (!url) {
    return {
      available: false,
      service: ML_SERVICE_STATUS.rl.name,
      description: ML_SERVICE_STATUS.rl.description,
      reason:
        "RL_SERVICE_URL environment variable is not set. The standalone " +
        "Stable-Baselines3 RL hypothesis ranker has not been deployed yet. " +
        "This endpoint refuses to return fabricated repurposing predictions.",
    };
  }
  return {
    available: true,
    service: ML_SERVICE_STATUS.rl.name,
    description: ML_SERVICE_STATUS.rl.description,
    reason: `RL service is configured at ${url}.`,
  };
}
