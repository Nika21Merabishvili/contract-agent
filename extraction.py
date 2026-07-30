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
    # True when this page's text came from OCR rather than an embedded text
    # layer. Rides the result to the web page and the Excel sheet, because
    # OCR'd values -- names, tax IDs, amounts, dates -- warrant a human check.
    ocr: bool = False


# Below this many letters/digits across the whole document, the "text" is not
# a readable contract -- it is emptiness or specks (a scanned PDF sometimes
# carries a few stray characters of real text layer, e.g. a footer). Used both
# to decide that embedded extraction failed and to judge whether OCR output is
# usable, so a threshold change moves both gates together.
MIN_USABLE_CHARS = 200


def plausible_text(pages: list[Page]) -> bool:
    """Is this extraction believable as a contract, or garbage/nothing?

    Two checks: enough letters and digits to plausibly be a contract at all
    (MIN_USABLE_CHARS), and letters/digits making up at least half of the
    non-whitespace characters -- OCR of a blank or unreadable scan tends to
    produce sparse punctuation and lone characters, and that noise must fail
    here rather than flow onward and become a confident tax verdict.
    """
    stripped = "".join("".join(p.text.split()) for p in pages)
    if not stripped:
        return False
    alnum = sum(ch.isalnum() for ch in stripped)
    return alnum >= MIN_USABLE_CHARS and alnum / len(stripped) >= 0.5


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


def extract(
    path: Path,
    page_spec: str | None = None,
    *,
    use_ocr: bool = True,
    ocr_engine: str = "hybrid",
) -> list[Page]:
    """Extract contract text; OCR the page images if there is no text layer.

    The embedded text layer is always preferred -- when a PDF has one, it is
    exact and this behaves exactly as it always has, OCR never runs. Only when
    embedded extraction yields nothing (or implausibly little -- see
    plausible_text) does the scanned-document fallback kick in: the pages are
    rendered and transcribed, and the recovered text flows into the same
    pipeline. Pages that came through OCR are marked (Page.ocr) so the result
    can carry a verify-these-values flag.

    `ocr_engine` selects the transcriber:
      "hybrid" (default) -- glm-ocr for the page, with the Georgian portions
          corrected by Tesseract (ocr_hybrid.py); each script comes from the
          engine that reads it best.
      "glm"       -- the glm-ocr vision model alone (ocr_glm.py).
      "tesseract" -- the classic Tesseract engine alone, English + Georgian
          language data (ocr.py).
    All three produce the same list[Page] shape.

    `use_ocr=False` (the CLI's --no-ocr) skips the fallback for debugging; a
    scanned PDF then fails with the no-text message, as it did before OCR
    existed.
    """
    pages = extract_embedded(path, page_spec)
    if plausible_text(pages):
        return pages

    if not use_ocr:
        raise SystemExit(
            f"error: no extractable text in {path.name}, and OCR was disabled (--no-ocr).\n"
            "The pages are probably scanned images. Re-run without --no-ocr to OCR them."
        )

    # Imported here, not at module scope: the OCR backends (ollama / pypdfium2 /
    # pytesseract and the Tesseract binary) are only requirements when a scanned
    # PDF actually shows up, and both ocr modules import helpers from here, so a
    # top-level import would also be circular. OcrUnavailable is defined in
    # ocr.py and shared by both engines.
    from ocr import OcrUnavailable

    if ocr_engine == "tesseract":
        from ocr import ocr_pdf as run_ocr
        engine_label = "Tesseract, English+Georgian"
    elif ocr_engine == "glm":
        from ocr_glm import ocr_pdf_glm as run_ocr
        engine_label = "glm-ocr vision model"
    else:  # "hybrid" -- glm-ocr, Georgian corrected by Tesseract
        from ocr_hybrid import ocr_pdf_hybrid as run_ocr
        engine_label = "glm-ocr + Tesseract for Georgian"

    diag.warn(
        f"  {path.name} has no embedded text layer -- running OCR ({engine_label}).\n"
        "  This takes noticeably longer than a normal PDF."
    )
    try:
        ocr_pages = run_ocr(path, page_spec)
    except OcrUnavailable as exc:
        raise SystemExit(
            f"error: {path.name} has no embedded text, and OCR cannot run: {exc}"
        ) from None

    if not plausible_text(ocr_pages):
        raise SystemExit(no_text_message(path, after_ocr=True))

    diag.warn(
        "  OCR done. Values from this contract -- names, ID numbers, amounts,\n"
        "  dates -- were machine-read from images and should be verified against\n"
        "  the original document."
    )
    return ocr_pages


def extract_embedded(path: Path, page_spec: str | None = None) -> list[Page]:
    """The embedded-text-layer extraction, exactly as it always worked.

    Uses pdfplumber when available for its table detection, falling back to pypdf
    for text only. The fallback still works -- it just leaves tables mangled, which
    is what put the contract value out of reach in the benchmark run.

    Returns an empty list when there is no text layer; `extract` decides what
    that means (OCR fallback, or the no-text failure).
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
    return pages


def no_text_message(path: Path, *, after_ocr: bool = False) -> str:
    if after_ocr:
        return (
            f"error: no readable text in {path.name}, even after OCR.\n"
            "The scan may be blank, too low-resolution, or too poor to read.\n"
            "Values are never guessed from an unreadable contract, so this file\n"
            "is not analysed."
        )
    return (
        f"error: no extractable text in {path.name}.\n"
        "The pages are probably scanned images with no embedded text layer."
    )


def strip_comments(text: str) -> str:
    """Drop '#' banner lines. article_104_en.txt carries a review-status banner
    that is for humans and must never reach the model."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def load_text_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        # Knowledge files (Article 104) never go through OCR -- a scanned copy
        # of a statute should be replaced with a text one, not transcribed.
        pages = extract_pypdf(path)
        if not pages:
            raise SystemExit(no_text_message(path))
        return "\n\n".join(p.text for p in pages)
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
