"""A local web UI in front of the existing contract pipeline.

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

Analysis stays entirely local: it talks only to the local Ollama, with no cloud,
no API keys, no database, no job queue -- a batch is just a loop within one
request. The contract text never leaves the machine.

Security posture (see SECURITY.md): the app was hardened for a network-reachable
intranet deployment. Everything cross-cutting -- the local bcrypt login, session
auth, CSRF, security headers/CSP, upload validation limits, the audit log --
lives in `security.py`; this module wires those in and keeps the request
handlers readable. In production it is served by a real WSGI server behind a
TLS-terminating reverse proxy (see serve.py), not the dev server in main().
"""

from __future__ import annotations

import base64
import io
import secrets
import sys
import threading
import traceback
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import security as sec
from errors import Cancelled
from export_excel import build_workbook
from pipeline import analyze_many
from treaty_rates import refresh_all_treaty_rates

# Georgian filenames and diagnostics must not raise on a cp1252 Windows console;
# reconfigure the same way the CLI entry points do.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = Flask(__name__)
csrf = CSRFProtect(app)
# Sets SECRET_KEY (from the environment), cookie flags, MAX_CONTENT_LENGTH, the
# reverse-proxy trust, the CSP-nonce and security-header hooks, and opens the
# audit log. See security.install / security.Config.
sec.install(app, csrf)

# Cleared at the start of every /analyze and polled inside the streaming Ollama
# call (see ollama_client.ask), so the Stop button -- which just sets this --
# takes effect within about one token. The analysis watchdog (_run_analysis)
# reuses the same event to enforce a wall-clock timeout.
_cancel_event = threading.Event()

# One flag shared by every request only makes sense if there is ever just one
# analysis in flight. This turns a second /analyze into a clean 409 instead of
# two batches quietly racing on the one _cancel_event above.
_analysis_lock = threading.Lock()

# Allowed declared MIME types for an uploaded PDF. Extension and magic bytes are
# checked too (see _validate_upload); a declared type that is present but not a
# PDF type is rejected outright.
_PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}


# --------------------------------------------------------------------------- #
# Small response helpers
# --------------------------------------------------------------------------- #


def _fail(message: str, status: int):
    """A readable error for the page: JSON the frontend renders as a status line,
    never a stack trace or a bare 500."""
    return jsonify(error=message), status


def _wants_json() -> bool:
    """The upload/cancel endpoints speak JSON; the page routes serve HTML."""
    if request.path in {"/analyze", "/cancel"}:
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _error_page(message: str, status: int):
    """A generic error, negotiated: JSON for the API, a bare HTML page otherwise.

    No stack traces, no internal paths -- production error pages give the user a
    sentence and nothing an attacker can map the app with.
    """
    if _wants_json():
        return _fail(message, status)
    # Plain HTML, no inline script/style, so it needs no CSP nonce.
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<title>Error</title></head><body style='font-family:system-ui;max-width:40rem;"
        "margin:3rem auto;padding:0 1rem'><h1>Something went wrong</h1>"
        f"<p>{message}</p><p><a href='/'>Back to start</a></p></body></html>"
    )
    return body, status


def _safe_next(target: str | None) -> str:
    """A same-origin relative path to redirect to after login, or "/".

    Only a path beginning with a single "/" (and no backslash) is allowed, so a
    crafted ?next= cannot turn the login into an open redirect to another site.
    """
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# --------------------------------------------------------------------------- #
# Authentication routes
# --------------------------------------------------------------------------- #


@app.get("/login")
def login():
    if sec.current_user():
        return redirect(url_for("index"))
    return render_template("login.html", next=_safe_next(request.args.get("next")))


@app.post("/login")
def do_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = _safe_next(request.form.get("next"))
    ip = request.remote_addr or "unknown"

    locked = sec.throttle.locked(username, ip)
    if locked > 0:
        sec.audit("login", "locked_out", user=username or "-")
        return (
            render_template(
                "login.html",
                next=next_url,
                error=f"Too many failed attempts. Try again in about {int(locked // 60) + 1} minute(s).",
            ),
            429,
        )

    if username and password and sec.authenticate(username, password):
        sec.throttle.record_success(username, ip)
        # A fresh session id on privilege change defeats session fixation.
        session.clear()
        session["user"] = username
        session["epoch"] = sec.current_epoch(username)
        session.permanent = True
        sec.audit("login", "success", user=username)
        return redirect(next_url)

    sec.throttle.record_failure(username, ip)
    sec.audit("login", "failure", user=username or "-")
    # One message for both wrong-user and wrong-password: don't reveal which.
    return (
        render_template(
            "login.html", next=next_url, error="Invalid username or password."
        ),
        401,
    )


@app.post("/logout")
def logout():
    user = sec.current_user()
    if user:
        # Bump the epoch so the just-cleared cookie (or any other live session
        # for this user) can never be replayed, then drop this session.
        sec.bump_epoch(user)
        sec.audit("logout", "success", user=user)
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# The application
# --------------------------------------------------------------------------- #


