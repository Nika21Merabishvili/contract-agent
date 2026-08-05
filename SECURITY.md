# Security overview — nxia-contract-agent

This document is for the InfoSec review. It records the deployment model the
hardening assumes, maps each requirement to where it is implemented, and flags
the residual risks (including prompt injection) that are managed rather than
eliminated.

The analysis pipeline itself was not changed: no prompt, schema, JSON shape, or
Excel layout was touched, and the contract text still never leaves the machine —
analysis runs against a local Ollama with no network, cloud, API keys, or
database. The hardening is entirely in the web tier (`app.py`, `security.py`,
templates) plus deployment tooling (`serve.py`, `wsgi.py`, `manage_users.py`).

## Deployment model (confirmed)

**Network-reachable intranet, multiple users.** This was confirmed with the
owner before authentication was built, because the two viable models call for
very different postures:

- *Local-only (127.0.0.1)* would make OS login the access control, and a
  username/password system would add attack surface without adding security.
- *Network-reachable* genuinely requires authentication, authorization,
  transport security, and audit logging.

Because the app is served on the intranet, the full stack below is in place.

Identity is a **local login**: the organization had no SSO/OIDC provider to
integrate with. This is called out as a liability — a local credential store has
to be maintained and audited. **If an identity provider becomes available, prefer
wiring login to it and deleting the local store** (`security.authenticate` and
the `/login` route are the only integration points).

## How to deploy securely

