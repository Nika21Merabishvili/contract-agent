"""Analyse a service contract against Article 104 of the Georgian Tax Code.

Extracts text from a contract PDF, pairs it with the Georgian text of Article
104 (kept in the knowledge/ folder), sends both to a local Qwen model via
Ollama, and prints the result as structured JSON. A downstream script turns
that JSON into an Excel workbook -- this program never formats output itself.
Saving the JSON to a file is currently disabled; uncomment the marked lines at
the end of main() to re-enable it (then --out chooses the destination).

Usage:
    python pdf_analyze.py                          # prompts for a contract PDF
    python pdf_analyze.py contract.pdf
    python pdf_analyze.py contract.pdf --article104 path/to/article104.txt
    python pdf_analyze.py --dump-article104        # verify Article 104 loads cleanly
"""

from __future__ import annotations

import argparse
import json
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

# The answer is a full JSON object whose values are Georgian sentences; Georgian
# script tokenises expensively, so reserve generous room for the response.
RESPONSE_HEADROOM = 4096

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

NOT_SPECIFIED = "არ არის მითითებული"

CONTRACT_FIELDS = [
    "party_a_name",
    "party_a_inn",
    "party_a_legal_form",
    "party_a_address",
    "party_a_role",
    "party_a_residency",
    "party_b_name",
    "party_b_inn",
    "party_b_legal_form",
    "party_b_address",
    "party_b_role",
    "party_b_residency",
    "service_type",
    "service_description",
    "contract_value",
    "payment_frequency",
    "currency",
    "payment_terms",
    "signing_date",
    "effective_date",
    "contract_duration",
    "end_date",
    "place_of_service",
]

TAX_FIELDS = [
    "is_georgian_source_income",
    "is_georgian_source_income_justification",
    "withholding_or_reverse_vat_obligation",
    "withholding_or_reverse_vat_explanation",
]

# Passed to Ollama as the `format` parameter: the server constrains decoding so
# the model is physically unable to answer with anything but JSON of this shape.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_data": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in CONTRACT_FIELDS},
            "required": CONTRACT_FIELDS,
        },
        "tax_analysis": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in TAX_FIELDS},
            "required": TAX_FIELDS,
        },
    },
    "required": ["contract_data", "tax_analysis"],
}

PROMPT_TEMPLATE = """You are a leading legal and tax analyst AI assistant specializing in Georgian tax law.

You are given two documents:
1. A service agreement/contract (usually in English).
2. Article 104 of the Tax Code of Georgia, in Georgian.

Your objective: (1) extract structured data from the contract, and (2) analyse that data against Article 104. Return everything as a single JSON object with two top-level objects, "contract_data" and "tax_analysis". A downstream program builds an Excel file from your JSON, so output data only -- no commentary, no Markdown.

STEP 1 -- EXTRACT CONTRACT DATA (the "contract_data" object)
Read the whole contract and fill in every field listed in the FIELD GUIDE below.
- Never leave a field empty. If a piece of information is not explicitly stated in the contract, use exactly this value: "{not_specified}".
- Proper nouns -- company names, individual names, addresses -- must be copied exactly as written in the contract. Never translate or transliterate them.
- Every other value must be written in natural, professional legal Georgian.
- Dates: DD/MM/YYYY format where possible.

FIELD GUIDE
Party A is the first party named in the contract; Party B is the second. For each party, fill six fields:
- "party_a_name" / "party_b_name": the party's full name, exactly as written in the contract.
- "party_a_inn" / "party_b_inn": the party's tax identification number. It may be labelled INN, TIN, tax ID, identification number, company number, or registration number. Copy it exactly as written.
- "party_a_legal_form" / "party_b_legal_form": the party's legal form of incorporation (LLC, Ltd, GmbH, JSC, individual entrepreneur, ...), written in Georgian, e.g. "შეზღუდული პასუხისმგებლობის საზოგადოება" or "ინდივიდუალური მეწარმე".
- "party_a_address" / "party_b_address": the party's registered/legal address, exactly as written.
- "party_a_role" / "party_b_role": "შემსრულებელი" for the party providing the service, "შემკვეთი" for the party ordering and paying for it.
- "party_a_residency" / "party_b_residency": residency relative to Georgia, with the country in brackets, e.g. "რეზიდენტი (საქართველო)" or "არარეზიდენტი (გერმანია)". If not stated outright, judge from the party's address or place of incorporation.

Then the terms of the deal:
- "service_type": the category of service -- see SERVICE TYPE below.
- "service_description": briefly describe the specific scope of work of this contract, in Georgian (a longer, contract-specific version of the service type).
- "contract_value": the total contract price as a plain number -- no currency symbols, no thousands separators.
- "payment_frequency": how often payment is made, e.g. "ერთჯერადი" (one-time), "ყოველთვიური" (monthly), "ეტაპობრივი" (milestone-based).
- "currency": ISO code of the contract currency (USD, EUR, GEL, ...).
- "payment_terms": when and under what conditions payment is due, e.g. "წინასწარი გადახდა" (advance payment) or "ინვოისის მიღებიდან 10 დღეში" (10 days after receiving the invoice).
- "signing_date": the date the contract was signed.
- "effective_date": the date the contract enters into force.
- "contract_duration": how long the contract remains in force, e.g. "12 თვე", "1 წელი", "უვადო" (indefinite). If only the effective and end dates are given, derive the duration from them.
- "end_date": the date the contract expires or is set to terminate.
- "place_of_service": where the service is performed or delivered.

SERVICE TYPE ("service_type"): match the contract against these categories and write the Georgian name given after the arrow:
- IT services (software development, support, hosting) -> "IT მომსახურება"
- Consulting services (business consulting, financial advice) -> "საკონსულტაციო მომსახურება"
- Marketing services (advertising campaigns, SMM, branding) -> "მარკეტინგული მომსახურება"
- Legal services (representation, document preparation) -> "იურიდიული მომსახურება"
- Logistics services (cargo transportation, warehousing) -> "ლოგისტიკური მომსახურება"
If none of these fit, describe the actual service found in the contract, in Georgian.

STEP 2 -- TAX ANALYSIS UNDER ARTICLE 104 (the "tax_analysis" object)
Using the data you extracted (especially the parties' residency, roles, service type, and place of service), analyse the contract against the Georgian text of Article 104 (income received from a source in Georgia) provided below. Work strictly from that text -- do not rely on outside knowledge of the Tax Code.
- "is_georgian_source_income": "დიახ" or "არა" -- does the income received under this contract count as income from a Georgian source under Article 104?
- "is_georgian_source_income_justification": justify the answer in professional Georgian, citing the exact clause/sub-clause of Article 104 (e.g. "104-ე მუხლის პირველი ნაწილის „გ“ ქვეპუნქტი").
- "withholding_or_reverse_vat_obligation": "დიახ", "არა", or "ნაწილობრივ" -- does a withholding tax obligation at the source of payment, or a reverse-charge VAT obligation, arise for either party?
- "withholding_or_reverse_vat_explanation": briefly explain the reasoning in professional Georgian, referencing the relevant provision where applicable.

--- BEGIN CONTRACT ---
{contract}
--- END CONTRACT ---

--- BEGIN ARTICLE 104, TAX CODE OF GEORGIA (GEORGIAN) ---
{article}
--- END ARTICLE 104 ---"""


