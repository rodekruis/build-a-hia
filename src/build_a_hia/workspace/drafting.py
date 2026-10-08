"""Steps 4-6: propose and review the structure, then generate offers and Q&As."""

import re
from collections.abc import Callable
from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from werkzeug.wrappers import Response

from ..services.assembly import assemble
from ..services.content import FIELD_LABELS, OFFER_LIST_FIELDS, OFFER_TEXT_FIELDS, GeneratedContent
from ..services.drafting import ContentState, DraftingError, StructureState
from ..services.model import model_configured
from ..services.review import collect_gaps
from ..services.sessions import WorkspaceSession
from ..services.storage import ConflictError
from ..services.structure import (
    Structure,
    StructureError,
    add_category,
    add_subcategory,
    change_parent,
    merge,
    merge_preview,
    move,
    remove,
    rename,
)
from .forms import (
    ActionForm,
    AddNodeForm,
    NodeForm,
    OfferForm,
    ProposeForm,
    RenameForm,
    ReviseForm,
    TargetForm,
    VersionForm,
)
from .session import current_workspace, require_workspace, services

drafting_bp = Blueprint("drafting", __name__)

_NODE_KEY = re.compile(r"^[0-9a-f]{8}$")


def _workspace() -> WorkspaceSession:
    workspace = current_workspace()
    if workspace is None:
        abort(404)
    return workspace


def _to_structure(anchor: str = "") -> Response:
    return redirect(url_for("drafting.structure", _anchor=anchor or None))


def _structure_context(workspace: WorkspaceSession) -> dict[str, Any]:
    hia = services()
    state, draft = hia.structures.draft(workspace.key)
    return {
        "workspace": workspace,
        "state": state,
        "draft": draft,
        "problems": draft.problems() if draft else [],
        "model_ready": model_configured(hia.settings),
        "outdated": _structure_outdated(state, workspace),
        "propose_form": ProposeForm(formdata=None),
        "revise_form": ReviseForm(formdata=None),
        "action_form": ActionForm(),
    }


def _structure_outdated(state: StructureState, workspace: WorkspaceSession) -> bool:
    return state.has_draft and (
        state.sources_version != workspace.approved_sources_version
        or state.context_version != workspace.context_version
        or not workspace.sources_approved
    )


@drafting_bp.get("/structure")
@require_workspace
def structure():
    """Show the structure step, or redirect to the context step if no context is saved."""
    workspace = _workspace()
    if workspace.context is None:
        return redirect(url_for("workspace.context"))
    return render_template("drafting/structure.html", **_structure_context(workspace))


@drafting_bp.get("/structure/status")
def structure_status():
    """Render the polled structure status fragment; 410 when the session is gone.

    Polling does not extend the session's idle timeout.
    """
    workspace = current_workspace(touch=False)
    if workspace is None:
        return "", 410
    return render_template("drafting/_structure_status.html", **_structure_context(workspace))


def _propose(form: ProposeForm | ReviseForm) -> Response:
    workspace = _workspace()
    if not form.validate_on_submit():
        flash("Describe the changes you want.", "error")
        return _to_structure()
    try:
        services().structures.request_proposal(workspace, (form.instructions.data or "").strip())
    except DraftingError as error:
        flash(error.message, "error")
    else:
        flash("The AI is working on the structure. This page updates when it is ready.", "success")
    return _to_structure()


@drafting_bp.post("/structure/propose")
@require_workspace
def propose():
    """Queue an AI structure proposal for the session."""
    return _propose(ProposeForm())


@drafting_bp.post("/structure/revise")
@require_workspace
def revise():
    """Queue an AI revision of the structure draft with the user's instructions."""
    return _propose(ReviseForm())


def _message(error: Exception) -> str:
    return error.message if isinstance(error, DraftingError) else "Busy; try again."


