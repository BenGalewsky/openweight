"""MLflow tracking: a parent run per eval branch, a child run per step, traces linked by session.

Everything goes to the existing experiment ``MLFLOW_EXPERIMENT_ID``; this module never creates
experiments. Configuration comes from the environment, which `load_env` fills from ``.env``.
"""

import os
import time
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv
from mlflow import MlflowClient
from mlflow.entities import Metric, Param
from mlflow.exceptions import MlflowException
from mlflow.utils.mlflow_tags import MLFLOW_PARENT_RUN_ID

from openweight.branches import Evaluation
from openweight.promptdb import Step

# Passed through to the harnesses so their tracing reaches the same server and experiment.
MLFLOW_ENV = (
    "MLFLOW_TRACKING_URI",
    "MLFLOW_TRACKING_USERNAME",
    "MLFLOW_TRACKING_PASSWORD",
    "MLFLOW_EXPERIMENT_ID",
)
ROLE_TAG = "openweight.role"
SUPERSEDED_TAG = "openweight.superseded"
SESSION_METADATA = "mlflow.trace.session"
TRACE_POLL_S = 2.0


class TrackingError(RuntimeError):
    """The tracking server or experiment is not usable."""


def load_env(repo: Path) -> None:
    """Load ``.env`` from the main repository; variables already set take precedence."""
    load_dotenv(repo / ".env", override=False)


def harness_env() -> dict[str, str]:
    """The MLflow variables to pass into the sandbox (those that are set)."""
    return {key: os.environ[key] for key in MLFLOW_ENV if os.environ.get(key)}


class Tracker:
    def __init__(self, experiment_id: str, client: MlflowClient | None = None) -> None:
        self.experiment_id = experiment_id
        self.client = client or MlflowClient()

    @classmethod
    def from_env(cls) -> "Tracker":
        """A tracker for ``MLFLOW_EXPERIMENT_ID``, failing fast if the experiment is not there."""
        experiment_id = os.environ.get("MLFLOW_EXPERIMENT_ID")
        if not experiment_id:
            raise TrackingError("MLFLOW_EXPERIMENT_ID is not set (see .env.example)")
        client = MlflowClient()
        try:
            experiment = client.get_experiment(experiment_id)
        except MlflowException as e:
            raise TrackingError(
                f"experiment {experiment_id} is not available on {client.tracking_uri}: {e.message}"
            ) from None
        if experiment.lifecycle_stage != "active":
            raise TrackingError(f"experiment {experiment_id} ({experiment.name}) is deleted")
        return cls(experiment_id, client)

    def branch_run(self, evaluation: Evaluation, harness_version: str, promptdb_sha: str) -> str:
        """The branch's parent run for this PromptDB revision, created if needed, marked running."""
        tags = {
            ROLE_TAG: "branch",
            "branch": evaluation.branch,
            "harness": evaluation.harness,
            "model": evaluation.model,
            "effort": evaluation.effort or "",
            "harness_version": harness_version,
            "promptdb_sha": promptdb_sha,
        }
        found = self.client.search_runs(
            [self.experiment_id],
            f"tags.`{ROLE_TAG}` = 'branch' and tags.branch = '{evaluation.branch}'"
            f" and tags.promptdb_sha = '{promptdb_sha}'",
            max_results=1,
            order_by=["attributes.start_time DESC"],
        )
        if not found:
            return self.client.create_run(
                self.experiment_id, run_name=evaluation.branch, tags=tags
            ).info.run_id
        run_id = found[0].info.run_id
        self.client.update_run(run_id, status="RUNNING")
        self.set_tags(run_id, tags)
        return run_id

    def start_step(
        self,
        parent_id: str,
        evaluation: Evaluation,
        step: Step,
        promptdb_sha: str,
        timeout_s: float,
    ) -> str:
        run = self.client.create_run(
            self.experiment_id,
            run_name=f"step-{step.number:02d}",
            tags={
                MLFLOW_PARENT_RUN_ID: parent_id,
                ROLE_TAG: "step",
                "branch": evaluation.branch,
                "harness": evaluation.harness,
                "model": evaluation.model,
                "session_model": evaluation.session_model,
                "promptdb_sha": promptdb_sha,
            },
        )
        params = {
            "step": step.number,
            "title": step.title,
            "phase": step.phase,
            "phase_title": step.phase_title,
            "harness": evaluation.harness,
            "model": evaluation.model,
            "session_model": evaluation.session_model,
            "effort": evaluation.effort or "",
            "timeout_s": timeout_s,
        }
        self.client.log_batch(
            run.info.run_id, params=[Param(key, str(value)) for key, value in params.items()]
        )
        return run.info.run_id

    def log_metrics(self, run_id: str, metrics: Mapping[str, float]) -> None:
        now = int(time.time() * 1000)
        self.client.log_batch(
            run_id, metrics=[Metric(key, value, now, 0) for key, value in metrics.items()]
        )

    def set_tags(self, run_id: str, tags: Mapping[str, str]) -> None:
        for key, value in tags.items():
            self.client.set_tag(run_id, key, value)

    def log_text(self, run_id: str, text: str, artifact_file: str) -> None:
        self.client.log_text(run_id, text, artifact_file)

    def log_artifact(self, run_id: str, path: Path) -> None:
        self.client.log_artifact(run_id, str(path))

    def log_artifacts(self, run_id: str, directory: Path, artifact_path: str) -> None:
        self.client.log_artifacts(run_id, str(directory), artifact_path)

    def metrics(self, run_id: str) -> dict[str, float]:
        """A run's latest metrics, or none if the run is not on the server."""
        try:
            return dict(self.client.get_run(run_id).data.metrics)
        except MlflowException:
            return {}

    def end(self, run_id: str, status: str = "FINISHED") -> None:
        self.client.set_terminated(run_id, status)

    def supersede(self, run_ids: list[str]) -> None:
        """Mark step runs whose commits were discarded by a reset."""
        for run_id in run_ids:
            self.client.set_tag(run_id, SUPERSEDED_TAG, "true")

    def link_traces(
        self, run_id: str, session_id: str, tags: Mapping[str, str], wait_s: float = 60
    ) -> list[str]:
        """Link the harness's traces for ``session_id`` to the step run and tag them.

        The tracing hook writes the trace as the session ends, so it may appear a little after the
        harness exits; this polls for up to ``wait_s`` seconds. Returns the linked trace ids.
        """
        deadline = time.monotonic() + wait_s
        while True:
            traces = self.client.search_traces(
                locations=[self.experiment_id],
                filter_string=f"metadata.`{SESSION_METADATA}` = '{session_id}'",
                include_spans=False,
            )
            if traces or time.monotonic() >= deadline:
                break
            time.sleep(TRACE_POLL_S)
        trace_ids = [trace.info.trace_id for trace in traces]
        if trace_ids:
            self.client.link_traces_to_run(trace_ids, run_id)
        for trace_id in trace_ids:
            for key, value in tags.items():
                self.client.set_trace_tag(trace_id, key, value)
        return trace_ids