@dataclass
class Page:
    number: int
    text: str


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


def extract(path: Path, page_spec: str | None = None) -> list[Page]:
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


def load_text_document(path: Path) -> str:
    """Load a .pdf, .txt, or .md document as one plain-text string."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "\n\n".join(p.text for p in extract(path))
    if suffix in {".txt", ".md"}:
        return clean(path.read_text(encoding="utf-8-sig"))
    raise SystemExit(f"error: unsupported file type: {path.name} (use .pdf, .txt, or .md)")


def find_article104(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"error: no such file: {explicit}")
        return explicit

    # Prefer plain text: PDF extraction of Georgian script sometimes produces
    # garbage cmaps, and a .txt file is checked once and trusted forever.
    for suffix in (".txt", ".md", ".pdf"):
        matches = sorted(KNOWLEDGE_DIR.glob(f"article_104*{suffix}"))
        if matches:
            return matches[0]

    raise SystemExit(
        "error: Article 104 not found.\n"
        f"Put the Georgian text of Article 104 in {KNOWLEDGE_DIR}\n"
        "named article_104.txt (preferred) or article_104.pdf,\n"
        "or pass an explicit path with --article104."
    )


class Cancelled(Exception):
    """Raised when the user interrupts generation with Ctrl+C."""


def show_prompt(prompt: str) -> None:
    rule = "=" * 72
    print(
        f"\n{rule}\n"
        f"PROMPT SENT TO MODEL (~{estimate_tokens(prompt)} tokens)\n"
        f"{rule}\n{prompt}\n{rule}\n",
        file=sys.stderr,
    )


def ask(prompt: str, *, think: bool, show_input: bool = False) -> str:
    """One Ollama call with num_ctx sized to the prompt.

    Without an explicit num_ctx, Ollama defaults to 4096 and silently discards
    everything above it -- the documents would be truncated with no error.

    Streams the response so Ctrl+C lands between tokens: a blocking call would
    keep generating on the server with no way to interrupt it. Progress goes to
    stderr; stdout is reserved for the final JSON.
    """
    needed = estimate_tokens(prompt) + RESPONSE_HEADROOM
    num_ctx = max(CTX_MIN, min(CTX_MAX, needed))

    if needed > CTX_MAX:
        raise SystemExit(
            f"error: contract + Article 104 need ~{needed} tokens, above the "
            f"{CTX_MAX} limit.\nUse --pages to send only the relevant contract "
            "pages, or raise CTX_MAX if your hardware allows it."
        )

    if show_input:
        show_prompt(prompt)

    stream = chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        think=think,
        format=OUTPUT_SCHEMA,
        options={"num_ctx": num_ctx, "temperature": 0},
        stream=True,
    )

    parts: list[str] = []
    try:
        for part in stream:
            piece = part.message.content or ""
            if piece:
                parts.append(piece)
                print(piece, end="", flush=True, file=sys.stderr)
    except KeyboardInterrupt:
        # Closing the generator drops the HTTP connection, which tells Ollama to
        # abandon the request rather than keep burning GPU on an answer nobody wants.
        stream.close()
        print(file=sys.stderr)
        raise Cancelled from None
    finally:
        print(flush=True, file=sys.stderr)

    return "".join(parts).strip()


def parse_result(raw: str) -> dict:
    # The format schema makes fences unlikely, but strip them defensively in
    # case an older Ollama server ignores the parameter.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"error: model returned invalid JSON ({exc}).\n"
            "Update Ollama if this persists -- structured outputs (the `format` "
            "parameter) need Ollama 0.5.0 or newer.\n\n"
            f"Raw response:\n{raw}"
        )
    if not isinstance(data, dict):
        raise SystemExit(f"error: model returned {type(data).__name__}, expected a JSON object")
    return data


def fill_missing(data: dict) -> dict:
    """Guarantee every schema field exists so the Excel step never hits a KeyError."""
    for section, fields in (("contract_data", CONTRACT_FIELDS), ("tax_analysis", TAX_FIELDS)):
        block = data.setdefault(section, {})
        for field in fields:
            if not str(block.get(field, "") or "").strip():
                block[field] = NOT_SPECIFIED
                print(
                    f'  note: model left {section}.{field} empty; filled with "{NOT_SPECIFIED}"',
                    file=sys.stderr,
                )
    return data


def analyse_contract(
    pages: list[Page], article_text: str, *, think: bool, show_input: bool = False
) -> dict:
    contract_text = "\n\n".join(f"[page {p.number}]\n{p.text}" for p in pages)
    prompt = PROMPT_TEMPLATE.format(
        not_specified=NOT_SPECIFIED, contract=contract_text, article=article_text
    )
    raw = ask(prompt, think=think, show_input=show_input)
    return fill_missing(parse_result(raw))


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
            answer = input(
                "\nSelect a contract PDF.\n"
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
    # character -- and every value in the output is Georgian.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Analyse a contract against Article 104 of the Georgian Tax Code; output JSON."
    )
    parser.add_argument("pdf", type=Path, nargs="?", help="contract PDF; prompts if omitted")
    parser.add_argument(
        "--article104",
        type=Path,
        help=f"Article 104 text (.txt/.md/.pdf); default: search {KNOWLEDGE_DIR}",
    )
    parser.add_argument(
        "--out", type=Path, help="where to write the JSON (default: next to the contract PDF)"
    )
    parser.add_argument("--pages", help="limit contract extraction, e.g. '1-10' or '2,5,9-12'")
    parser.add_argument("--think", action="store_true", help="enable the model's reasoning mode")
    parser.add_argument(
        "--dump-text", action="store_true", help="print extracted contract text and exit"
    )
    parser.add_argument(
        "--dump-article104",
        action="store_true",
        help="print the Article 104 text as the model will see it, then exit",
    )
    parser.add_argument(
        "--show-input",
        action="store_true",
        help="print the full prompt (instructions + both documents) before it is sent",
    )
    args = parser.parse_args()

    article_path = find_article104(args.article104)

    if args.dump_article104:
        text = load_text_document(article_path)
        print(f"=== {article_path} ===", file=sys.stderr)
        print(text)
        print(
            "\nCheck the text above: it must be readable Georgian, not boxes or "
            "Latin garbage.\nIf it is garbled, re-save Article 104 as a UTF-8 .txt "
            f"file in {KNOWLEDGE_DIR}.",
            file=sys.stderr,
        )
        return

    if args.pdf is None:
        args.pdf = prompt_for_pdf()
    elif not args.pdf.is_file():
        raise SystemExit(f"error: no such file: {args.pdf}")

    pages = extract(args.pdf, args.pages)
    words = sum(len(p.text.split()) for p in pages)
    print(f"Extracted {len(pages)} page(s), ~{words} words from {args.pdf.name}", file=sys.stderr)

    if args.dump_text:
        for page in pages:
            print(f"\n=== page {page.number} ===\n{page.text}")
        return

    article_text = load_text_document(article_path)
    print(f"Article 104 loaded from {article_path.name}", file=sys.stderr)
    print("Analysing contract... (Ctrl+C to stop)", file=sys.stderr)

    try:
        result = analyse_contract(
            pages, article_text, think=args.think, show_input=args.show_input
        )
    except Cancelled:
        raise SystemExit(130)

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)

    # Uncomment to also save the JSON to a file (next to the PDF, or at --out):
    # out_path = args.out or args.pdf.with_suffix(".json")
    # out_path.write_text(rendered + "\n", encoding="utf-8")
    # print(f"\nSaved: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
