"""What every harness driver provides, and the normalized result of one step."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openweight.branches import Evaluation

OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"


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

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        """The command that runs one non-interactive session in ``/workspace``, writing its JSON
        event stream to stdout. Harnesses that cannot choose a session id ignore ``session_id``
        and report their own in `StepResult`."""
        ...

    def parse(self, events: Path, exit_code: int) -> StepResult:
        """Normalize the event stream the command wrote to ``events``."""
        ...
