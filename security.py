"""Security scaffolding for the network-reachable deployment of the web app.

`app.py` stays a thin wrapper around the analysis pipeline; everything the
InfoSec review adds -- configuration from the environment, the local user store,
login throttling/lockout, the session-auth decorator, per-request CSP nonces and
the rest of the security headers, and the audit log -- lives here so the request
handlers read as request handlers.

Deployment model (confirmed with the owner): this app is served on the company
intranet to multiple users, so authentication, authorization, transport
security and audit logging are all required. Identity is a *local* login: there
was no SSO/OIDC provider to integrate with, so passwords are stored here, hashed
with bcrypt. That user store is a liability the org must maintain and audit -- if
an identity provider becomes available later, prefer wiring this app's login to
it and deleting the local store.

Nothing in this module imports the analysis pipeline, and the pipeline does not
import this module: the analysis behaviour is untouched, and the CLI (cli.py)
keeps working without bcrypt/flask-wtf/waitress installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

import bcrypt
from flask import (
    current_app,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from dotenv import load_dotenv
load_dotenv()  # reads .env from the working directory into os.environ


# --------------------------------------------------------------------------- #
# Configuration -- everything tunable, sourced from the environment
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"error: environment variable {name} must be an integer, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Resolved configuration for one process, read once at import.

    Secrets and paths come from the environment so nothing sensitive is
    hardcoded or committed. Limits have defaults sized for a local, single-node
    intranet tool; every one of them can be raised or lowered without touching
    code.
    """

    # Distinguishes a production deployment (secret required, secure cookies,
    # HSTS) from a developer running the app on their own machine over http.
    # Never set this to "development" on the intranet.
    ENV = os.environ.get("NXIA_ENV", "production").strip().lower()
    IS_PRODUCTION = ENV not in {"development", "dev", "debug"}

    # Signs the session cookie. Must be stable across restarts (a new key logs
    # everyone out) and secret. Refused-to-start if absent in production; a
    # throwaway key is generated in development so `python app.py` still runs.
    SECRET_KEY = os.environ.get("NXIA_SECRET_KEY", "").strip()

    # The local user store: {username: {"pw_hash": ..., "epoch": int}}.
    USERS_FILE = Path(os.environ.get("NXIA_USERS_FILE", BASE_DIR / "instance" / "users.json"))

    # Audit log directory -- outside any static/served path on purpose.
    LOG_DIR = Path(os.environ.get("NXIA_LOG_DIR", BASE_DIR / "logs"))
    LOG_MAX_BYTES = _env_int("NXIA_LOG_MAX_BYTES", 5 * 1024 * 1024)
    LOG_BACKUPS = _env_int("NXIA_LOG_BACKUPS", 10)

    # Session lifetime; the cookie also expires client-side after this.
    SESSION_LIFETIME_MIN = _env_int("NXIA_SESSION_LIFETIME_MIN", 480)  # 8 hours

    # `Secure` cookie flag. Defaults on in production (the intranet deployment is
    # behind TLS) and off in development so a local http run still logs in. The
    # env var overrides either way -- never set it to 0 on the intranet, which
    # would leak the session cookie over http.
    COOKIE_SECURE = _env_bool("NXIA_COOKIE_SECURE", IS_PRODUCTION)

    # Emitted only when TLS terminates in front of us (the intranet case); off in
    # development, where there is no TLS to assert.
    HSTS_ENABLED = _env_bool("NXIA_HSTS", IS_PRODUCTION)
    HSTS_MAX_AGE = _env_int("NXIA_HSTS_MAX_AGE", 31536000)  # 1 year

    # Number of trusted proxy hops in front of the app (nginx terminating TLS =
    # 1). ProxyFix trusts exactly this many X-Forwarded-* entries, so the source
    # IP we log and the scheme we treat as secure are the client's, not spoofed.
    TRUSTED_PROXY_HOPS = _env_int("NXIA_TRUSTED_PROXY_HOPS", 1)

    # Upload limits. Each is an independent denial-of-service guard: one slow
    # model run per contract means an unbounded batch of large PDFs is expensive.
    MAX_FILES_PER_BATCH = _env_int("NXIA_MAX_FILES", 20)
    MAX_PDF_BYTES = _env_int("NXIA_MAX_PDF_BYTES", 25 * 1024 * 1024)  # per file
    MAX_TOTAL_BYTES = _env_int("NXIA_MAX_TOTAL_BYTES", 200 * 1024 * 1024)  # whole request
    MAX_PAGES_PER_PDF = _env_int("NXIA_MAX_PAGES", 100)

    # Bound how long parsing one uploaded PDF's page count may take, so a crafted
    # "PDF bomb" cannot hang the precheck. The full extraction+analysis run is
    # bounded separately by the analysis watchdog below.
    PDF_PARSE_TIMEOUT_S = _env_int("NXIA_PDF_PARSE_TIMEOUT_S", 20)

    # Wall-clock budget per contract for the analysis itself. The request handler
    # scales this by the number of contracts and, on expiry, trips the pipeline's
    # existing cancel_event -- the same mechanism the Stop button uses -- so an
    # adversarial or stuck model call cannot pin a worker forever.
    ANALYSIS_TIMEOUT_PER_CONTRACT_S = _env_int("NXIA_ANALYSIS_TIMEOUT_S", 600)  # 10 min

    # Login throttling: after MAX_LOGIN_FAILURES within the window, that username
    # (and, separately, that source IP) is locked out for LOCKOUT_SECONDS.
    MAX_LOGIN_FAILURES = _env_int("NXIA_MAX_LOGIN_FAILURES", 5)
    LOGIN_FAILURE_WINDOW_S = _env_int("NXIA_LOGIN_WINDOW_S", 900)  # 15 min
    LOCKOUT_SECONDS = _env_int("NXIA_LOCKOUT_SECONDS", 900)  # 15 min

    # Password policy for the local store (see manage_users.py). A floor, not the
    # whole policy -- align MIN_PASSWORD_LENGTH with the company standard.
    MIN_PASSWORD_LENGTH = _env_int("NXIA_MIN_PASSWORD_LENGTH", 12)

    # bcrypt work factor. 12 is a sensible default in 2025-era hardware terms.
    BCRYPT_ROUNDS = _env_int("NXIA_BCRYPT_ROUNDS", 12)


