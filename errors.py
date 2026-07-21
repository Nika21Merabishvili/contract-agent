"""Exceptions shared across the pipeline.

Kept in their own module so both the low-level Ollama client (which raises
Cancelled/ModelError) and the orchestration layer (which raises AnalysisFailure
and catches all three) can import them without a circular dependency.
"""

from __future__ import annotations


class Cancelled(Exception):
    """Raised when the user interrupts generation with Ctrl+C."""


class ModelError(Exception):
    """The model's answer was unusable after retries."""


class AnalysisFailure(ModelError):
    """The model failed to reach a tax verdict.

    Distinct from a contract being silent on a fact. A missing fact is data; a
    missing verdict is a failure, and must never be papered over with a filler
    value that reads like an extracted fact downstream.
    """
