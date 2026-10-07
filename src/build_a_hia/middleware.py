"""Request timing logs and security headers for every response."""

import logging
from time import perf_counter

from flask import Flask, g, request

logger = logging.getLogger(__name__)


def register_middleware(app: Flask) -> None:
    """Register request hooks for security headers, caching and request logging.

    Every response gets a strict Content-Security-Policy and framing and sniffing protection,
    plus HSTS when secure cookies are configured (production). Non-static responses default
    to `Cache-Control: no-store`
    because they show session content. Requests other than `/health` are logged with
    method, path, status and duration, but no query string or body.

    Args:
        app: The Flask app to register the hooks on.
    """

    @app.before_request
    def start_request_timer() -> None:
        if request.path != "/health":
            g.request_started_at = perf_counter()

    @app.after_request
    def add_response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Pages show session content (sources, drafts); keep them out of shared and back caches.
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")

        # TLS ends at the ingress, so request.is_secure is False; browsers ignore HSTS over HTTP.
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        started_at = g.get("request_started_at")
        if started_at is not None:
            logger.info(
                "HTTP request",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
            )
        return response