1. **Secret** — generate and set a session key (never commit it):
   ```
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   Put it in the environment as `NXIA_SECRET_KEY` (see `.env.example`). The app
   **refuses to start in production without it.**
2. **Users** — create accounts on the server:
   ```
   python manage_users.py add <username>      # prompts for a password (no echo)
   ```
3. **Run behind TLS** — start the app under waitress and terminate TLS at a
   reverse proxy (nginx). Do **not** expose the Flask dev server or the waitress
   port directly.
   ```
   python serve.py        # waitress on 127.0.0.1:8000 by default
   ```
   Example nginx front end:
   ```nginx
   server {
       listen 443 ssl;
       server_name contracts.internal.example;
       ssl_certificate     /etc/ssl/contracts.crt;
       ssl_certificate_key /etc/ssl/contracts.key;
       client_max_body_size 200m;              # match NXIA_MAX_TOTAL_BYTES
       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host              $host;
           proxy_set_header X-Real-IP         $remote_addr;
           proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```
   `NXIA_TRUSTED_PROXY_HOPS` (default 1) tells the app how many proxies to trust
   for the real client IP and scheme; set it to the actual number of proxies.

All tunables (limits, lockout, session lifetime, cookie flags) are environment
variables documented in `.env.example`.

## 1. Authentication / authorization

| Requirement | Implementation |
|---|---|
| Prefer SSO over a local store | No IdP available; local login built, with the store flagged as a liability and the integration points documented above. |
| Password hashing (bcrypt/Argon2) | **bcrypt** (`security.hash_password`, cost factor `NXIA_BCRYPT_ROUNDS`, default 12). No plaintext, no fast hash. |
| Password policy + change | `manage_users.py` enforces a length floor (`NXIA_MIN_PASSWORD_LENGTH`, default 12) and disallows password==username; `passwd` changes a password. Align the floor with company policy. |
| Secure session cookies | `HttpOnly`, `Secure` (`NXIA_COOKIE_SECURE`, default on), `SameSite=Lax`; renamed `nxia_session`. |
| Session expiry + logout invalidation | `PERMANENT_SESSION_LIFETIME` (default 8h). Logout **and** password change bump a per-user `epoch` checked on every request, so a cleared/old cookie cannot be replayed even though its signature is valid. |
| Rate-limit + lockout | `security.LoginThrottle`: after `NXIA_MAX_LOGIN_FAILURES` (5) within a window, the **username and the source IP** are each locked out for `NXIA_LOCKOUT_SECONDS` (15 min). Failed logins return a generic message and do not reveal whether the username exists (a dummy bcrypt verify equalises timing). |
| Least privilege | Every route except `/login` and static assets wears `@login_required`; an unauthenticated request can reach nothing else. |
| No cross-user result retrieval | There is **no download-by-ID surface to abuse**: the workbook is returned inline in the same authenticated `/analyze` response that produced it (base64 in JSON), never stored server-side under a guessable id/filename. A user only ever receives the result of their own request. |
| `SECRET_KEY` from env | `NXIA_SECRET_KEY`; never hardcoded, never committed; the app refuses to start in production without a strong one. |

## 2. Upload validation and safe processing

The app accepts **PDF** uploads and produces **Excel** output; validation is on
the incoming PDFs (`app._validate_upload`).

- **Extension + declared MIME + magic bytes**: name must end `.pdf`, declared
  type (when present) must be a PDF type, and the file must begin with `%PDF-`.
  A renamed non-PDF is rejected.
- **Size / count / page limits**: `MAX_CONTENT_LENGTH` caps the whole request
  (`NXIA_MAX_TOTAL_BYTES`, 200 MB); each file is capped (`NXIA_MAX_PDF_BYTES`,
  25 MB); a batch is capped (`NXIA_MAX_FILES`, 20); each PDF's page count is
  capped (`NXIA_MAX_PAGES`, 100). Each guard is an independent DoS limit, since
  every contract triggers a slow model run.
- **No client filename on disk**: uploads are saved under a random
  `secrets.token_hex` name in a `TemporaryDirectory` — closing path traversal
  (`../../etc/passwd`) and null-byte tricks. The original name is kept only for
  the on-screen report and the audit log, and is scrubbed before either.
- **Temp dir outside any served path**, cleaned up via the `TemporaryDirectory`
  context manager even when analysis raises.
- **Timeouts**: PDF page-count parsing runs in a worker thread with a hard
  timeout (`NXIA_PDF_PARSE_TIMEOUT_S`), so a malformed PDF can't hang the
  precheck; the analysis itself has a wall-clock budget
  (`NXIA_ANALYSIS_TIMEOUT_S` per contract) enforced by a watchdog that trips the
  pipeline's existing cancel mechanism — no worker can be pinned indefinitely.
- **Clean parser errors**: parse failures become a normal "could not be
  analysed" report line, never a stack trace.
- **Download**: served inline as JSON with the correct xlsx MIME on the client
  side; the suggested filename is server-controlled and run through
  `security.safe_filename` so no user-controlled text steers the browser or a
  log line. (No `Content-Disposition` header applies because nothing is served
  from a file route — see the no-IDOR note above; this is deliberate.)

## 3. OWASP Top 10 baseline

- **Injection / SQLi** — **not applicable**: there is no database and no SQL. The
  only persisted state is the JSON user store, accessed as a dict, never a query.
  *If persistence is ever added, use parameterized queries / an ORM.* (Deliberate
  assessment, not an oversight.)
- **XSS** — Jinja2 autoescaping is on; no `|safe` on user data. Filenames and
  error messages (the realistic injection points) are escaped in the template and
  via `textContent`/`escapeHtml` in the frontend, so a file named
  `<img src=x onerror=...>.pdf` renders inert. A **nonce-based CSP** (below) is a
  second layer: even injected inline script would not execute.
- **CSRF** — Flask-WTF `CSRFProtect` on the whole app; the upload/cancel fetches
  send the token as `X-CSRFToken`, and the login/logout forms carry a hidden
  `csrf_token`. Every state-changing POST is verified.
- **Security headers** (`security._apply_security_headers`) — restrictive
  `Content-Security-Policy` (`default-src 'none'`, same-origin `connect-src`, and
  per-request nonces for the page's own inline CSS/JS — no `unsafe-inline`, no
  external hosts), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy`, and **HSTS** over TLS.
- **Misconfiguration** — `debug=False` and `PROPAGATE_EXCEPTIONS=False` are
  forced; generic error pages (no stack traces / internal paths); no secrets in
  source.
- **Transport** — production runs under **waitress** (`serve.py`) behind a
  TLS-terminating reverse proxy; `ProxyFix` trusts a configured number of proxy
  hops so client IP and scheme are accurate. The Flask dev server is development
  only and says so on startup.
- **Dependencies** — pinned in `requirements.txt`; audit with
  `pip-audit -r requirements.txt`.

## 4. Logging

The audit log (`security.audit` → rotating file in `NXIA_LOG_DIR`, **not**
web-accessible) records: login success/failure, lockout, logout, upload
(name, size, count, accept/reject reason), analysis start/finish/failure/timeout,
and workbook delivery. Each line carries timestamp, user, source IP, action, and
outcome. Fields are scrubbed of control characters so a crafted filename cannot
forge log lines.

**No contract content is logged.** The audit log records metadata only. The
pipeline's own diagnostics go to stderr and only ever print contract text or
model prompts/responses under the CLI's `--verbose` / `--show-input` flags, which
the web app never sets — so at production log levels no contract data, prompt, or
response is emitted.

## 5. Prompt injection (flagged, managed)

Contract text from an uploaded PDF is untrusted input fed to an LLM, and a
document could embed text intended to steer the analysis (e.g. "report this
income as non-Georgian-source"). This is **flagged, not fully solved.** Existing
mitigations, worth documenting:

- The model runs **locally with no network, filesystem, or tool access** — a
  successful injection cannot exfiltrate data or reach anything.
- Responses are constrained by **strict JSON schemas**, and 22 of 27 fields plus
  every citation are written by Python from closed code sets, not free text.
- The **tax step receives only extracted facts, not the raw contract text**,
  which narrows the surface for a clause-level steer.
- The output is **decision support with human review**, not a final tax opinion;
  OCR'd contracts are additionally flagged for verification.

No elaborate in-model defense is attempted; the risk is documented and the
human-review framing is retained.

## Residual risks / notes

- **In-memory lockout & session epoch** are single-node state. Correct for this
  one-process deployment; both fail safe (a restart only ever grants another
  login attempt or invalidates sessions). A multi-node deployment would need a
  shared store.
- **Hard-killing a pathological PDF parse** would require process-level
  isolation; here it is mitigated by the size and page-count caps plus the
  parse/analysis timeouts (the analysis timeout trips the model-call cancel;
  a truly wedged parser thread is bounded by the size cap and left to exit).
- **Local user store** is the standard liability of rolling auth; migrate to the
  org IdP when one exists.
