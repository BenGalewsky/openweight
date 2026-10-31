"""Drivers for the coding harnesses under evaluation."""

from openweight.harnesses.base import Harness, HarnessError, StepResult
from openweight.harnesses.claude import ClaudeHarness
from openweight.harnesses.opencode import OpenCodeHarness


def get(name: str) -> Harness:
    """The driver for harness ``name``."""
    if name == "claude":
        return ClaudeHarness()
    if name == "opencode":
        return OpenCodeHarness()
    raise ValueError(f"unknown harness {name!r}")


__all__ = ["ClaudeHarness", "Harness", "HarnessError", "OpenCodeHarness", "StepResult", "get"]
