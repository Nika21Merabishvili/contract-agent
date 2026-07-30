"""Hybrid OCR: glm-ocr for the page, Tesseract for the Georgian parts.

The two OCR engines have opposite strengths. glm-ocr (ocr_glm) transcribes
English scans excellently but is weak on Georgian script; Tesseract (ocr) is the
reverse -- strong on Georgian, and fast and light enough to run on any machine.
This module takes glm-ocr as the base transcription and substitutes only the
Georgian portions with Tesseract's reading, so each script comes from the engine
that reads it best.

The merge aligns the two independent transcriptions word by word with difflib.
Where they agree -- the accurate English both engines produce -- the text is kept
as-is; those agreements also anchor the alignment. Where they differ, the region
is Georgian (glm misread it) or Latin (a Tesseract misread): Georgian is taken
from Tesseract, everything else from glm.

Tesseract only runs for pages where glm-ocr actually produced Georgian, so an
English-only scan never needs it and never pays for it. If Tesseract is
unavailable, glm-ocr's text is kept as-is with a warning rather than failing --
the Georgian just doesn't get corrected.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

import diagnostics as diag
from extraction import Page, clean

# Georgian Unicode: Asomtavruli + Mkhedruli (U+10A0-10FF), Extended (U+1C90-1CBF,
# the Mtavruli capitals), and Supplement (U+2D00-2D2F, Nuskhuri).
GEORGIAN_RE = re.compile(r"[Ⴀ-ჿᲐ-Ჿⴀ-⴯]")

# In a differing region, this share of Georgian letters flips the choice to
# Tesseract. Low, because any real Georgian in a region means glm misread it and
# Tesseract should win; a lone stray Georgian char from OCR noise stays below it.
GEORGIAN_THRESHOLD = 0.2


def has_georgian(text: str) -> bool:
    return bool(GEORGIAN_RE.search(text))


def _georgian_fraction(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if GEORGIAN_RE.match(ch)) / len(letters)


def _tokenize(text: str) -> list[str]:
    """Split into word tokens, keeping newlines as their own tokens so paragraph
    breaks survive the merge."""
    return re.findall(r"\n|\S+", text)


def _detokenize(tokens: list[str]) -> str:
    out = ""
    for tok in tokens:
        if tok == "\n":
            out = out.rstrip(" ") + "\n"
        else:
            if out and not out.endswith("\n"):
                out += " "
            out += tok
    return out


def merge_georgian(glm_text: str, tess_text: str) -> str:
    """Merge two OCR readings of one page: glm base, Georgian from Tesseract.

    Aligns the word/newline token streams; keeps agreements (mostly English),
    and for each differing region takes Tesseract when Georgian is involved,
    otherwise glm. `autojunk=False` so common tokens still anchor the alignment
    on a long page.
    """
    glm_toks = _tokenize(glm_text)
    tess_toks = _tokenize(tess_text)
    matcher = SequenceMatcher(None, glm_toks, tess_toks, autojunk=False)

    merged: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        glm_seg = glm_toks[i1:i2]
        tess_seg = tess_toks[j1:j2]
        if tag == "equal":
            merged.extend(glm_seg)
        elif (
            _georgian_fraction("".join(glm_seg)) >= GEORGIAN_THRESHOLD
            or _georgian_fraction("".join(tess_seg)) >= GEORGIAN_THRESHOLD
        ):
            merged.extend(tess_seg)  # Georgian region: Tesseract reads it better
        else:
            merged.extend(glm_seg)  # Latin difference: keep glm's stronger English
    return _detokenize(merged)


def _page_spec(numbers: list[int]) -> str:
    return ",".join(str(n) for n in numbers)


def ocr_pdf_hybrid(
    path: Path, page_spec: str | None = None, *, host: str | None = None
) -> list[Page]:
    """glm-ocr the whole document, then correct the Georgian with Tesseract.

    Returns the same list[Page] shape as the other engines, ocr=True on every
    page. `host` is forwarded to glm-ocr (pass the Modal endpoint to run it on the
    GPU); Tesseract always runs locally.
    """
    from ocr_glm import ocr_pdf_glm

    glm_pages = ocr_pdf_glm(path, page_spec, host=host)

    georgian_numbers = [p.number for p in glm_pages if has_georgian(p.text)]
    if not georgian_numbers:
        return glm_pages  # English-only scan -- glm is enough, Tesseract not needed

    # Tesseract only for the pages that actually have Georgian.
    from ocr import OcrUnavailable, ocr_pdf

    diag.progress(
        f"  hybrid OCR: correcting Georgian on {len(georgian_numbers)} page(s) with Tesseract..."
    )
    try:
        tess_pages = ocr_pdf(path, _page_spec(georgian_numbers))
    except OcrUnavailable as exc:
        diag.warn(
            "  hybrid OCR: Tesseract is unavailable, so glm-ocr's Georgian is kept\n"
            f"  as-is (it may be inaccurate): {exc}"
        )
        return glm_pages

    tess_by_number = {p.number: p.text for p in tess_pages}
    merged: list[Page] = []
    for gp in glm_pages:
        tess_text = tess_by_number.get(gp.number)
        if tess_text and has_georgian(gp.text):
            merged.append(Page(gp.number, clean(merge_georgian(gp.text, tess_text)), ocr=True))
        else:
            merged.append(gp)
    return merged
