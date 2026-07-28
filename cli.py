"""Command line: parse arguments, find the inputs, run the pipeline, print JSON.

The only thing this module writes to stdout is the final JSON object (or, in a
--dump mode, the requested text payload). Progress, telemetry and warnings all
go to stderr via `diagnostics`, so `... > out.json` yields clean JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import diagnostics as diag
from errors import AnalysisFailure, Cancelled, ModelError
from extraction import (
    KNOWLEDGE_DIR,
    extract,
    find_article104,
    load_text_document,
    warn_if_unreviewed,
)
from pipeline import analyse_contract
from treaty_rates import refresh_all_treaty_rates


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
            title="Select a contract PDF",
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
            # The prompt goes to stderr (not input()'s default stdout) so stdout
            # stays JSON-only even when a PDF is chosen interactively.
            diag.prompt(
                "\nSelect a contract PDF.\n"
                "  [Enter] open a file picker, or paste/drag a path here (q to quit): "
            )
            answer = input()
        except (EOFError, KeyboardInterrupt):
            diag.progress()
            raise SystemExit(130)
        answer = clean_path_input(answer)

        if answer.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)

        if not answer:
            path = pick_file_dialog()
            if path is None:
                diag.progress("  no file picker available here -- type a path instead")
                continue
        else:
            path = Path(answer).expanduser()

        if not path.is_file():
            diag.progress(f"  no such file: {path}")
            continue
        if path.suffix.lower() != ".pdf":
            diag.progress(f"  not a .pdf: {path.name}")
            continue
        return path


def main() -> None:
    # The Windows console defaults to cp1252, which raises on any non-Latin-1
    # character -- and most values in the output are Georgian.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Analyse a contract against Article 104 of the Georgian Tax Code; output JSON."
    )
    parser.add_argument(
        "pdfs", metavar="pdf", type=Path, nargs="*",
        help="contract PDF(s); several run as a batch (one analyse_many pass) and "
             "print one JSON array; prompts for one if omitted",
    )
    parser.add_argument(
        "--article104",
        type=Path,
        help=f"Article 104 text (.txt/.md/.pdf); default: search {KNOWLEDGE_DIR}",
    )
    parser.add_argument(
        "--article-lang",
        choices=("en", "ka"),
        default="en",
        help="statute language the model reasons over (default: en)",
    )
    parser.add_argument(
        "--out", type=Path, help="where to write the JSON (default: next to the contract PDF)"
    )
    parser.add_argument(
        "--xlsx",
        type=Path,
        nargs="?",
        const=Path("-"),
        help="also run step 2 and write the Excel workbook; optional path "
             "(default: next to the contract PDF). Same as piping into export_excel.py",
    )
    parser.add_argument("--pages", help="limit contract extraction, e.g. '1-10' or '2,5,9-12'")
    parser.add_argument("--think", action="store_true", help="enable the model's reasoning mode")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="stream the model's tokens and per-call context accounting to stderr",
    )
    parser.add_argument(
        "--dump-text", action="store_true", help="print extracted contract text and exit"
    )
    parser.add_argument(
        "--dump-article104",
        action="store_true",
        help="print the Article 104 text as the model will see it, then exit",
    )
    parser.add_argument(
        "--show-input", action="store_true", help="print each prompt before it is sent"
    )
    args = parser.parse_args()

    diag.set_verbose(args.verbose)

    article_path = find_article104(args.article104, args.article_lang)

    if args.dump_article104:
        text = load_text_document(article_path)
        diag.progress(f"=== {article_path} ===")
        print(text)
        if args.article_lang == "ka":
            diag.progress(
                "\nCheck the text above: it must be readable Georgian, not boxes or "
                f"Latin garbage.\nIf it is garbled, re-save Article 104 as a UTF-8 .txt "
                f"file in {KNOWLEDGE_DIR}."
            )
        return

    if not args.pdfs:
        args.pdfs = [prompt_for_pdf()]
    else:
        missing = [p for p in args.pdfs if not p.is_file()]
        if missing:
            raise SystemExit("error: no such file: " + ", ".join(str(p) for p in missing))

    if len(args.pdfs) > 1:
        run_batch(args)
        return

    pdf = args.pdfs[0]

    pages = extract(pdf, args.pages)
    words = sum(len(p.text.split()) for p in pages)
    diag.progress(f"Extracted {len(pages)} page(s), ~{words} words from {pdf.name}")

    if args.dump_text:
        for page in pages:
            print(f"\n=== page {page.number} ===\n{page.text}")
        return

    article_text = load_text_document(article_path)
    warn_if_unreviewed(article_path)
    diag.progress(f"Article 104 loaded from {article_path.name}")
    refresh_all_treaty_rates()
    diag.progress("Analysing contract... (Ctrl+C to stop)")

    try:
        result = analyse_contract(
            pages, article_text, think=args.think, show_input=args.show_input
        )
    except Cancelled:
        raise SystemExit(130)
    except AnalysisFailure as exc:
        raise SystemExit(
            f"\nerror: the model did not reach a tax verdict.\n  {exc}\n\n"
            "Not filling this in: a verdict the model failed to reach must not reach\n"
            "the Excel looking like an extracted fact. Re-run, or try --think."
        )
    except ModelError as exc:
        raise SystemExit(f"\nerror: {exc}")

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)

    if args.xlsx is not None:
        # Imported here, not at module scope: openpyxl is only needed by step 2,
        # and an analysis that already printed its JSON should not be reported as
        # a failed run just because the export dependency is missing.
        from export_excel import write_workbook

        xlsx_path = pdf.with_suffix(".xlsx") if args.xlsx == Path("-") else args.xlsx
        diag.progress(f"\nSaved: {write_workbook([result], xlsx_path)}")

    # Uncomment to also save the JSON to a file (next to the PDF, or at --out):
    # out_path = args.out or pdf.with_suffix(".json")
    # out_path.write_text(rendered + "\n", encoding="utf-8")
    # diag.progress(f"\nSaved: {out_path}")


def run_batch(args: argparse.Namespace) -> None:
    """Several PDFs on the command line: loop `pipeline.analyze_many` and print
    one JSON array, one assembled object per contract that succeeded.

    Reuses the exact function the web app's batch upload calls -- same
    "continue past a failure" behaviour -- so a bad contract in the middle of a
    long CLI batch does not cost the analysis already spent on the others.
    `--dump-text` has no batch meaning (which of N contracts would it dump?) and
    is refused rather than silently guessing.
    """
    if args.dump_text:
        raise SystemExit("error: --dump-text takes exactly one PDF at a time")
    if args.show_input:
        diag.warn("note: --show-input will print a prompt for every call, for every contract")

    from pipeline import analyze_many

    refresh_all_treaty_rates()
    diag.progress(f"Analysing {len(args.pdfs)} contracts... (Ctrl+C to stop)")
    try:
        items = analyze_many(
            args.pdfs,
            article_lang=args.article_lang,
            article104=args.article104,
            pages=args.pages,
            think=args.think,
        )
    except Cancelled:
        raise SystemExit(130)

    succeeded = [item for item in items if item.ok]
    failed = [item for item in items if not item.ok]
    for item in failed:
        diag.warn(f"  skipped {item.name}: {item.error}")

    if not succeeded:
        raise SystemExit("error: none of the given contracts could be analysed; see warnings above.")

    rendered = json.dumps([item.result for item in succeeded], ensure_ascii=False, indent=2)
    print(rendered)

    if args.xlsx is not None:
        from export_excel import write_workbook

        if args.xlsx == Path("-"):
            xlsx_path = Path(f"contracts_{len(succeeded)}.xlsx")
        else:
            xlsx_path = args.xlsx
        records = [item.result for item in succeeded]
        # A source column only earns its place once there is more than one row to
        # tell apart -- same rule the web app's batch upload uses.
        sources = [item.name for item in succeeded] if len(succeeded) > 1 else None
        diag.progress(f"\nSaved: {write_workbook(records, xlsx_path, sources=sources)}")

    if failed:
        diag.warn(
            f"\n{len(failed)} of {len(args.pdfs)} contract(s) were skipped -- see warnings above."
        )
