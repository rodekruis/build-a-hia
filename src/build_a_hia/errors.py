"""Error pages that never expose internal error details."""

import logging

from flask import Flask, render_template
from flask_wtf.csrf import CSRFError

logger = logging.getLogger(__name__)


def register_error_handlers(app: Flask) -> None:
    """Register HTML handlers for CSRF failures (400), 404, 413 and 500 errors.

    Unhandled errors are logged server-side; the user only sees a generic page.

    Args:
        app: The Flask app to register the handlers on.
    """

    @app.errorhandler(CSRFError)
    def csrf_error(_error: CSRFError) -> tuple[str, int]:
        return render_template("errors/400.html"), 400

    @app.errorhandler(404)
    def not_found(_error: Exception) -> tuple[str, int]:
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(_error: Exception) -> tuple[str, int]:
        return render_template("errors/413.html"), 413

    @app.errorhandler(500)
    def internal_error(_error: Exception) -> tuple[str, int]:
        logger.exception("Unhandled application error")
        return render_template("errors/500.html"), 500
