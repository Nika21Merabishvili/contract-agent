"""A minimal local web UI in front of the existing contract pipeline.

The terminal tool is two steps -- `pdf_analyze.py` (PDF -> assembled JSON) and
`export_excel.py` (JSON -> one-sheet .xlsx). This wraps the very same code in a
single-page Flask app so a non-technical user can do it in a browser: upload one
contract PDF, wait, and get the .xlsx back as a download.

It is a thin wrapper, not a second implementation. The analysis is `pipeline.analyze`
(the same four model calls the CLI runs) and the workbook is `export_excel.build_workbook`
(the same sheet the CLI writes). This file only moves bytes: save the upload, call
those two functions, stream the result back, delete the temp file. The terminal
workflow is untouched and keeps working exactly as before.

Local and single-user by design: it binds to 127.0.0.1, talks only to the local
Ollama, and handles one request at a time. No cloud, no API keys, no database.

    python app.py            # starts the server and prints the URL
"""

from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import diagnostics as diag
from errors import AnalysisFailure, ModelError
from export_excel import build_workbook
from pipeline import analyze

# Georgian filenames and diagnostics must not raise on a cp1252 Windows console;
# reconfigure the same way the CLI entry points do.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Contracts are small; this only exists to reject an accidental huge upload before
# it is read into memory. Raising it is safe if a real contract ever needs it.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _fail(message: str, status: int):
    """A readable error for the page: JSON the frontend renders as a status line,
    never a stack trace or a bare 500."""
    return jsonify(error=message), status


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    return _fail(f"That file is too large (limit {limit_mb} MB).", 413)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/analyze")
def analyze_contract():
    """Take one uploaded PDF, run the real pipeline, return the .xlsx as a download."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _fail("No file was uploaded. Choose a contract PDF and try again.", 400)

    # secure_filename strips any path/oddities; keep a .pdf name for the temp file
    # and derive the download name from it, so messages and the result reference
    # something close to what the user picked.
    safe_name = secure_filename(upload.filename) or "contract.pdf"
    if not safe_name.lower().endswith(".pdf"):
        return _fail(f"“{upload.filename}” is not a PDF. Please upload a .pdf contract.", 400)

    with TemporaryDirectory(prefix="nxia_") as tmp:
        pdf_path = Path(tmp) / safe_name
        upload.save(pdf_path)

        # A quick magic-byte check catches a renamed / mislabelled file before the
        # model is ever started, so the user gets "not a PDF" instead of a crash.
        with open(pdf_path, "rb") as handle:
            if handle.read(5) != b"%PDF-":
                return _fail(
                    "That file is not a readable PDF -- it may be renamed or corrupted. "
                    "Please upload a real .pdf contract.",
                    400,
                )

        try:
            result = analyze(pdf_path)          # PDF -> assembled JSON (reused pipeline)
            book = build_workbook([result])     # JSON -> workbook (reused export logic)
            buffer = io.BytesIO()               # stream from memory; no temp .xlsx to clean up
            book.save(buffer)
            buffer.seek(0)
        except AnalysisFailure as exc:
            diag.progress(f"[web] analysis failure: {exc}")
            return _fail(
                "The model could not reach a tax verdict for this contract, so no result "
                "was produced. A verdict it failed to reach is left blank rather than "
                "guessed. Please try again -- a second run often succeeds.",
                422,
            )
        except SystemExit as exc:
            # The extractor raises SystemExit for a scanned/image-only PDF (no
            # extractable text), a password-protected file, or an over-long prompt.
            # Its message is already user-facing; drop the CLI-style "error: " prefix.
            message = str(exc.code) if exc.code else "The PDF could not be read."
            return _fail(message.removeprefix("error: "), 422)
        except ModelError as exc:
            diag.progress(f"[web] model error: {exc}")
            return _fail(
                "The local model did not return a usable answer. Please try again.", 422
            )
        except Exception:  # noqa: BLE001 -- last resort: never leak a stack trace to the page
            traceback.print_exc(file=sys.stderr)
            return _fail(
                "Analysis failed unexpectedly. Check that Ollama is running "
                "(`ollama serve`) with the model pulled, then try again. "
                "The server console has the details.",
                500,
            )

    download_name = Path(safe_name).with_suffix(".xlsx").name
    return send_file(
        buffer,
        mimetype=XLSX_MIME,
        as_attachment=True,
        download_name=download_name,
    )


def main() -> None:
    host, port = "127.0.0.1", 5000
    print(f"nxia-contract-agent web UI  ->  http://{host}:{port}   (Ctrl+C to stop)")
    # threaded=False: one request at a time, synchronously -- this is a single-user
    # local tool, and the analysis is the only long-running operation.
    app.run(host=host, port=port, threaded=False)


if __name__ == "__main__":
    main()