@app.get("/")
@sec.login_required
def index():
    return render_template("index.html", user=g.current_user)


@app.post("/analyze")
@sec.login_required
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


def _count_pdf_pages(path: Path) -> int:
    """Page count via the same engines the extractor prefers (pdfplumber, then
    pypdf), so any PDF the pipeline can read yields a count here too. Raises if
    neither engine can open the file."""
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001 -- fall back to the text-only engine
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)


def _pages_within_timeout(path: Path, timeout: int) -> tuple[str, int | None]:
    """Count pages, but never let a crafted PDF hang the precheck.

    Runs the parse in a daemon thread and gives up after `timeout` seconds.
    Returns ("ok", n) / ("timeout", None) / ("error", None). A leaked thread on
    timeout is the pathological case only; the size cap already bounds the input.
    """
    result: dict[str, object] = {}

    def work() -> None:
        try:
            result["pages"] = _count_pdf_pages(path)
        except Exception as exc:  # noqa: BLE001 -- reported as ("error", None)
            result["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return "timeout", None
    if "error" in result:
        return "error", None
    return "ok", int(result["pages"])  # type: ignore[arg-type]


def _validate_upload(upload, index: int, tmp_dir: Path) -> tuple[Path | None, int, str | None]:
    """Validate one upload and, if it passes, save it under a random name.

    Returns (dest_path, size_bytes, error). Exactly one of dest_path / error is
    set. Validation is by extension, declared MIME type, actual file signature,
    per-file size, and page count -- extension alone is never trusted, and the
    client filename is never used to build the path.
    """
    display_name = upload.filename or ""
    if not display_name.lower().endswith(".pdf"):
        return None, 0, "not a PDF"
    if upload.mimetype and upload.mimetype not in _PDF_MIME_TYPES:
        return None, 0, "declared type is not PDF"

    # Never use the client-supplied name for the path: a fully random server-side
    # name closes path traversal (../../etc/passwd) and null-byte tricks outright.
    dest = tmp_dir / f"{secrets.token_hex(16)}.pdf"
    upload.save(dest)

    size = dest.stat().st_size
    if size == 0:
        return None, size, "empty file"
    if size > sec.Config.MAX_PDF_BYTES:
        limit_mb = sec.Config.MAX_PDF_BYTES // (1024 * 1024)
        return None, size, f"larger than the {limit_mb} MB per-file limit"

    with open(dest, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            return None, size, "not a readable PDF -- it may be renamed or corrupted"

    status, pages = _pages_within_timeout(dest, sec.Config.PDF_PARSE_TIMEOUT_S)
    if status == "timeout":
        return None, size, "could not be parsed in time (possibly malformed)"
    if status == "error":
        return None, size, "not a readable PDF -- it may be corrupted or encrypted"
    if pages is not None and pages > sec.Config.MAX_PAGES_PER_PDF:
        return None, size, f"too many pages ({pages}; limit {sec.Config.MAX_PAGES_PER_PDF})"

    return dest, size, None


def _run_analysis():
    """Take one or more uploaded PDFs, analyse each with the real pipeline, and
    return one workbook built from every contract that could be analysed.

    The response is JSON on success (200): `succeeded` (names, in upload order),
    `failed` (name + reason for anything that could not be analysed -- possibly
    empty), `ocr` (the subset of succeeded names whose text had to be OCR'd
    from a scanned PDF -- the page tells the user to verify those values),
    `filename` (a suggested download name), and `workbook_base64` (the
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

    if len(uploads) > sec.Config.MAX_FILES_PER_BATCH:
        sec.audit("upload", "rejected", files=len(uploads), reason="batch too large")
        return _fail(
            f"Too many files at once ({len(uploads)}). "
            f"Upload at most {sec.Config.MAX_FILES_PER_BATCH} contracts per batch.",
            413,
        )

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
            dest, size, error = _validate_upload(upload, index, tmp_dir)
            if error is not None:
                precheck_failed.append({"name": display_name, "error": error})
                sec.audit("upload", "rejected", file=display_name, reason=error)
                continue
            good_paths.append(dest)
            names.append(display_name)
            sec.audit("upload", "accepted", file=display_name, bytes=size)

        if not good_paths:
            details = "; ".join(f"{f['name']} ({f['error']})" for f in precheck_failed)
            return _fail(f"No usable PDF was uploaded. {details}", 400)

        sec.audit("analysis", "started", files=len(good_paths))
        refresh_all_treaty_rates()

        # Wall-clock guard: on expiry, trip the same cancel_event the Stop button
        # uses, so a stuck or adversarial model call cannot pin this worker
        # forever. Scaled by the number of contracts, since each is a full run.
        budget = len(good_paths) * sec.Config.ANALYSIS_TIMEOUT_PER_CONTRACT_S
        timed_out = threading.Event()

        def _trip_timeout() -> None:
            timed_out.set()
            _cancel_event.set()

        watchdog = threading.Timer(budget, _trip_timeout)
        watchdog.start()
        try:
            items = analyze_many(good_paths, cancel_event=_cancel_event)
        except Cancelled:
            if timed_out.is_set():
                sec.audit("analysis", "timeout", files=len(good_paths))
                return _fail(
                    "Analysis took too long and was stopped. Try fewer or smaller "
                    "contracts, or split the batch.",
                    504,
                )
            # Same all-or-nothing semantics as Ctrl+C on the CLI (see
            # pipeline.analyze_many): contracts already finished in this batch
            # are discarded, not partially exported.
            sec.audit("analysis", "cancelled", files=len(good_paths))
            return jsonify(cancelled=True, message="Analysis stopped. No file was downloaded.")
        except Exception:  # noqa: BLE001 -- last resort: never leak a stack trace to the page
            traceback.print_exc(file=sys.stderr)
            sec.audit("analysis", "error", files=len(good_paths))
            return _fail(
                "Analysis failed unexpectedly. Check that Ollama is running "
                "(`ollama serve`) with the model pulled, then try again. "
                "The server console has the details.",
                500,
            )
        finally:
            watchdog.cancel()

        # Pair each result back up with the original (pre-sanitising) filename the
        # user recognises -- analyze_many only ever saw the random temp path.
        paired = list(zip(names, items))
        succeeded = [(name, item.result) for name, item in paired if item.ok]
        failed = precheck_failed + [
            {"name": name, "error": item.error} for name, item in paired if not item.ok
        ]

        if not succeeded:
            sec.audit("analysis", "finished", succeeded=0, failed=len(failed))
            details = "; ".join(f"{f['name']} ({f['error']})" for f in failed)
            return _fail(f"None of the uploaded contracts could be analysed. {details}", 422)

        succeeded_names = [name for name, _ in succeeded]
        succeeded_results = [result for _, result in succeeded]

        # Contracts whose text came from OCR (scanned PDFs -- see
        # extraction.extract). The page lists them with a verify-the-values
        # note; the workbook marks the same rows via its own note column.
        ocr_names = [
            name for name, result in succeeded if result.get("_source") == "ocr"
        ]

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
    # The name is echoed into the response and used as the client-side download
    # filename; strip any path/control characters so nothing user-controlled can
    # steer where the browser writes or forge a log line.
    download_name = sec.safe_filename(download_name, fallback="contracts.xlsx")

    sec.audit("analysis", "finished", succeeded=len(succeeded), failed=len(failed))
    sec.audit("download", "delivered", filename=download_name, contracts=len(succeeded))

    return jsonify(
        succeeded=succeeded_names,
        failed=failed,
        ocr=ocr_names,
        filename=download_name,
        workbook_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
    )


@app.post("/cancel")
@sec.login_required
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


# --------------------------------------------------------------------------- #
# Error handlers -- generic pages, never a stack trace or internal path
# --------------------------------------------------------------------------- #


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    limit_mb = sec.Config.MAX_TOTAL_BYTES // (1024 * 1024)
    return _error_page(f"That upload is too large in total (limit {limit_mb} MB).", 413)


@app.errorhandler(CSRFError)
def _csrf_error(_exc):
    # Don't echo the CSRF library's reason; a generic message is enough.
    return _error_page("Your session token was missing or invalid. Reload the page and try again.", 400)


@app.errorhandler(404)
def _not_found(_exc):
    return _error_page("Not found.", 404)


@app.errorhandler(405)
def _method_not_allowed(_exc):
    return _error_page("Method not allowed.", 405)


@app.errorhandler(Exception)
def _unhandled(exc):
    # Let Flask's own handling deal with normal HTTP errors (404, redirects,
    # the RequestEntityTooLarge/CSRF handlers above); only genuine crashes fall
    # through to a generic 500 with the detail kept to the server console.
    if isinstance(exc, HTTPException):
        return exc
    traceback.print_exc(file=sys.stderr)
    sec.audit("request", "unhandled_error", path=request.path)
    return _error_page("Something went wrong. Please try again.", 500)


def main() -> None:
    host, port = "127.0.0.1", 5000
    print(
        "nxia-contract-agent web UI (DEVELOPMENT server)  ->  "
        f"http://{host}:{port}   (Ctrl+C to stop)\n"
        "  This is Flask's dev server -- do NOT expose it on the network.\n"
        "  For the intranet deployment use serve.py (waitress) behind a TLS proxy; see SECURITY.md."
    )
    # threaded=True: a batch can run for minutes, and the Stop button's /cancel
    # request needs to reach the server while /analyze is still running.
    # _analysis_lock (module level) keeps this from turning into two concurrent
    # batches. debug stays False -- Werkzeug's debugger is remote code execution.
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
