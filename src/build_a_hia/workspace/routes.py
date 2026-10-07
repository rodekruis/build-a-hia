"""Steps 1-3: enter the project context, add and check sources, and approve them."""

import re
from dataclasses import replace
from datetime import date
from typing import Any, cast

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from ..services.context import ProjectContext, default_direction
from ..services.inspection import SourceError, format_label
from ..services.languages import LANGUAGES, language_label, resolve_language
from ..services.sessions import WorkspaceSession
from ..services.sources import Source, markdown_blob
from ..services.storage import NotFoundError
from .forms import (
    ActionForm,
    ApproveForm,
    ContextForm,
    SourceDetailsForm,
    UploadForm,
    UrlForm,
)
from .session import (
    clear_session_cookie,
    current_workspace,
    require_workspace,
    services,
    set_session_cookie,
)

workspace_bp = Blueprint("workspace", __name__)

_SOURCE_ID = re.compile(r"^[0-9a-f]{16}$")
PREVIEW_CHARS = 20_000
DIRECTION_LABELS = {"ltr": "left to right", "rtl": "right to left", "auto": "automatic direction"}


@workspace_bp.after_app_request
def clear_invalid_cookie(response: Response) -> Response:
    """Delete the session cookie when it pointed to an expired or unknown session.

    Args:
        response: The outgoing response.

    Returns:
        The same response, with the cookie cleared if needed.
    """
    if g.get("hia_cookie_invalid"):
        clear_session_cookie(response)
    return response


@workspace_bp.app_template_filter("format_label")
def format_label_filter(value: str) -> str:
    """Template filter that turns a stored format code into a readable label."""
    return format_label(value)


def _workspace() -> WorkspaceSession:
    workspace = current_workspace()
    if workspace is None:
        abort(404)
    return workspace


def _source_or_404(workspace: WorkspaceSession, source_id: str) -> Source:
    if not _SOURCE_ID.match(source_id):
        abort(404)
    source = services().sources.get(workspace.key, source_id)
    if source is None:
        abort(404)
    return source


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""


def _render_sources(workspace: WorkspaceSession, status: int = 200, **forms: Any):
    hia = services()
    context = workspace.context
    page = {
        "workspace": workspace,
        "context": context,
        "expires_at": hia.sessions.expires_at(workspace),
        "settings": hia.settings,
        "upload_form": UploadForm(),
        "url_form": UrlForm(formdata=None),
        "delete_form": ActionForm(),
        **_status_context(workspace),
    }
    page.update(forms)
    return render_template("workspace/sources.html", **page), status


def _status_context(workspace: WorkspaceSession) -> dict[str, Any]:
    hia = services()
    return {
        "workspace": workspace,
        "sources": hia.sources.list_for(workspace.key),
        "summary": hia.sources.summary(workspace.key),
        "action_form": ActionForm(),
        "approve_form": ApproveForm(formdata=None),
    }


@workspace_bp.route("/context", methods=["GET", "POST"])
def context():
    """Show and save the project context; the first save creates the session.

    A new session sets the session cookie on the redirect to the sources step.
    """
    workspace = current_workspace()
    existing = workspace.context if workspace else None
    if request.method == "GET" and existing is not None:
        form = ContextForm(
            data={
                "country": existing.country,
                "situation": existing.situation,
                "target_group": existing.target_group,
                "locations": existing.locations,
                "output_language": language_label(existing.output_language),
                "locale_dir": existing.locale_dir,
                "source_languages": existing.source_languages,
                "reference_date": (
                    date.fromisoformat(existing.reference_date) if existing.reference_date else None
                ),
            }
        )
    else:
        form = ContextForm()

    if form.validate_on_submit():
        language = resolve_language(form.output_language.data or "") or "en"
        values = {
            "country": (form.country.data or "").strip(),
            "situation": (form.situation.data or "").strip(),
            "target_group": (form.target_group.data or "").strip(),
            "locations": (form.locations.data or "").strip(),
            "output_language": language,
            "locale_dir": form.locale_dir.data or default_direction(language),
            "source_languages": (form.source_languages.data or "").strip(),
            "reference_date": _iso(form.reference_date.data),
        }
        new_context = replace(existing, **values) if existing else ProjectContext(**values)
        hia = services()
        token = None
        if workspace is None:
            token, workspace = hia.sessions.create()
        hia.sessions.save_context(workspace.key, new_context)
        known = new_context.language
        flash(
            f"Context saved. Output language: {known.name if known else language} ({language}), "
            f"{DIRECTION_LABELS[new_context.locale_dir]}.",
            "success",
        )
        if known is None:
            flash(
                f"“{language}” is not a language we know, so it is used as typed. Check that it is "
                "the right language code before you continue.",
                "error",
            )
        response = redirect(url_for("workspace.sources"))
        if token:
            set_session_cookie(response, token)
        return response

    status = 400 if form.errors else 200
    return render_template(
        "workspace/context.html", form=form, workspace=workspace, languages=LANGUAGES
    ), status


@workspace_bp.get("/sources")
@require_workspace
def sources():
    """Show the sources step, or redirect to the context step if no context is saved."""
    workspace = _workspace()
    if workspace.context is None:
        return redirect(url_for("workspace.context"))
    return _render_sources(workspace)


