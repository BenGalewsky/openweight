"""What every harness driver provides, and the normalized result of one step."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openweight.branches import Evaluation
from openweight.sandbox import Sandbox

OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"


class HarnessError(RuntimeError):
    """The harness cannot run the evaluation, e.g. its model is not served."""


@dataclass(frozen=True)
class StepResult:
    """One agent session, normalized across harnesses.

    Token counts follow the providers' split: ``input_tokens`` excludes cached input, which is
    counted in ``cache_read_tokens`` (cache hits) and ``cache_write_tokens`` (cache writes).
    ``cost_usd`` is the harness's own (notional) estimate. Fields are None when the harness did
    not report them, e.g. after a timeout.
    """

    status: str
    exit_code: int
    session_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    num_turns: int | None = None
    tool_calls: int = 0
    # Epoch seconds at which a usage limit that stopped the session resets, if known.
    rate_limit_resets_at: float | None = None

    def metrics(self) -> dict[str, float]:
        names = (
            "cost_usd", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "num_turns", "tool_calls",
        )  # fmt: skip
        values = {name: getattr(self, name) for name in names}
        return {name: float(value) for name, value in values.items() if value is not None}


class Harness(Protocol):
    name: str
    # Environment variables holding this harness's model credentials, passed into the sandbox.
    credentials: tuple[str, ...]
    # Whether the harness writes its traces from inside the sandbox, which then needs the MLFLOW_*
    # variables. Harnesses that do not get no MLflow credentials there.
    traces_in_sandbox: bool
    # Whether `command` reads the prompt from stdin instead of taking it as an argument.
    prompt_on_stdin: bool

    def check(self, evaluation: Evaluation) -> None:
        """Raise `HarnessError` if the evaluation cannot run, e.g. its model is not served."""
        ...

    def budget(self, evaluation: Evaluation) -> float | None:
        """What is left of the provider's spend budget, in its own units, or None if the
        provider reports no budget (a subscription's usage limits are not one)."""
        ...

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        """The command that runs one non-interactive session in ``/workspace``, writing its JSON
        event stream to stdout. Harnesses that cannot choose a session id ignore ``session_id``
        and report their own in `StepResult`."""
        ...

    def finish(self, box: Sandbox, events: Path) -> None:
        """Run on the host after each session (also after a timeout), before `parse`: append to
        ``events`` what the harness keeps outside its event stream and write the traces its own
        tracing does not."""
        ...

    def parse(self, events: Path, exit_code: int) -> StepResult:
        """Normalize the event stream the command wrote to ``events``."""
        ...
