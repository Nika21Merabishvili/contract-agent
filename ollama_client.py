"""The one place a prompt + schema becomes a validated JSON object.

All four calls go through `ask_json`: it sizes the context window to the prompt,
streams the answer, recovers the JSON object, parses it, runs a caller-supplied
validator, and retries up to three times with the complaint fed back in. Ollama
serves the request locally; there is no API key.
"""

from __future__ import annotations

import json
import re

from ollama import chat

import diagnostics as diag
from errors import Cancelled, ModelError

MODEL = "qwen3.5:4b"

# The model can address 262144 tokens, but a context that large allocates a huge
# KV cache -- and `ollama ps` shows this machine running the model ~97% on CPU,
# where that cache is pure latency. Size num_ctx to the actual content instead.
CTX_MAX = 32768
CTX_MIN = 4096

RESPONSE_HEADROOM = 2048

# Sampling. Three of these fight defaults that would otherwise corrupt extraction:
#   temperature      -- qwen3.5:4b ships with 1.0 (`ollama show`); noise on a
#                       task whose answers are copied out of a document.
#   presence_penalty -- ships with 1.5. Penalties are applied to logits *before*
#                       sampling, so this shifts the argmax even at temperature 0.
#                       On a task that copies names and addresses verbatim,
#                       penalising recently-seen tokens is entirely harmful.
#   top_k / top_p    -- irrelevant at temperature 0, pinned so a model default
#                       change cannot quietly reintroduce sampling.
SAMPLING = {
    "temperature": 0,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "repeat_penalty": 1.0,
    "top_k": 1,
    "top_p": 1.0,
    "seed": 0,
}

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """Token estimate calibrated against this model's tokenizer.

    Measured on qwen3.5:4b via prompt_eval_count: article_104.txt is 5,882 chars
    -> 2,953 tokens (~2.0 chars/token for Georgian), English ~4 chars/token. A flat
    chars//3 rule under-counts Georgian by ~1.5x, so the buckets are split.

    SAFETY_MARGIN exists because the split still under-counts: the spaces, digits
    and punctuation inside Georgian text fall outside the Georgian block and are
    charged at the cheaper Latin rate, which measured 5% low on the real statute.
    This number sizes num_ctx, so it must err high -- guessing low silently
    truncates the input, which is the failure mode this whole file exists to avoid.
    """
    SAFETY_MARGIN = 1.15
    georgian = sum(1 for ch in text if "Ⴀ" <= ch <= "ჿ")
    raw = georgian // 2 + (len(text) - georgian) // 3
    return int(raw * SAFETY_MARGIN) + 1


def show_prompt(label: str, prompt: str) -> None:
    rule = "=" * 72
    diag.progress(
        f"\n{rule}\nPROMPT [{label}] (~{estimate_tokens(prompt)} tokens)\n"
        f"{rule}\n{prompt}\n{rule}\n"
    )


def extract_json_object(raw: str) -> str:
    """Return the single top-level JSON object from a model reply.

    With `format=schema` the reply is already pure JSON, so for a well-formed
    answer this returns the object unchanged. It exists for robustness: if a
    model (a future one, or one run with --think) ever wraps its JSON in a
    <think> block or stray prose, the object is still recovered rather than
    breaking json.loads. Braces inside strings are ignored.
    """
    text = _THINK_BLOCK.sub("", raw).strip()
    start = text.find("{")
    if start == -1:
        return text  # no object at all; let json.loads raise a clear error

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return text[start:]  # unbalanced; let json.loads raise


def ask(prompt: str, schema: dict, *, label: str, think: bool, show_input: bool) -> str:
    """One Ollama call, with num_ctx sized to the prompt.

    Without an explicit num_ctx Ollama defaults to a small window and silently
    discards everything above it. num_ctx is logged alongside the server's own
    prompt_eval_count so truncation is visible rather than inferred. Every line
    of output here goes to stderr (see diagnostics) so stdout stays JSON-only.
    """
    needed = estimate_tokens(prompt) + RESPONSE_HEADROOM
    num_ctx = max(CTX_MIN, min(CTX_MAX, needed))

    if needed > CTX_MAX:
        raise SystemExit(
            f"error: the {label} prompt needs ~{needed} tokens, above the {CTX_MAX} "
            f"limit.\nUse --pages to send fewer contract pages, or raise CTX_MAX if "
            "your hardware allows it."
        )

    if show_input:
        show_prompt(label, prompt)

    stream = chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        think=think,
        format=schema,
        options={"num_ctx": num_ctx, **SAMPLING},
        stream=True,
    )

    parts: list[str] = []
    final = None
    try:
        for part in stream:
            piece = part.message.content or ""
            if piece:
                parts.append(piece)
                diag.detail(piece, end="")  # streamed tokens: verbose only
            thinking = getattr(part.message, "thinking", None)
            if thinking:
                diag.detail(thinking, end="")  # reasoning: verbose only, never captured
            final = part
    except KeyboardInterrupt:
        # Closing the generator drops the HTTP connection, which tells Ollama to
        # abandon the request rather than keep burning CPU on an unwanted answer.
        stream.close()
        diag.progress()
        raise Cancelled from None
    finally:
        diag.detail()  # terminate the streamed line (verbose only)

    used = getattr(final, "prompt_eval_count", None) if final else None
    if used:
        headroom = num_ctx - used
        if headroom < 256:
            # Truncation is a correctness problem, not routine telemetry -- always show it.
            diag.warn(
                f"  [{label}] prompt_eval_count={used}  num_ctx={num_ctx}  "
                f"headroom={headroom}  <-- NO HEADROOM, INPUT MAY BE TRUNCATED"
            )
        else:
            diag.detail(
                f"  [{label}] prompt_eval_count={used}  num_ctx={num_ctx}  headroom={headroom}"
            )

    return "".join(parts).strip()


def ask_json(
    prompt: str,
    schema: dict,
    validate,
    *,
    label: str,
    think: bool,
    show_input: bool,
    attempts: int = 3,
) -> dict:
    """Call the model, parse, validate, and retry with the complaint fed back.

    This is where every constraint Ollama's grammar cannot express is enforced --
    date formats above all, since `pattern` makes the server reject the request
    outright.
    """
    complaint = None
    for attempt in range(1, attempts + 1):
        text = prompt if complaint is None else (
            f"{prompt}\n\n--- YOUR PREVIOUS ANSWER WAS REJECTED ---\n{complaint}\n"
            "Return the whole object again, corrected."
        )
        raw = ask(text, schema, label=f"{label} #{attempt}", think=think, show_input=show_input)
        try:
            data = json.loads(extract_json_object(raw))
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
            validate(data)
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            complaint = str(exc)
            diag.warn(f"  [{label}] attempt {attempt} rejected: {complaint}")

    raise ModelError(f"{label}: no valid answer after {attempts} attempts. Last: {complaint}")
