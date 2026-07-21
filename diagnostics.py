"""Where diagnostics go.

stdout is reserved for the program's real output -- the final JSON object (or a
--dump payload). Every progress line, telemetry number, streamed token, warning
and retry message goes to stderr instead, so that

    python pdf_analyze.py contract.pdf > out.json

leaves out.json holding nothing but valid JSON.

The noisiest streams -- the model's token-by-token output and the per-call
context accounting -- are gated behind --verbose, off by default, so an ordinary
run's terminal stays readable. They are not deleted, only quietened: pass
--verbose to bring them back for debugging. Correctness-critical warnings (an
input that may have been truncated) always print, verbose or not.
"""

from __future__ import annotations

import sys

_verbose = False


def set_verbose(on: bool) -> None:
    global _verbose
    _verbose = on


def is_verbose() -> bool:
    return _verbose


def progress(msg: str = "") -> None:
    """Concise progress/status -- always to stderr, never stdout."""
    print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """A warning the user should see regardless of verbosity. stderr."""
    print(msg, file=sys.stderr, flush=True)


def prompt(msg: str) -> None:
    """Write an interactive prompt to stderr with no trailing newline, so the
    input cursor sits after it and stdout stays reserved for JSON."""
    print(msg, end="", file=sys.stderr, flush=True)


def detail(msg: str = "", *, end: str = "\n") -> None:
    """Verbose-only diagnostics: streamed tokens and per-call telemetry.

    A no-op unless --verbose is set. Kept off stdout in every case.
    """
    if _verbose:
        print(msg, end=end, file=sys.stderr, flush=True)
