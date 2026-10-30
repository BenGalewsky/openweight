"""Drivers for the coding harnesses under evaluation."""

from openweight.harnesses.base import Harness, StepResult
from openweight.harnesses.claude import ClaudeHarness


def get(name: str) -> Harness:
    """The driver for harness ``name``."""
    if name == "claude":
        return ClaudeHarness()
    if name == "opencode":
        raise NotImplementedError("the OpenCode harness is milestone 5")
    raise ValueError(f"unknown harness {name!r}")


__all__ = ["ClaudeHarness", "Harness", "StepResult", "get"]
