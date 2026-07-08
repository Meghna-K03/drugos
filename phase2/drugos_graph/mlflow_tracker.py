"""DrugOS Graph Module — MLflow Experiment Tracker
====================================================
Logs training metrics, model parameters, and artifacts to MLflow
for experiment tracking and reproducibility.
"""

import logging
from typing import Any, Dict, Optional

from .config import ensure_dirs

logger = logging.getLogger(__name__)


class MLflowTracker:
    """Tracks experiments using MLflow.

    Falls back to local file logging if MLflow is not installed.
    """

    def __init__(self, experiment_name: str = "DrugOS_Week2", tracking_uri: Optional[str] = None):
        self.experiment_name = experiment_name
        self.tracking_uri = tracking_uri
        self.mlflow = None
        self.run = None
        self._local_log = []

        try:
            import mlflow
            self.mlflow = mlflow
            if tracking_uri:
                mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_experiment(experiment_name)
            logger.info(f"MLflow initialized: experiment={experiment_name}")
        except ImportError:
            logger.warning(
                "mlflow not installed — using local file logging. "
                "Install with: pip install mlflow"
            )
        except (OSError, ValueError, RuntimeError, ConnectionError) as e:
            # v41 ROOT FIX (Task J SEV3): narrowed from bare ``except Exception``.
            # The legitimate failure modes for MLflow init are:
            #   - OSError: tracking server unreachable, local file store
            #     on a read-only filesystem.
            #   - ValueError: invalid tracking_uri scheme (e.g. "foo://bar").
            #   - RuntimeError: mlflow internal state corruption
            #     (experiment exists but is in a deleted state).
            #   - ConnectionError: tracking server network issue.
            # Other exceptions (MlflowException subclasses that aren't
            # OSError/RuntimeError, AttributeError from version drift)
            # should propagate so the operator sees the real bug.
            logger.warning(
                f"MLflow initialization failed ({type(e).__name__}: {e}). "
                f"Using local file logging as fallback. Tracking URI was: "
                f"{tracking_uri!r}.",
            )
            self.mlflow = None

    def start_run(self, run_name: str = "default") -> None:
        """Start a new MLflow run.

        audit-2025 ROOT FIX (issue 4): if ``mlflow.start_run`` raises
        after partially creating the run, the previous code left
        ``self.run`` unset and the caller had no way to call
        ``end_run`` (which checks ``self.run``). The fix sets
        ``self.run`` BEFORE the potentially-failing call so that
        ``end_run`` can clean up even if ``start_run`` raised. Also
        wraps the call in try/except so a failure doesn't leave the
        tracker in an inconsistent state.
        """
        if self.mlflow:
            try:
                self.run = self.mlflow.start_run(run_name=run_name)
            except (OSError, ValueError, RuntimeError, ConnectionError) as e:
                # v41 ROOT FIX (Task J SEV3): narrowed from bare
                # ``except Exception``. Same rationale as __init__ — the
                # narrowed set covers network/URI/state failures; real
                # bugs propagate.
                logger.warning(
                    f"MLflow start_run failed ({type(e).__name__}: {e}). "
                    f"Run not started; metrics will fall back to local log."
                )
                self.run = None
                return
        logger.info(f"Started experiment run: {run_name}")

    def log_params(self, params: Dict[str, Any]) -> None:
        """Log hyperparameters."""
        # v35 ROOT FIX (L-1): the previous code appended to
        # ``self._local_log`` UNCONDITIONALLY — even when MLflow was
        # active and had already logged the params. For long runs
        # (100+ epochs × 5+ params), this caused ``_local_log`` to grow
        # unbounded (500+ entries). The fix only appends when MLflow is
        # NOT active (i.e., the local log IS the canonical record).
        if self.mlflow and self.run:
            self.mlflow.log_params(params)
        else:
            self._local_log.append({"type": "params", "data": params})
        logger.info(f"Logged params: {params}")

    def log_metrics(self, metrics: Dict[str, float], step: int = 0) -> None:
        """Log training metrics."""
        # v35 ROOT FIX (L-1): see ``log_params`` — only append to the
        # local log when MLflow is NOT active.
        if self.mlflow and self.run:
            self.mlflow.log_metrics(metrics, step=step)
        else:
            self._local_log.append({"type": "metrics", "data": metrics, "step": step})

    def log_artifact(self, path: str) -> None:
        """Log a file artifact."""
        # v35 ROOT FIX (L-1): see ``log_params`` — only append to the
        # local log when MLflow is NOT active.
        if self.mlflow and self.run:
            self.mlflow.log_artifact(path)
        else:
            self._local_log.append({"type": "artifact", "path": path})

    def end_run(self) -> None:
        """End the current MLflow run."""
        if self.mlflow and self.run:
            self.mlflow.end_run()
            self.run = None
        logger.info("Experiment run ended")

    def get_local_log(self) -> list:
        """Return local log (for fallback when MLflow is unavailable)."""
        return self._local_log

    def __enter__(self):
        self.start_run()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_run()
        return False