def resolve_secret_key() -> str:
    """The SECRET_KEY to run with, or die trying in production.

    In production a missing/short key is a hard error: a predictable key means
    forgeable session cookies. In development we mint an ephemeral key so the app
    still starts (every restart invalidates sessions, which is fine locally).
    """
    key = Config.SECRET_KEY
    if key and len(key) >= 32:
        return key
    if Config.IS_PRODUCTION:
        raise SystemExit(
            "error: NXIA_SECRET_KEY is not set (or is shorter than 32 chars).\n"
            "Generate one and put it in the environment before starting in production:\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            "See SECURITY.md. Refusing to start with a weak/absent session key."
        )
    # Development only.
    logging.getLogger("nxia.audit").warning(
        "NXIA_SECRET_KEY unset -- using an ephemeral development key; sessions "
        "will not survive a restart. Do NOT run this way on the network."
    )
    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------- #
# Audit log -- metadata about operations, never contract content
# --------------------------------------------------------------------------- #

_audit = logging.getLogger("nxia.audit")

# Newlines/control chars in a field (an attacker-controlled filename, say) would
# otherwise forge extra log lines. Collapse them before anything reaches a record.
_LOG_UNSAFE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")

# Path separators, Windows-illegal characters, and control chars. Stripped from
# any filename we echo back or use as a download name -- unlike werkzeug's
# secure_filename this keeps non-ASCII, so a Georgian contract name survives
# while `../`, backslashes and null bytes cannot.
_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def safe_filename(name: str, *, fallback: str) -> str:
    """A display/download filename with every path-traversal and control
    character neutralised, but Unicode (e.g. Georgian) preserved."""
    cleaned = _FILENAME_UNSAFE.sub("_", str(name)).strip().strip(".")
    return cleaned or fallback


def _scrub(value: object, limit: int = 256) -> str:
    text = str(value)
    text = _LOG_UNSAFE.sub(" ", text)
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


