"""Turn a PDF (or the Article 104 knowledge file) into clean text for the model.

pdfplumber is preferred for its table detection; pypdf is the text-only
fallback. Everything here is deterministic string wrangling -- no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

import diagnostics as diag

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

ARTICLE_FILES = {"ka": "article_104.txt", "en": "article_104_en.txt"}


@dataclass
class Page:
    number: int
    text: str


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


def dedupe(page):
    """Undo fake-bold character doubling.

    Some PDF renderers (including the markdown-to-PDF pipeline that produced the
    benchmark contract) fake bold by drawing each glyph twice at a sub-point
    offset. Extractors faithfully return both copies, so bold text arrives as
    `NNeexxuuss AAnnaallyyttiiccss` -- and it is the *values* that are bold:
    party names, the EIN, the CIK, the effective date, and the stated total
    `$150,000.00`. A model cannot read a number that reads `$$115500,,000000..0000`.

    tolerance=1 is load-bearing. At 2 it also collapses legitimately repeated
    characters -- "SOC 2 Type II" becomes "SOC 2 Type I".
    """
    try:
        return page.dedupe_chars(tolerance=1)
    except Exception:
        return page


def render_table(rows: list[list[str | None]]) -> str:
    """Render an extracted table as markdown.

    pypdf flattens a table into reading order, which interleaves headers and cells
    into columns that no longer line up -- that is how the fee table in the
    benchmark contract lost its totals. A markdown grid keeps each cell attached
    to its column.
    """
    grid = [[(cell or "").replace("\n", " ").strip() for cell in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    header, *body = grid
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def extract(path: Path, page_spec: str | None = None) -> list[Page]:
    """Extract contract text, with any tables re-rendered as markdown.

    Uses pdfplumber when available for its table detection, falling back to pypdf
    for text only. The fallback still works -- it just leaves tables mangled, which
    is what put the contract value out of reach in the benchmark run.
    """
    try:
        import pdfplumber
    except ImportError:
        diag.warn(
            "  note: pdfplumber not installed -- tables will reach the model as\n"
            "        interleaved columns. `pip install pdfplumber` to fix."
        )
        return extract_pypdf(path, page_spec)

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        wanted = parse_page_range(page_spec, total) if page_spec else None
        for index, page in enumerate(pdf.pages, start=1):
            if wanted is not None and index not in wanted:
                continue
            page = dedupe(page)
            body = clean(page.extract_text() or "")
            tables = [render_table(t) for t in (page.extract_tables() or [])]
            tables = [t for t in tables if t]
            if tables:
                body += "\n\nTables on this page, re-rendered:\n\n" + "\n\n".join(tables)
            if body:
                pages.append(Page(number=index, text=body))

    if not pages:
        raise SystemExit(no_text_message(path))
    return pages


def extract_pypdf(path: Path, page_spec: str | None = None) -> list[Page]:
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
        raise SystemExit(no_text_message(path))
    return pages


def no_text_message(path: Path) -> str:
    return (
        f"error: no extractable text in {path.name}.\n"
        "The pages are probably scanned images. Neither pdfplumber nor pypdf OCRs --\n"
        "they only read embedded text. Options: run OCRmyPDF over the file first, or\n"
        "feed the page images to qwen3.5:4b directly (it has vision capability)."
    )


def strip_comments(text: str) -> str:
    """Drop '#' banner lines. article_104_en.txt carries a review-status banner
    that is for humans and must never reach the model."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def load_text_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "\n\n".join(p.text for p in extract_pypdf(path))
    if suffix in {".txt", ".md"}:
        return clean(strip_comments(path.read_text(encoding="utf-8-sig")))
    raise SystemExit(f"error: unsupported file type: {path.name} (use .pdf, .txt, or .md)")


def find_article104(explicit: Path | None, lang: str) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"error: no such file: {explicit}")
        return explicit

    stem = Path(ARTICLE_FILES[lang]).stem
    for suffix in (".txt", ".md", ".pdf"):
        candidate = KNOWLEDGE_DIR / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate

    raise SystemExit(
        f"error: Article 104 ({lang}) not found.\n"
        f"Expected {KNOWLEDGE_DIR / ARTICLE_FILES[lang]}\n"
        "or pass an explicit path with --article104."
    )


def warn_if_unreviewed(path: Path) -> None:
    head = path.read_text(encoding="utf-8-sig")[:600]
    if "UNREVIEWED" in head:
        diag.warn(
            "\n  WARNING: "
            f"{path.name} is an UNREVIEWED machine translation of a tax statute.\n"
            "  Every citation derived from it is unverified until a Georgian-speaking\n"
            "  tax lawyer checks it against article_104.txt and updates the banner.\n"
            "  Use --article-lang ka to reason over the authoritative Georgian text.\n"
        )
