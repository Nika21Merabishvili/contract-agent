"""OCR fallback via a local vision model (glm-ocr), served by Ollama.

The default engine for scanned/image-only contract PDFs. Like ocr.py it turns a
page with no embedded text layer into a list[Page], but instead of Tesseract it
renders each page to an image and asks the glm-ocr model to transcribe it. A
vision model reads degraded scans, unusual fonts and mixed English/Georgian
pages more robustly than Tesseract's per-character matching, and it needs no
system binary or language packs -- only the model, pulled once.

Local only, like the rest of the project: Ollama serves glm-ocr on this machine,
no cloud service and no API key. ocr.py (Tesseract) is kept as an alternative
engine, selectable via extraction.extract(..., ocr_engine="tesseract").

Because glm-ocr is a model, not a fixed matcher, its output warrants the same
verify-these-values caution as any OCR path: Page.ocr=True rides every page.
"""

from __future__ import annotations

import io
from pathlib import Path

import diagnostics as diag
from extraction import Page, clean, parse_page_range

# The OcrUnavailable type is shared with ocr.py so extraction.extract catches a
# single exception regardless of which engine ran.
from ocr import OcrUnavailable

MODEL = "glm-ocr:bf16"

# Vision models resize internally and tolerate lower resolution than Tesseract,
# which needs ~300 DPI. 200 keeps the rendered image (and its token cost) small
# while staying comfortably readable.
RENDER_DPI = 200

# The image plus a full page of transcribed text has to fit the window; the
# Ollama default (~2k) would truncate a dense contract page. Deterministic
# sampling for the same reason ollama_client pins it -- OCR copies text, it does
# not compose it, so any temperature is pure risk.
NUM_CTX = 8192
OPTIONS = {"temperature": 0, "seed": 0, "num_ctx": NUM_CTX}

PROMPT = (
    "You are an OCR transcription engine. Transcribe every piece of text in this "
    "document image exactly as it appears. Preserve the original languages "
    "(English and/or Georgian), all numbers, punctuation and line breaks. Do not "
    "translate, summarise, correct, explain, or add any commentary or markdown -- "
    "output only the transcribed text of the page."
)

INSTALL_HINT = (
    f"Pull the model once, then make sure Ollama is running:\n"
    f"  ollama pull {MODEL}\n"
    "  ollama serve   (or launch the Ollama app)\n"
    "Or switch to the Tesseract engine with --ocr-engine tesseract."
)


def _make_client(host: str | None):
    """An ollama Client aimed at `host`, or the default (OLLAMA_HOST / localhost).

    An explicit Client is used rather than the module-level ollama.chat so the
    host can be chosen per call -- passing the Modal endpoint routes OCR to the
    GPU. host=None reproduces the default the rest of the pipeline uses: the
    OLLAMA_HOST env var if set (how run_modal.ps1 points app.py at Modal),
    otherwise local Ollama.
    """
    try:
        import ollama
    except ImportError:
        raise OcrUnavailable(
            "the ollama package is not installed.\n  pip install ollama\n" + INSTALL_HINT
        ) from None
    return ollama.Client(host=host)


def _check_available(client) -> None:
    """Fail early with instructions if Ollama or the glm-ocr model is missing.

    By the time this runs the PDF has already been found to have no text layer,
    so the user's next step is always "start Ollama" or "pull the model", never
    "read a traceback" -- every failure mode raises OcrUnavailable with the fix.
    """
    try:
        listing = client.list()
    except Exception as exc:  # noqa: BLE001 -- any failure here means "cannot reach Ollama"
        raise OcrUnavailable(
            f"Ollama is not reachable ({exc}).\n" + INSTALL_HINT
        ) from None

    names = {getattr(m, "model", None) or getattr(m, "name", "") for m in listing.models}
    if MODEL not in names:
        raise OcrUnavailable(
            f"the {MODEL} OCR model is not pulled.\n" + INSTALL_HINT
        )


def _page_to_png(page) -> bytes:
    """Render one pypdfium2 page to PNG bytes at RENDER_DPI, in colour."""
    bitmap = page.render(scale=RENDER_DPI / 72)
    image = bitmap.to_pil().convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def collapse_repetition(text: str, min_cycles: int = 3) -> str:
    """Undo a vision model's runaway repetition loop.

    Greedy decoding (temperature 0) on a sparse page can make glm-ocr transcribe
    the page correctly, then repeat that whole transcription over and over until
    it hits the token limit -- one observed page came back with the same four
    lines ~150 times, cut off mid-word at the end. When the line sequence is one
    short block repeated at least `min_cycles` times (the final cycle may be
    truncated mid-generation), keep a single copy.

    A repetition penalty would prevent this at the source, but it is deliberately
    NOT used: penalising recently-seen tokens corrupts verbatim copying -- an
    amount like `0.00 0.00 0.00` or a repeated digit is exactly what OCR must
    preserve (the same reason ollama_client keeps every penalty at zero). So the
    loop is removed after the fact instead. min_cycles=3 keeps legitimately
    repeated lines (a couple of identical table rows) intact.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    n = len(lines)
    if n < 2 * min_cycles:
        return text
    for p in range(1, n // min_cycles + 1):
        block = lines[:p]
        matched = 0
        while matched < n and lines[matched] == block[matched % p]:
            matched += 1
        # Generation can be cut off mid-line: allow the last, partial line to be
        # a prefix of whatever the cycle expected in that position.
        if matched == n - 1 and block[matched % p].startswith(lines[matched]):
            matched = n
        if matched == n and n / p >= min_cycles:
            return "\n".join(block)
    return text


def ocr_pdf_glm(
    path: Path, page_spec: str | None = None, *, host: str | None = None
) -> list[Page]:
    """Render each page to an image and transcribe it with the glm-ocr model.

    Returns the same list[Page] shape the embedded-text extractor produces, with
    `ocr=True` on every page so the flag rides the result to the Excel sheet. The
    caller (extraction.extract) applies the plausibility gate; this only
    transcribes.

    `host` selects which Ollama serves the model: None uses the default
    (OLLAMA_HOST env var, else local Ollama); pass the Modal endpoint to run OCR
    on the GPU instead of this machine.
    """
    client = _make_client(host)
    _check_available(client)

    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise OcrUnavailable(
            "the pypdfium2 package is not installed.\n  pip install pypdfium2"
        ) from None

    try:
        pdf = pdfium.PdfDocument(path)
    except Exception as exc:  # noqa: BLE001 -- corrupt/locked file: same idiom as ocr.py
        raise SystemExit(f"error: {path.name} could not be opened for OCR ({exc})") from None

    try:
        total = len(pdf)
        wanted = parse_page_range(page_spec, total) if page_spec else None
        pages: list[Page] = []
        for index in range(1, total + 1):
            if wanted is not None and index not in wanted:
                continue
            diag.progress(f"  OCR (glm-ocr): page {index}/{total}...")
            png = _page_to_png(pdf[index - 1])
            try:
                response = client.chat(
                    model=MODEL,
                    messages=[{"role": "user", "content": PROMPT, "images": [png]}],
                    options=OPTIONS,
                )
            except Exception as exc:  # noqa: BLE001 -- a mid-run Ollama failure is still "OCR unavailable"
                raise OcrUnavailable(
                    f"glm-ocr failed on page {index} ({exc}).\n" + INSTALL_HINT
                ) from None
            body = clean(collapse_repetition(response.message.content or ""))
            if body:
                pages.append(Page(number=index, text=body, ocr=True))
        return pages
    finally:
        pdf.close()
