"""OCR fallback for scanned (image-only) contract PDFs.

`extraction.extract()` reads the embedded text layer; a scanned or
photographed contract has none. When that happens -- and only then --
extraction falls back to this module: each page is rendered to an image with
pypdfium2 (~300 DPI, grayscale) and read by Tesseract via pytesseract, with
both English and Georgian language data loaded, so the recovered text flows
through the existing pipeline unchanged.

Local only, like the rest of the project: Tesseract runs on this machine, no
cloud service, no API key. It is a *system* dependency (a binary plus language
packs, not a pip package -- see README.md), so everything here degrades to a
clear message naming what to install rather than a stack trace.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import diagnostics as diag
from extraction import Page, clean, parse_page_range

# Both languages, always: contracts here are English, Georgian, or mixed, and
# a Georgian page OCR'd with only `eng` loaded comes back as gibberish -- a
# silent failure the quality gate downstream may not even catch.
OCR_LANGS = ("eng", "kat")

# Scans are often stored at 150 DPI or lower; Tesseract's accuracy drops fast
# below ~300, so pages are rendered at 300 regardless of the source resolution.
RENDER_DPI = 300

# Where the Windows installer puts the binary when it is not on PATH.
WINDOWS_TESSERACT_DIRS = (
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR",
)

# Adding kat.traineddata to the system tessdata needs admin rights on Windows.
# This per-user directory is the no-admin alternative: put eng.traineddata and
# kat.traineddata here and OCR uses it automatically (via --tessdata-dir).
USER_TESSDATA = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "nxia-contract-agent"
    / "tessdata"
)

INSTALL_HINT = (
    "Install Tesseract with BOTH the English and Georgian language data:\n"
    "  Windows:       winget install UB-Mannheim.TesseractOCR\n"
    "                 then download kat.traineddata from\n"
    "                 https://github.com/tesseract-ocr/tessdata and copy it into\n"
    "                 C:\\Program Files\\Tesseract-OCR\\tessdata\n"
    f"                 (or, without admin rights, put eng+kat .traineddata in\n"
    f"                 {USER_TESSDATA})\n"
    "  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-kat\n"
    "  macOS:         brew install tesseract tesseract-lang"
)


class OcrUnavailable(RuntimeError):
    """OCR is needed but cannot run here; the message names the missing piece."""


def _load_pytesseract():
    """Import pytesseract and locate a runnable Tesseract with eng+kat loaded.

    Returns (pytesseract, config) where config is "" for a complete system
    install, or a --tessdata-dir override pointing at USER_TESSDATA when the
    Georgian pack lives there instead (see the comment on USER_TESSDATA).

    Every failure mode -- missing package, missing binary, missing language
    pack -- raises OcrUnavailable with instructions, because by the time this
    runs the PDF has already been found to have no text layer: the user's next
    step is always "install the missing piece", never "read a traceback".
    """
    try:
        import pytesseract
    except ImportError:
        raise OcrUnavailable(
            "the pytesseract package is not installed.\n"
            "  pip install pytesseract\n" + INSTALL_HINT
        ) from None

    if shutil.which("tesseract") is None:
        for candidate in WINDOWS_TESSERACT_DIRS:
            exe = candidate / "tesseract.exe"
            if exe.is_file():
                pytesseract.pytesseract.tesseract_cmd = str(exe)
                break
        else:
            raise OcrUnavailable(
                "the Tesseract OCR engine is not installed (or not on PATH).\n"
                + INSTALL_HINT
            )

    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # noqa: BLE001 -- any failure here means "not runnable"
        raise OcrUnavailable(
            f"Tesseract was found but could not be run: {exc}\n" + INSTALL_HINT
        ) from None

    missing = [lang for lang in OCR_LANGS if lang not in available]
    if not missing:
        return pytesseract, ""

    # The system tessdata is short a language; fall back to the per-user
    # directory if it holds every language OCR needs. Unquoted, forward
    # slashes: pytesseract passes config tokens to the tesseract binary
    # verbatim (quotes and backslash escapes are NOT interpreted), which is
    # also why USER_TESSDATA must not contain spaces -- LOCALAPPDATA never
    # does under a normal Windows profile.
    if all((USER_TESSDATA / f"{lang}.traineddata").is_file() for lang in OCR_LANGS):
        return pytesseract, f"--tessdata-dir {USER_TESSDATA.as_posix()}"

    raise OcrUnavailable(
        f"Tesseract is installed but missing language data: {', '.join(missing)}.\n"
        "Georgian contracts OCR as gibberish without the kat pack, so both are "
        "required.\n" + INSTALL_HINT
    )


def ocr_pdf(path: Path, page_spec: str | None = None) -> list[Page]:
    """Render each page at ~300 DPI grayscale and OCR it with eng+kat.

    Returns the same list[Page] shape the embedded-text extractor produces,
    with `ocr=True` on every page so the flag can ride the result all the way
    to the Excel sheet. The caller (extraction.extract) applies the quality
    gate; this function only transcribes.
    """
    pytesseract, config = _load_pytesseract()
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise OcrUnavailable(
            "the pypdfium2 package is not installed.\n  pip install pypdfium2"
        ) from None

    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as exc:  # noqa: BLE001 -- corrupt/locked file: same failure idiom as extraction
        raise SystemExit(f"error: {path.name} could not be opened for OCR ({exc})") from None

    try:
        total = len(pdf)
        wanted = parse_page_range(page_spec, total) if page_spec else None
        pages: list[Page] = []
        for index in range(1, total + 1):
            if wanted is not None and index not in wanted:
                continue
            diag.progress(f"  OCR: page {index}/{total}...")
            bitmap = pdf[index - 1].render(scale=RENDER_DPI / 72, grayscale=True)
            image = bitmap.to_pil()
            text = pytesseract.image_to_string(
                image, lang="+".join(OCR_LANGS), config=config
            )
            body = clean(text)
            if body:
                pages.append(Page(number=index, text=body, ocr=True))
        return pages
    finally:
        pdf.close()
