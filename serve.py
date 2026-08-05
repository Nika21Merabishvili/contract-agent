"""Production runner: serve the app with waitress (a real WSGI server).

Flask's built-in server (app.main()) is for development only. In production the
app runs here, under waitress, behind a reverse proxy that terminates TLS (see
SECURITY.md). waitress is used rather than gunicorn because this deployment
targets Windows, where gunicorn does not run.

    # set the required secret first (see .env.example / SECURITY.md)
    python serve.py

Configuration (environment):
    NXIA_HOST     bind address   (default 127.0.0.1 -- assumes the TLS proxy is
                                  on the same host; use 0.0.0.0 only if the proxy
                                  is elsewhere and a firewall fronts this port)
    NXIA_PORT     bind port      (default 8000)
    NXIA_THREADS  worker threads (default 8)

This binds plain HTTP on purpose: TLS belongs at the proxy. Do not expose this
port directly to the network.
"""

from __future__ import annotations

import os

from waitress import serve

from app import app


def main() -> None:
    host = os.environ.get("NXIA_HOST", "127.0.0.1")
    port = int(os.environ.get("NXIA_PORT", "8000"))
    threads = int(os.environ.get("NXIA_THREADS", "8"))
    print(
        f"nxia-contract-agent serving on http://{host}:{port} "
        f"(waitress, {threads} threads).\n"
        "  TLS terminates at the reverse proxy in front of this -- see SECURITY.md.\n"
        "  Do not expose this port directly to the network."
    )
    serve(app, host=host, port=port, threads=threads, ident="nxia-contract-agent")


if __name__ == "__main__":
    main()
