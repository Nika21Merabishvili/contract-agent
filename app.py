"""A minimal local web UI in front of the existing contract pipeline.

The terminal tool is two steps -- `pdf_analyze.py` (PDF -> assembled JSON) and
`export_excel.py` (JSON -> one-sheet .xlsx). This wraps the very same code in a
single-page Flask app so a non-technical user can do it in a browser: upload one
or more contract PDFs, wait, and get back one Excel workbook -- one header row
plus one data row per contract that could be analysed, in upload order.

It is a thin wrapper, not a second implementation. Each contract is analysed by
`pipeline.analyze_many`, which loops the same `pipeline.analyze` a single-file
run uses -- a fresh model context per contract, never several contracts in one
prompt. The workbook is `export_excel.build_workbook`, the same sheet-building
code the CLI uses. This file only moves bytes: save each upload, run the batch,
stream the result back, delete the temp files. The terminal workflow is
untouched and keeps working exactly as before.

Local and single-user by design: it binds to 127.0.0.1, talks only to the local
Ollama, and has no cloud, no API keys, no database, no job queue -- a batch is
just a loop within one request. It is threaded (see main()) only so the Stop
button's /cancel request can reach the server while a batch is still running;
_analysis_lock below keeps it to one batch at a time regardless.

    python app.py            # starts the server and prints the URL
"""

from __future__ import annotations

import base64
import io
import sys
import threading
import traceback
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from errors import Cancelled
from export_excel import build_workbook
from pipeline import analyze_many

# Georgian filenames and diagnostics must not raise on a cp1252 Windows console;
# reconfigure the same way the CLI entry points do.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# A batch can be many contracts; this only exists to reject an accidental huge
# upload before it is read into memory. Raising it is safe if ever needed.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Cleared at the start of every /analyze and polled inside the streaming
# Ollama call (see ollama_client.ask), so the Stop button -- which just sets
# this -- takes effect within about one token rather than waiting for a whole
# contract, or the whole batch, to finish.
_cancel_event = threading.Event()

# One flag shared by every request only makes sense if there is ever just one
# analysis in flight. This turns a second /analyze (a double click, a second
# tab) into a clean 409 instead of two batches quietly racing on the one
# _cancel_event above.
_analysis_lock = threading.Lock()


def _fail(message: str, status: int):
    """A readable error for the page: JSON the frontend renders as a status line,
    never a stack trace or a bare 500."""
    return jsonify(error=message), status


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    return _fail(f"That upload is too large in total (limit {limit_mb} MB).", 413)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/analyze")
def analyze_contract():
    """Acquire _analysis_lock and delegate to _run_analysis; see its docstring
    for the actual request/response contract.

    Kept separate from _run_analysis so the lock is released on every exit --
    every early return, the success path, and any exception -- via one `finally`
    here rather than one at the end of each of that function's several returns.
    A busy lock means a batch is already running: reported as 409, not queued,
    since queuing behind a multi-minute batch would just look like a hang.
    """
    if not _analysis_lock.acquire(blocking=False):
        return _fail("An analysis is already running. Wait for it to finish, or stop it.", 409)
    try:
        return _run_analysis()
    finally:
        _analysis_lock.release()