def init_audit_log() -> None:
    """Send the audit logger to a rotating file outside any served directory.

    Idempotent: safe to call once at startup. The file lives in LOG_DIR, which is
    never mapped to a URL, so the log is not web-accessible.
    """
    Config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = Config.LOG_DIR / "audit.log"

    already = any(
        isinstance(h, RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")) == logfile.resolve()
        for h in _audit.handlers
    )
    if not already:
        handler = RotatingFileHandler(
            logfile,
            maxBytes=Config.LOG_MAX_BYTES,
            backupCount=Config.LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        _audit.addHandler(handler)
    _audit.setLevel(logging.INFO)
    # Don't let audit records bubble to the root logger / stderr, where they
    # could interleave with the pipeline's diagnostics.
    _audit.propagate = False


def _client_ip() -> str:
    # request.remote_addr is the real client only because ProxyFix has already
    # applied the trusted X-Forwarded-For hops (see install()).
    return request.remote_addr or "unknown"


def audit(action: str, outcome: str, *, user: str | None = None, **fields: object) -> None:
    """Write one audit line: timestamp (via the formatter), user, source IP,
    action, outcome, plus scrubbed metadata.

    Contract content must never be passed here. Callers log *about* an operation
    -- counts, sizes, sanitised filenames -- not the data inside the documents.
    """
    who = user if user is not None else session.get("user", "-")
    parts = [
        f"action={_scrub(action)}",
        f"outcome={_scrub(outcome)}",
        f"user={_scrub(who)}",
        f"ip={_scrub(_client_ip())}",
    ]
    for key, value in fields.items():
        parts.append(f"{_scrub(key)}={_scrub(value)}")
    _audit.info(" ".join(parts))


# --------------------------------------------------------------------------- #
# Local user store -- bcrypt hashes on disk, loaded into memory
# --------------------------------------------------------------------------- #

_users_lock = threading.Lock()


def _read_users() -> dict:
    if not Config.USERS_FILE.exists():
        return {}
    try:
        data = json.loads(Config.USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"error: could not read the user store {Config.USERS_FILE}: {exc}")
    return data.get("users", {})


def _write_users(users: dict) -> None:
    Config.USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = Config.USERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": users}, indent=2), encoding="utf-8")
    tmp.replace(Config.USERS_FILE)
    # The store holds password hashes; keep it off group/other where the OS
    # supports it (a no-op on Windows, honoured on POSIX).
    try:
        os.chmod(Config.USERS_FILE, 0o600)
    except OSError:
        pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=Config.BCRYPT_ROUNDS)
    ).decode("ascii")


def verify_password(password: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pw_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def user_exists(username: str) -> bool:
    with _users_lock:
        return username in _read_users()


def list_users() -> list[str]:
    with _users_lock:
        return sorted(_read_users())


def set_user_password(username: str, password: str) -> None:
    """Create or update a user, and invalidate any of their live sessions.

    Bumping `epoch` (checked on every authenticated request) means a password
    change logs that user's other sessions out -- the standard reason to change
    a password being that one may be compromised.
    """
    with _users_lock:
        users = _read_users()
        record = users.get(username, {})
        record["pw_hash"] = hash_password(password)
        record["epoch"] = int(record.get("epoch", 0)) + 1
        users[username] = record
        _write_users(users)


def remove_user(username: str) -> bool:
    with _users_lock:
        users = _read_users()
        if username not in users:
            return False
        del users[username]
        _write_users(users)
        return True


def authenticate(username: str, password: str) -> bool:
    """Constant-ish-time credential check.

    On an unknown username we still run a bcrypt verify against a dummy hash so
    the response time does not reveal whether the username exists.
    """
    with _users_lock:
        record = _read_users().get(username)
    if record is None:
        verify_password(password, _DUMMY_HASH)  # equalise timing
        return False
    return verify_password(password, record.get("pw_hash", ""))


def current_epoch(username: str) -> int | None:
    with _users_lock:
        record = _read_users().get(username)
    return int(record["epoch"]) if record else None


def bump_epoch(username: str) -> None:
    """Invalidate every existing session for a user (used on logout)."""
    with _users_lock:
        users = _read_users()
        record = users.get(username)
        if record is None:
            return
        record["epoch"] = int(record.get("epoch", 0)) + 1
        _write_users(users)


# A precomputed hash of a random string: the target of the dummy verify above.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(rounds=4)).decode("ascii")


# --------------------------------------------------------------------------- #
# Login throttling / lockout -- in-memory, per username and per source IP
# --------------------------------------------------------------------------- #


class LoginThrottle:
    """Sliding-window failure counter with temporary lockout.

    State is in process memory: correct for this single-node app, and it fails
    safe -- a restart clears counters, which only ever *grants* another attempt,
    never bypasses a password. Both the username and the source IP are tracked,
    so neither password-spraying one account nor spreading attempts across
    usernames from one host slips the limit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _keys(self, username: str, ip: str) -> tuple[str, str]:
        return f"user:{username.lower()}", f"ip:{ip}"

    def locked(self, username: str, ip: str) -> float:
        """Seconds remaining on any active lockout for this user or IP (0 if none)."""
        now = time.monotonic()
        with self._lock:
            remaining = 0.0
            for key in self._keys(username, ip):
                until = self._locked_until.get(key, 0.0)
                remaining = max(remaining, until - now)
            return max(0.0, remaining)

    def record_failure(self, username: str, ip: str) -> None:
        now = time.monotonic()
        window = Config.LOGIN_FAILURE_WINDOW_S
        with self._lock:
            for key in self._keys(username, ip):
                hits = [t for t in self._failures.get(key, []) if now - t < window]
                hits.append(now)
                self._failures[key] = hits
                if len(hits) >= Config.MAX_LOGIN_FAILURES:
                    self._locked_until[key] = now + Config.LOCKOUT_SECONDS
                    self._failures[key] = []

    def record_success(self, username: str, ip: str) -> None:
        with self._lock:
            for key in self._keys(username, ip):
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)


throttle = LoginThrottle()


# --------------------------------------------------------------------------- #
# Session auth -- the decorator every non-public route wears
# --------------------------------------------------------------------------- #


def current_user() -> str | None:
    """The authenticated username for this request, or None.

    A session is valid only if its stored epoch still matches the user's epoch
    in the store; logout and password changes bump the epoch, so a stale cookie
    (or one replayed after logout) fails here even though its signature is good.
    """
    username = session.get("user")
    if not username:
        return None
    if session.get("epoch") != current_epoch(username):
        session.clear()
        return None
    return username


def _wants_json() -> bool:
    # The upload/cancel endpoints are called by fetch and render JSON; the page
    # routes are visited by the browser and should redirect to the login page.
    if request.path in {"/analyze", "/cancel"}:
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(view):
    """Gate a route behind an authenticated, non-stale session.

    Least privilege: applied to everything except the login page and static
    assets, so an unauthenticated request can reach nothing but the login form.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify(error="Your session has expired. Please sign in again."), 401
            return redirect(url_for("login", next=request.path))
        g.current_user = user
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------- #
# Security headers + per-request CSP nonce
# --------------------------------------------------------------------------- #


def _csp(nonce: str) -> str:
    """A restrictive Content-Security-Policy.

    The frontend is first-party HTML/CSS/JS only and talks to no third-party
    origin, so everything defaults to 'none' and only what the page actually
    uses is opened back up: same-origin fetch (connect-src), and the page's own
    inline <style>/<script>, each carrying this request's nonce so no *other*
    inline code can run. No 'unsafe-inline', no external hosts.
    """
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src 'nonce-{nonce}'",
            f"style-src 'nonce-{nonce}'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        ]
    )