def _edit(form: VersionForm, change: Callable[[Structure], None], success: str) -> Response:
    workspace = _workspace()
    if not form.validate_on_submit():
        flash("Check the form and try again.", "error")
        return _to_structure()
    try:
        services().structures.edit(workspace.key, form.version.data or 0, change)
    except (DraftingError, ConflictError) as error:
        flash(_message(error), "error")
    else:
        flash(success, "success")
    key_field = getattr(form, "key", None)
    return _to_structure(f"node-{key_field.data}" if key_field is not None else "")


@drafting_bp.post("/structure/rename")
@require_workspace
def rename_node():
    """Rename a category or sub-category; stale structure versions are rejected."""
    form = RenameForm()
    return _edit(
        form,
        lambda structure: rename(
            structure, form.key.data or "", form.name.data or "", form.description.data or ""
        ),
        "Saved.",
    )


@drafting_bp.post("/structure/add")
@require_workspace
def add_node():
    """Add a category, or a sub-category when a parent key is given."""
    form = AddNodeForm()
    name = form.name.data or ""
    description = form.description.data or ""

    def change(structure: Structure) -> None:
        if form.parent.data:
            add_subcategory(structure, form.parent.data, name, description)
        else:
            add_category(structure, name, description)

    return _edit(form, change, "Added.")


@drafting_bp.post("/structure/<any(up, down):direction>")
@require_workspace
def move_node(direction: str):
    """Move a node one position up or down among its siblings."""
    form = NodeForm()
    offset = -1 if direction == "up" else 1
    return _edit(form, lambda structure: move(structure, form.key.data or "", offset), "Moved.")


@drafting_bp.post("/structure/remove")
@require_workspace
def remove_node():
    """Remove a node from the structure draft."""
    form = NodeForm()
    return _edit(form, lambda structure: remove(structure, form.key.data or ""), "Removed.")


@drafting_bp.post("/structure/move-to")
@require_workspace
def move_to():
    """Move a sub-category to another category."""
    form = TargetForm()
    return _edit(
        form,
        lambda structure: change_parent(structure, form.key.data or "", form.target.data or ""),
        "Moved to the other category.",
    )


@drafting_bp.post("/structure/merge")
@require_workspace
def merge_nodes():
    """Show a merge preview, or merge two nodes once the user has confirmed.

    Both the preview and the merge reject a structure version that is no longer current.
    """
    workspace = _workspace()
    form = TargetForm()
    if not form.validate_on_submit():
        flash("Choose what to merge.", "error")
        return _to_structure()
    source_key = form.key.data or ""
    target_key = form.target.data or ""
    if form.confirm.data:
        return _edit(form, lambda structure: merge(structure, source_key, target_key), "Merged.")
    state, draft = services().structures.draft(workspace.key)
    if draft is None or state.version != form.version.data:
        flash("The structure changed in the meantime. Review it and try again.", "error")
        return _to_structure()
    try:
        source = draft.find(source_key)[1]
        result = merge_preview(draft, source_key, target_key)
    except StructureError as error:
        flash(error.message, "error")
        return _to_structure()
    return render_template(
        "drafting/merge.html",
        source=source,
        result=result,
        version=state.version,
        target_key=target_key,
    )


@drafting_bp.post("/structure/approve")
@require_workspace
def approve_structure():
    """Approve the structure version the user saw and continue to the content step."""
    workspace = _workspace()
    form = VersionForm()
    if not form.validate_on_submit():
        abort(400)
    try:
        services().structures.approve(workspace.key, form.version.data or 0)
    except (DraftingError, ConflictError) as error:
        flash(_message(error), "error")
        return _to_structure("approve")
    flash("Structure approved. You can now generate the content.", "success")
    return redirect(url_for("drafting.content"))


