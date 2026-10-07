"""Home page and health check."""

from flask import Blueprint, Response, jsonify, render_template

from ..workspace.session import current_workspace, services

main_bp = Blueprint("main", __name__)


@main_bp.get("/")
def index() -> str:
    """Show the home page, aware of the current workspace session if there is one."""
    return render_template(
        "main/index.html",
        workspace=current_workspace(),
        settings=services().settings,
    )


@main_bp.get("/health")
def health() -> tuple[Response, int]:
    """Return a static liveness response; it does not check storage or the model."""
    return jsonify(status="ok"), 200