@workspace_bp.get("/sources/status")
def sources_status():
    """Render the polled source status fragment; 410 when the session is gone.

    Polling does not extend the session's idle timeout.
    """
    workspace = current_workspace(touch=False)
    if workspace is None:
        return "", 410
    return render_template("workspace/_status.html", **_status_context(workspace))


@workspace_bp.post("/sources/files")
@require_workspace
def upload_files():
    """Add uploaded files as sources and queue them for conversion.

    Each file is read up to one byte over the size limit so oversized files are rejected.
    """
    workspace = _workspace()
    form = UploadForm()
    if not form.validate_on_submit():
        flash("The upload could not be processed. Try again.", "error")
        return redirect(url_for("workspace.sources"))
    uploads = [upload for upload in request.files.getlist(form.files.name) if upload.filename]
    if not uploads:
        flash("Choose at least one file.", "error")
        return redirect(url_for("workspace.sources"))

    hia = services()
    for upload in uploads:
        data = upload.stream.read(hia.settings.max_file_bytes + 1)
        try:
            source = hia.sources.add_file(workspace, filename=upload.filename or "", data=data)
        except SourceError as error:
            flash(f"{upload.filename}: {error.message}", "error")
        else:
            flash(f"Added {source.reference}: {source.title}.", "success")
    return redirect(url_for("workspace.sources"))


@workspace_bp.post("/sources/url")
@require_workspace
def add_url():
    """Add a web address as a source and queue it for fetching and conversion."""
    workspace = _workspace()
    form = UrlForm()
    if not form.validate_on_submit():
        return _render_sources(workspace, 400, url_form=form)
    try:
        source = services().sources.add_url(workspace, url=form.url.data or "")
    except SourceError as error:
        cast("list[str]", form.url.errors).append(error.message)
        return _render_sources(workspace, 400, url_form=form)
    flash(f"Added {source.reference}: {source.url}.", "success")
    return redirect(url_for("workspace.sources"))


@workspace_bp.route("/sources/<source_id>", methods=["GET", "POST"])
@require_workspace
def source_detail(source_id: str):
    """Show a source with a preview of its converted text, and save its title and date."""
    workspace = _workspace()
    source = _source_or_404(workspace, source_id)
    if request.method == "GET":
        form = SourceDetailsForm(
            data={
                "title": source.title,
                "document_date": (
                    date.fromisoformat(source.document_date) if source.document_date else None
                ),
            }
        )
    else:
        form = SourceDetailsForm()
    if form.validate_on_submit():
        services().sources.update_metadata(
            workspace.key,
            source.id,
            title=form.title.data or "",
            document_date=_iso(form.document_date.data),
        )
        flash("Source details saved.", "success")
        return redirect(url_for("workspace.source_detail", source_id=source.id))

    preview = ""
    if source.chunk_count:
        try:
            raw = services().blobs.get(markdown_blob(workspace.key, source.id))
        except NotFoundError:
            raw = b""
        preview = raw.decode("utf-8", errors="replace")[:PREVIEW_CHARS]
    status = 400 if form.errors else 200
    return render_template(
        "workspace/source.html",
        source=source,
        form=form,
        preview=preview,
        preview_chars=PREVIEW_CHARS,
        action_form=ActionForm(),
    ), status


def _source_action(source_id: str, action: str) -> Response:
    workspace = _workspace()
    source = _source_or_404(workspace, source_id)
    if not ActionForm().validate_on_submit():
        abort(400)
    sources_service = services().sources
    if action == "cancel":
        sources_service.cancel(workspace.key, source.id)
        flash(f"Cancelled {source.reference}.", "success")
    elif action == "retry":
        try:
            sources_service.retry(workspace.key, source.id)
        except SourceError as error:
            flash(error.message, "error")
        else:
            flash(f"Retrying {source.reference}.", "success")
    else:
        sources_service.remove(workspace.key, source.id)
        flash(f"Removed {source.reference}.", "success")
    return redirect(url_for("workspace.sources"))


@workspace_bp.post("/sources/<source_id>/cancel")
@require_workspace
def cancel_source(source_id: str):
    """Cancel processing of a source."""
    return _source_action(source_id, "cancel")


@workspace_bp.post("/sources/<source_id>/retry")
@require_workspace
def retry_source(source_id: str):
    """Queue a source for processing again."""
    return _source_action(source_id, "retry")


@workspace_bp.post("/sources/<source_id>/remove")
@require_workspace
def remove_source(source_id: str):
    """Remove a source and its stored data."""
    return _source_action(source_id, "remove")


@workspace_bp.post("/sources/approve")
@require_workspace
def approve_sources():
    """Approve the current sources and continue to the structure step."""
    workspace = _workspace()
    form = ApproveForm()
    if not form.validate_on_submit():
        abort(400)
    try:
        services().sources.approve(workspace.key, accept_failed=bool(form.accept_failed.data))
    except SourceError as error:
        flash(error.message, "error")
        return redirect(url_for("workspace.sources", _anchor="readiness"))
    flash("Sources approved. Next, propose the structure.", "success")
    return redirect(url_for("drafting.structure"))


@workspace_bp.post("/session/delete")
@require_workspace
def delete_session():
    """Delete the session and all its data, and clear the session cookie."""
    workspace = _workspace()
    if not ActionForm().validate_on_submit():
        abort(400)
    services().sessions.delete(workspace.key)
    flash("Your session and all its data were deleted.", "success")
    response = redirect(url_for("main.index"))
    clear_session_cookie(response)
    return response
