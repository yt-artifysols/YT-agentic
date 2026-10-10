"""Tiny stdlib HTTP exporter: /metrics (Prometheus text) and /healthz. Run it as a sidecar job
next to the workers: `python -m ytcore.observability.exporter --port 9464`. It exposes counts and
ratios only: no content, ids of people, tokens or channel secrets."""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ytcore.observability.metrics import collect, render_prometheus
from ytcore.store.postgres import Connect


def make_handler(connect: Connect) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                body, status = b"ok\n", 200
            elif self.path == "/metrics":
                try:
                    body, status = render_prometheus(collect(connect)).encode(), 200
                except Exception:
                    body, status = b"metrics unavailable\n", 503
            else:
                body, status = b"not found\n", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access logs: nothing here is worth logging

    return Handler


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import psycopg

    parser = argparse.ArgumentParser(prog="python -m ytcore.observability.exporter")
    parser.add_argument("--port", type=int, default=9464)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container sidecar
    args = parser.parse_args(argv)
    from ytcore.config import build_secrets_provider, load_settings
    from ytcore.safety.scrub import install_log_scrubber
    from ytcore.secrets.provider import SecretRef

    install_log_scrubber()
    core = load_settings()
    dsn = build_secrets_provider(core).get(SecretRef(core.database_url_ref)).reveal()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(lambda: psycopg.connect(dsn, autocommit=True))
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
