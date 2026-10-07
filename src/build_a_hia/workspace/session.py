"""Request helpers for the anonymous workspace session cookie and the services container."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import current_app, flash, g, redirect, request, url_for
from werkzeug.wrappers import Response

from ..services.container import Services
from ..services.sessions import WorkspaceSession

COOKIE_NAME = "hia_session"


def services() -> Services:
    """Return the services container registered on the current app."""
    return current_app.extensions["hia_services"]


def current_workspace(*, touch: bool = True) -> WorkspaceSession | None:
    """Load the workspace session identified by the request's session cookie.

    The result is cached for the request. Only the session matching the cookie token is
    loaded, so one user's data is never reachable from another session. When the cookie
    names an expired or unknown session, `g.hia_cookie_invalid` is set so the cookie is
    cleared on the response.

    Args:
        touch: Whether to record activity and extend the idle timeout. Status polling
            passes False so an open tab does not keep a session alive.

    Returns:
        The active workspace session, or None if there is no valid session.
    """
    if "hia_workspace" in g:
        return g.hia_workspace
    token = request.cookies.get(COOKIE_NAME)
    workspace = services().sessions.load(token) if token else None
    if workspace is not None and touch:
        workspace = services().sessions.touch(workspace)
    g.hia_workspace = workspace
    g.hia_cookie_invalid = token is not None and workspace is None
    return workspace


def set_session_cookie(response: Response, token: str) -> None:
    """Set the HttpOnly, SameSite=Lax session cookie, limited to the session's maximum age.

    Args:
        response: The response to set the cookie on.
        token: The secret session token; only its hash is stored server-side.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=services().settings.session_max_seconds,
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        httponly=True,
        samesite="Lax",
    )
    g.hia_cookie_invalid = False


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie on the response.

    Args:
        response: The response to clear the cookie on.
    """
    response.delete_cookie(
        COOKIE_NAME,
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        httponly=True,
        samesite="Lax",
    )


def require_workspace[**P](view: Callable[P, Any]) -> Callable[P, Any]:
    """Redirect to the context step unless the request has an active workspace session.

    Flashes an expiry message when the cookie named a session that no longer exists.

    Args:
        view: The view function to protect.

    Returns:
        The wrapped view function.
    """

    @wraps(view)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        if current_workspace() is None:
            if g.get("hia_cookie_invalid"):
                flash("Your session has expired or was deleted. Start again.", "error")
            return redirect(url_for("workspace.context"))
        return view(*args, **kwargs)

    return wrapper