def _assign_nonce() -> None:
    g.csp_nonce = secrets.token_urlsafe(16)


def _apply_security_headers(response):
    response.headers["Content-Security-Policy"] = _csp(getattr(g, "csp_nonce", ""))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # HSTS only makes sense over TLS; asserting it over http would be ignored by
    # browsers anyway, but we gate it so a dev http run stays clean.
    if Config.HSTS_ENABLED and (Config.IS_PRODUCTION or request.is_secure):
        response.headers["Strict-Transport-Security"] = (
            f"max-age={Config.HSTS_MAX_AGE}; includeSubDomains"
        )
    return response


# --------------------------------------------------------------------------- #
# Wiring it all onto the Flask app
# --------------------------------------------------------------------------- #


def install(app, csrf) -> None:
    """Apply every cross-cutting security concern to the Flask app.

    `csrf` is a flask_wtf.CSRFProtect already bound to `app`; passed in so the
    caller owns the extension object (and can exempt a route if it ever needs
    to). This function sets the session/cookie config, trusts the reverse proxy,
    installs the CSP nonce and security-header hooks, and opens the audit log.
    """
    from datetime import timedelta

    from werkzeug.middleware.proxy_fix import ProxyFix

    app.config.update(
        SECRET_KEY=resolve_secret_key(),
        MAX_CONTENT_LENGTH=Config.MAX_TOTAL_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=Config.COOKIE_SECURE,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="nxia_session",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=Config.SESSION_LIFETIME_MIN),
        # Belt-and-braces: never serve the Werkzeug debugger, whatever else is set.
        DEBUG=False,
        PROPAGATE_EXCEPTIONS=False,
    )

    hops = Config.TRUSTED_PROXY_HOPS
    if hops > 0:
        # Trust exactly `hops` proxy entries for client IP and scheme; without
        # this every request would look like it came from the proxy over http.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    init_audit_log()

    app.before_request(_assign_nonce)
    app.after_request(_apply_security_headers)

    @app.context_processor
    def _inject_nonce():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}