def _run_analysis():
    """Take one or more uploaded PDFs, analyse each with the real pipeline, and
    return one workbook built from every contract that could be analysed.

    The response is JSON on success (200): `succeeded` (names, in upload order),
    `failed` (name + reason for anything that could not be analysed -- possibly
    empty), `filename` (a suggested download name), and `workbook_base64` (the
    .xlsx bytes). The page decodes that back into a file, triggers the download,
    and renders `failed` as the batch report. A total failure -- nothing usable
    was uploaded, or none of it could be analysed -- returns the same plain
    `{"error": ...}` shape the original single-file version always used. If the
    Stop button cancels the batch, the response instead has `cancelled: true`
    (see the Cancelled handler below) and no workbook.
    """
    _cancel_event.clear()
    uploads = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploads:
        return _fail("No file was uploaded. Choose one or more contract PDFs and try again.", 400)

    with TemporaryDirectory(prefix="nxia_") as tmp:
        tmp_dir = Path(tmp)

        # One slot per upload, in upload order, so a rejection made here (wrong
        # extension, not really a PDF) and a failure raised later by the pipeline
        # both land in the same report without disturbing that order.
        names: list[str] = []
        good_paths: list[Path] = []
        precheck_failed: list[dict] = []

        for index, upload in enumerate(uploads):
            display_name = upload.filename
            safe_name = secure_filename(display_name) or f"contract_{index}.pdf"
            if not safe_name.lower().endswith(".pdf"):
                precheck_failed.append({"name": display_name, "error": "not a PDF"})
                continue

            dest = tmp_dir / safe_name
            if dest.exists():  # two uploads sanitised to the same temp filename
                dest = tmp_dir / f"{dest.stem}_{index}{dest.suffix}"
            upload.save(dest)

            # A quick magic-byte check catches a renamed / mislabelled file
            # before the model is ever started.
            with open(dest, "rb") as handle:
                is_pdf = handle.read(5) == b"%PDF-"
            if not is_pdf:
                precheck_failed.append({
                    "name": display_name,
                    "error": "not a readable PDF -- it may be renamed or corrupted",
                })
                continue

            good_paths.append(dest)
            names.append(display_name)

        if not good_paths:
            details = "; ".join(f"{f['name']} ({f['error']})" for f in precheck_failed)
            return _fail(f"No usable PDF was uploaded. {details}", 400)

        try:
            items = analyze_many(good_paths, cancel_event=_cancel_event)
        except Cancelled:
            # Same all-or-nothing semantics as Ctrl+C on the CLI (see
            # pipeline.analyze_many): contracts already finished in this batch
            # are discarded, not partially exported.
            return jsonify(cancelled=True, message="Analysis stopped. No file was downloaded.")
        except Exception:  # noqa: BLE001 -- last resort: never leak a stack trace to the page
            traceback.print_exc(file=sys.stderr)
            return _fail(
                "Analysis failed unexpectedly. Check that Ollama is running "
                "(`ollama serve`) with the model pulled, then try again. "
                "The server console has the details.",
                500,
            )

        # Pair each result back up with the original (pre-sanitising) filename the
        # user recognises -- analyze_many only ever saw the sanitised temp path.
        paired = list(zip(names, items))
        succeeded = [(name, item.result) for name, item in paired if item.ok]
        failed = precheck_failed + [
            {"name": name, "error": item.error} for name, item in paired if not item.ok
        ]

        if not succeeded:
            details = "; ".join(f"{f['name']} ({f['error']})" for f in failed)
            return _fail(f"None of the uploaded contracts could be analysed. {details}", 422)

        succeeded_names = [name for name, _ in succeeded]
        succeeded_results = [result for _, result in succeeded]

        # A source column only earns its place once there is more than one row to
        # tell apart -- a lone surviving contract gets the plain, familiar sheet.
        book = build_workbook(
            succeeded_results,
            sources=succeeded_names if len(succeeded) > 1 else None,
        )
        buffer = io.BytesIO()
        book.save(buffer)

    if len(succeeded) == 1:
        download_name = Path(succeeded_names[0]).with_suffix(".xlsx").name
    else:
        download_name = f"contracts_{len(succeeded)}_{date.today():%Y%m%d}.xlsx"

    return jsonify(
        succeeded=succeeded_names,
        failed=failed,
        filename=download_name,
        workbook_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
    )


@app.post("/cancel")
def cancel_analysis():
    """The Stop button. Sets _cancel_event, which the in-flight /analyze request
    (if any) is polling inside its current Ollama call (see ollama_client.ask)
    and will notice within about one streamed token.

    Always returns success, including when nothing is running -- it is just a
    flag, harmlessly left set until the next /analyze clears it. This route only
    exists to set that flag; it does not itself wait for the batch to stop.
    """
    _cancel_event.set()
    return jsonify(ok=True)


def main() -> None:
    host, port = "127.0.0.1", 5000
    print(f"nxia-contract-agent web UI  ->  http://{host}:{port}   (Ctrl+C to stop)")
    # threaded=True: a batch can run for minutes, and the Stop button's /cancel
    # request needs to reach the server while /analyze is still running -- with
    # the old threaded=False it would just queue behind it and never arrive in
    # time to matter. _analysis_lock (module level, above) keeps this from
    # turning into two concurrent batches; it is still a local, single-user tool.
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
