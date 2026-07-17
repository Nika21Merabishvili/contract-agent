"""Extract text from a PDF and analyse it with a local Qwen model via Ollama.

Usage:
    python pdf_analyze.py                 # prompts for a file
    python pdf_analyze.py report.pdf
    python pdf_analyze.py report.pdf --task "List every financial risk mentioned."
    python pdf_analyze.py report.pdf --pages 1-10 --think
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ollama import chat
from pypdf import PdfReader

MODEL = "qwen3.5:4b"

# The model can address 262144 tokens, but a context that large allocates a huge
# KV cache. Size num_ctx to the actual content instead, capped at CTX_MAX.
CTX_MAX = 32768
CTX_MIN = 4096
RESPONSE_HEADROOM = 1536

# Tokens of PDF text per map-step request. Well under CTX_MAX so the prompt
# scaffolding and answer always fit, and small enough that a 4B model stays
# accurate rather than getting lost mid-context.
CHUNK_TOKENS = 6000

DEFAULT_TASK = (
    "Summarise this document: its purpose, key claims, and any figures, dates, "
    "or commitments that matter. Note anything surprising or internally inconsistent."
)


@dataclass
class Page:
    number: int
    text: str


@dataclass
class Chunk:
    text: str
    first_page: int
    last_page: int

    @property
    def label(self) -> str:
        if self.first_page == self.last_page:
            return f"page {self.first_page}"
        return f"pages {self.first_page}-{self.last_page}"


def estimate_tokens(text: str) -> int:
    """Conservative character-per-token estimate; deliberately over-counts."""
    return len(text) // 3 + 1


def parse_page_range(spec: str, total: int) -> set[int]:
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            wanted.update(range(int(start), int(end) + 1))
        else:
            wanted.add(int(part))
    bad = sorted(p for p in wanted if p < 1 or p > total)
    if bad:
        shown = f"{bad[0]}-{bad[-1]}" if len(bad) > 1 else str(bad[0])
        raise SystemExit(
            f"error: requested page(s) {shown} but the document has {total} page(s)"
        )
    return wanted


def clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract(path: Path, page_spec: str | None) -> list[Page]:
    reader = PdfReader(path)

    if reader.is_encrypted:
        # An empty password opens PDFs that are encrypted but not password-locked.
        if reader.decrypt("") == 0:
            raise SystemExit(f"error: {path.name} is password-protected")

    total = len(reader.pages)
    wanted = parse_page_range(page_spec, total) if page_spec else None

    pages: list[Page] = []
    for index, page in enumerate(reader.pages, start=1):
        if wanted is not None and index not in wanted:
            continue
        body = clean(page.extract_text() or "")
        if body:
            pages.append(Page(number=index, text=body))

    if not pages:
        raise SystemExit(
            f"error: no extractable text in {path.name}.\n"
            "The pages are probably scanned images. pypdf only reads embedded text --\n"
            "it does not OCR. Options: run OCRmyPDF over the file first, or feed the\n"
            "page images to qwen3.5:4b directly (it has vision capability)."
        )

    return pages


def chunk_pages(pages: list[Page], budget: int) -> list[Chunk]:
    """Group whole pages into chunks under the token budget.

    Pages are kept intact so citations stay meaningful. A single page larger than
    the budget becomes its own oversized chunk rather than being split mid-sentence.
    """
    chunks: list[Chunk] = []
    buf: list[Page] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        if not buf:
            return
        body = "\n\n".join(f"[page {p.number}]\n{p.text}" for p in buf)
        chunks.append(Chunk(body, buf[0].number, buf[-1].number))
        buf, buf_tokens = [], 0

    for page in pages:
        tokens = estimate_tokens(page.text)
        if buf and buf_tokens + tokens > budget:
            flush()
        buf.append(page)
        buf_tokens += tokens

    flush()
    return chunks


class Cancelled(Exception):
    """Raised when the user interrupts generation with Ctrl+C."""


def show_prompt(prompt: str, label: str) -> None:
    rule = "=" * 72
    print(
        f"\n{rule}\n"
        f"PROMPT SENT TO MODEL -- {label} (~{estimate_tokens(prompt)} tokens)\n"
        f"{rule}\n{prompt}\n{rule}\n",
        file=sys.stderr,
    )


def ask(prompt: str, *, think: bool, label: str = "", show_input: bool = False) -> str:
    """One Ollama call with num_ctx sized to the prompt.

    Without an explicit num_ctx, Ollama defaults to 4096 and silently discards
    everything above it -- the document would be truncated with no error.

    Streams the response so Ctrl+C lands between tokens: a blocking call would
    keep generating on the server with no way to interrupt it.
    """
    needed = estimate_tokens(prompt) + RESPONSE_HEADROOM
    num_ctx = max(CTX_MIN, min(CTX_MAX, needed))

    if needed > CTX_MAX:
        print(
            f"  warning: prompt needs ~{needed} tokens, capped at {CTX_MAX}; "
            "content will be truncated",
            file=sys.stderr,
        )

    if show_input:
        show_prompt(prompt, label or "prompt")

    stream = chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        think=think,
        options={"num_ctx": num_ctx, "temperature": 0.3},
        stream=True,
    )

    parts: list[str] = []
    try:
        for part in stream:
            piece = part.message.content or ""
            if piece:
                parts.append(piece)
                print(piece, end="", flush=True)
    except KeyboardInterrupt:
        # Closing the generator drops the HTTP connection, which tells Ollama to
        # abandon the request rather than keep burning GPU on an answer nobody wants.
        stream.close()
        print(file=sys.stderr)
        raise Cancelled from None
    finally:
        print(flush=True)

    return "".join(parts).strip()


def analyse_chunk(
    chunk: Chunk,
    task: str,
    index: int,
    total: int,
    *,
    think: bool,
    show_input: bool = False,
) -> str:
    scope = (
        "This is the complete document."
        if total == 1
        else f"This is section {index} of {total} of a longer document ({chunk.label})."
    )
    prompt = (
        f"{scope}\n\n"
        "Work only from the text below. Do not use outside knowledge, and do not "
        "infer content from sections you have not been shown. If the text does not "
        "support an answer, say so.\n\n"
        f"Task: {task}\n\n"
        "Cite the page number in square brackets for each specific claim.\n\n"
        "--- BEGIN DOCUMENT TEXT ---\n"
        f"{chunk.text}\n"
        "--- END DOCUMENT TEXT ---"
    )
    return ask(prompt, think=think, label=chunk.label, show_input=show_input)


def synthesise(
    notes: list[str],
    chunks: list[Chunk],
    task: str,
    *,
    think: bool,
    show_input: bool = False,
) -> str:
    joined = "\n\n".join(
        f"--- NOTES ON {c.label.upper()} ---\n{n}" for c, n in zip(chunks, notes)
    )
    prompt = (
        "Below are notes taken from consecutive sections of one document, each "
        "analysed separately.\n\n"
        f"Original task: {task}\n\n"
        "Merge them into a single coherent answer to that task. Remove repetition, "
        "keep the page citations, and resolve or explicitly flag any contradictions "
        "between sections. Add nothing that is not in the notes.\n\n"
        f"{joined}"
    )
    return ask(prompt, think=think, label="merge step", show_input=show_input)


def pick_file_dialog() -> Path | None:
    """Open the OS file picker. Returns None if tkinter is unavailable or nothing chosen."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None

    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # otherwise the dialog opens behind the terminal
        chosen = filedialog.askopenfilename(
            title="Select a PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        root.destroy()
    except Exception:
        return None

    return Path(chosen) if chosen else None


def clean_path_input(raw: str) -> str:
    """Drag-and-drop and 'Copy as path' wrap the path in quotes; strip them."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw.strip()


def prompt_for_pdf() -> Path:
    """Ask for a PDF: file picker by default, typed or pasted path as fallback."""
    while True:
        try:
            answer = input(
                "\nSelect a PDF.\n"
                "  [Enter] open a file picker, or paste/drag a path here (q to quit): "
            )
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            raise SystemExit(130)
        answer = clean_path_input(answer)

        if answer.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)

        if not answer:
            path = pick_file_dialog()
            if path is None:
                print("  no file picker available here -- type a path instead", file=sys.stderr)
                continue
        else:
            path = Path(answer).expanduser()

        if not path.is_file():
            print(f"  no such file: {path}", file=sys.stderr)
            continue
        if path.suffix.lower() != ".pdf":
            print(f"  not a .pdf: {path.name}", file=sys.stderr)
            continue
        return path


def main() -> None:
    # The Windows console defaults to cp1252, which raises on any non-Latin-1
    # character in the PDF or the model's answer.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Analyse a PDF with a local Qwen model.")
    parser.add_argument("pdf", type=Path, nargs="?", help="path to the PDF; prompts if omitted")
    parser.add_argument("--task", default=DEFAULT_TASK, help="what to ask about the document")
    parser.add_argument("--pages", help="limit extraction, e.g. '1-10' or '2,5,9-12'")
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        default=CHUNK_TOKENS,
        help=f"PDF text tokens per request (default {CHUNK_TOKENS}); lower it if the "
        "model is slow or loses track on long sections",
    )
    parser.add_argument("--think", action="store_true", help="enable the model's reasoning mode")
    parser.add_argument("--dump-text", action="store_true", help="print extracted text and exit")
    parser.add_argument(
        "--show-input",
        action="store_true",
        help="print each full prompt (instructions + document text) before it is sent",
    )
    args = parser.parse_args()

    if args.pdf is None:
        args.pdf = prompt_for_pdf()
        if args.task == DEFAULT_TASK and not args.dump_text:
            try:
                asked = input("What should I look for? [Enter for a general summary]: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit(130)
            if asked:
                args.task = asked
    elif not args.pdf.is_file():
        raise SystemExit(f"error: no such file: {args.pdf}")

    pages = extract(args.pdf, args.pages)
    words = sum(len(p.text.split()) for p in pages)
    print(f"Extracted {len(pages)} page(s), ~{words} words from {args.pdf.name}", file=sys.stderr)

    if args.dump_text:
        for page in pages:
            print(f"\n=== page {page.number} ===\n{page.text}")
        return

    chunks = chunk_pages(pages, args.chunk_tokens)
    print("Press Ctrl+C to stop generation.", file=sys.stderr)

    try:
        if len(chunks) == 1:
            print("Analysing (single pass)...", file=sys.stderr)
            analyse_chunk(chunks[0], args.task, 1, 1, think=args.think, show_input=args.show_input)
            return

        print(f"Analysing in {len(chunks)} chunks, then merging...", file=sys.stderr)
        notes = []
        for i, chunk in enumerate(chunks, start=1):
            print(f"  [{i}/{len(chunks)}] {chunk.label}", file=sys.stderr)
            notes.append(
                analyse_chunk(
                    chunk, args.task, i, len(chunks), think=args.think, show_input=args.show_input
                )
            )

        print("  merging...", file=sys.stderr)
        synthesise(notes, chunks, args.task, think=args.think, show_input=args.show_input)
    except Cancelled:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
