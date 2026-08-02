"""
DrugOS backend package.

This package is the FastAPI HTTP layer that wraps the existing Python
library code (rl/, phase1/, phase2/, graph_transformer/) so the Next.js
frontend has real services to call at RL_SERVICE_URL, KG_SERVICE_URL and
DATASET_SERVICE_URL.

Before this package existed, the Python side had zero HTTP server code —
only batch scripts (run_full_platform.py etc.) that wrote CSV/JSON to
disk. The frontend's /api/rl, /api/knowledge-graph and /api/dataset routes
proxy to the URLs this package serves.
"""