def _content_context(workspace: WorkspaceSession) -> dict[str, Any]:
    hia = services()
    state, approved = hia.structures.approved(workspace.key)
    states = hia.contents.states(workspace.key)
    rows = []
    generated = 0
    if approved is not None:
        for category in approved.categories:
            for sub in category.children:
                content_state = states.get(sub.key)
                generated += bool(content_state and content_state.blob)
                rows.append(
                    {
                        "category": category,
                        "subcategory": sub,
                        "state": content_state,
                        "status": _content_status(content_state, state, workspace),
                    }
                )
    pending = sum(row["status"] in ("missing", "failed", "outdated") for row in rows)
    return {
        "workspace": workspace,
        "structure_state": state,
        "approved": approved if state.approved else None,
        "rows": rows,
        "pending": pending,
        "generated": generated,
        "active": any(row["status"] in ("queued", "running") for row in rows),
        "model_ready": model_configured(hia.settings),
        "settings": hia.settings,
        "action_form": ActionForm(),
    }


def _content_status(
    content: ContentState | None, structure: StructureState, workspace: WorkspaceSession
) -> str:
    if content is None or not content.status:
        return "missing"
    if content.status == "done" and not content.is_current(structure, workspace):
        return "outdated"
    return content.status


@drafting_bp.get("/content")
@require_workspace
def content():
    """Show the content step with the generation status of every sub-category."""
    return render_template("drafting/content.html", **_content_context(_workspace()))


@drafting_bp.get("/content/status")
def content_status():
    """Render the polled content status fragment; 410 when the session is gone.

    Polling does not extend the session's idle timeout.
    """
    workspace = current_workspace(touch=False)
    if workspace is None:
        return "", 410
    return render_template("drafting/_content_status.html", **_content_context(workspace))


def _request_content(keys: list[str] | None) -> Response:
    workspace = _workspace()
    if not ActionForm().validate_on_submit():
        abort(400)
    try:
        queued = services().contents.request(workspace, keys)
    except DraftingError as error:
        flash(error.message, "error")
    else:
        if queued:
            flash(f"Generating content for {queued} sub-categories.", "success")
        else:
            flash("All content is up to date.", "success")
    return redirect(url_for("drafting.content"))


@drafting_bp.post("/content/generate")
@require_workspace
def generate_content():
    """Queue generation for every missing, failed or outdated sub-category."""
    return _request_content(None)


@drafting_bp.post("/content/<key>/regenerate")
@require_workspace
def regenerate_content(key: str):
    """Queue generation for one sub-category."""
    if not _NODE_KEY.match(key):
        abort(404)
    return _request_content([key])


@drafting_bp.get("/content/<key>")
@require_workspace
def content_detail(key: str):
    """Show one sub-category's generated content, gaps and edit forms."""
    workspace = _workspace()
    if not _NODE_KEY.match(key):
        abort(404)
    hia = services()
    state, approved = hia.structures.approved(workspace.key)
    if approved is None:
        abort(404)
    contents: dict[str, GeneratedContent] = {}
    content_state = None
    for sub_key, sub_state in hia.contents.states(workspace.key).items():
        result = hia.contents.result(workspace.key, sub_state)
        if result is not None:
            contents[sub_key] = result
        if sub_key == key:
            content_state = sub_state
    assembled = next(
        (
            (category, sub)
            for category in assemble(approved, contents)
            for sub in category.subcategories
            if sub.key == key
        ),
        None,
    )
    if assembled is None:
        abort(404)
    category, subcategory = assembled
    overrides = hia.gaps.overrides(workspace.key)
    gaps = [
        gap
        for gap in collect_gaps([category], [], overrides)
        if gap.subcategory_key == subcategory.key
    ]
    blob = content_state.blob if content_state else ""
    return render_template(
        "drafting/subcategory.html",
        category=category,
        subcategory=subcategory,
        content_state=content_state,
        status=_content_status(content_state, state, workspace),
        gaps=gaps,
        blob=blob,
        text_fields=OFFER_TEXT_FIELDS,
        list_fields=OFFER_LIST_FIELDS,
        field_labels=FIELD_LABELS,
        offer_form=OfferForm(formdata=None),
    )
