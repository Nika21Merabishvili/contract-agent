"""WSGI entry point for a production server.

Exposes the Flask app as `application` (the WSGI convention) and `app`, so it can
be served by waitress, gunicorn (on POSIX), uWSGI, or mod_wsgi without importing
`app.main()` (which is the *development* server and must not run in production):

    waitress-serve --listen=127.0.0.1:8000 wsgi:application
    gunicorn wsgi:application            # POSIX only

Importing this module runs security.install (via app.py), which refuses to start
in production without NXIA_SECRET_KEY set. See serve.py for a batteries-included
runner and SECURITY.md for the reverse-proxy / TLS deployment.
"""

from __future__ import annotations

from app import app

application = app
