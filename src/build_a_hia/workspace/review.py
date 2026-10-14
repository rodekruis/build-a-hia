"""Steps 7-8: review and edit generated content, track gaps, and download the workbooks."""

import logging
import re
from collections.abc import Callable

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.wrappers import Response

from ..services.content import OFFER_LIST_FIELDS, OFFER_TEXT_FIELDS, GeneratedContent
from ..services.drafting import DraftingError
from ..services.export import EXPORT_FILES, ExportError, content_status
from ..services.review import (
    ReviewError,
    add_offer,
    add_question,
    remove_offer,
    remove_question,
    update_offer,
    update_question,
)
from ..services.sessions import WorkspaceSession
from ..services.storage import ConflictError
from ..services.workbook import TEMPLATE_VERSION, TemplateError
from .forms import (
    ActionForm,
    BlobForm,
    EmptyDecisionForm,
    GapForm,
    OfferForm,
    QuestionForm,
)
from .session import current_workspace, require_workspace, services

review_bp = Blueprint("review", __name__)
logger = logging.getLogger(__name__)

_NODE_KEY = re.compile(r"^[0-9a-f]{8}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workspace() -> WorkspaceSession:
    workspace = current_workspace()
    if workspace is None:
        abort(404)
    return workspace


def _check(pattern: re.Pattern[str], value: str) -> None:
    if not pattern.match(value):
        abort(404)


def _to_content(key: str, anchor: str = "") -> Response:
    return redirect(url_for("drafting.content_detail", key=key, _anchor=anchor or None))


def _offer_values(form: OfferForm) -> dict[str, str]:
    return {name: getattr(form, name).data or "" for name in OFFER_TEXT_FIELDS + OFFER_LIST_FIELDS}


def _change(
    key: str,
    form: BlobForm,
    change: Callable[[GeneratedContent], object],
    success: str,
    anchor: str = "",
) -> Response:
    _check(_NODE_KEY, key)
    workspace = _workspace()
    autosave = request.accept_mimetypes.best == "application/json"

    def not_saved(message: str, status: int = 400) -> Response:
        if autosave:
            response = jsonify(saved=False, message=message)
            response.status_code = status
            return response
        flash(message, "error")
        return _to_content(key, anchor)

    if not form.validate_on_submit():
        message = next(iter(form.errors.values()), ["Check the form."])[0]
        return not_saved(f"Not saved: {message}")
    hia = services()
    state = hia.contents.state(workspace.key, key)
    if state is None or not state.blob:
        abort(404)
    if state.blob != form.blob.data:
        return not_saved("This content changed in the meantime. Reload and try again.", 409)
    content = hia.contents.result(workspace.key, state)
    if content is None:
        abort(404)
    try:
        change(content)
        updated = hia.contents.save_edit(workspace.key, key, state.blob, content)
    except (ReviewError, DraftingError) as error:
        return not_saved(error.message)
    except ConflictError:
        return not_saved("Busy; try again.", 409)
    else:
        if autosave:
            return jsonify(saved=True, blob=updated.blob)
        flash(success, "success")
    return _to_content(key, anchor)


@review_bp.post("/content/<key>/offers/new")
@require_workspace
def new_offer(key: str):
    """Add an offer to a sub-category's content; edits based on a stale blob are rejected."""
    form = OfferForm()
    return _change(
        key, form, lambda content: add_offer(content, _offer_values(form)), "Offer added.", "offers"
    )


@review_bp.post("/content/<key>/offers/<offer_id>")
@require_workspace
def edit_offer(key: str, offer_id: str):
    """Save a reviewer's edit to one offer; the content then needs approval again."""
    _check(_REF, offer_id)
    form = OfferForm()
    return _change(
        key,
        form,
        lambda content: update_offer(content, offer_id, _offer_values(form)),
        "Offer saved. Approve the content again.",
        f"offer-{offer_id}",
    )


@review_bp.post("/content/<key>/offers/<offer_id>/remove")
@require_workspace
def delete_offer(key: str, offer_id: str):
    """Remove one offer from a sub-category's content."""
    _check(_REF, offer_id)
    return _change(
        key, BlobForm(), lambda content: remove_offer(content, offer_id), "Offer removed.", "offers"
    )


@review_bp.post("/content/<key>/questions/new")
@require_workspace
def new_question(key: str):
    """Add a question and answer to a sub-category's content."""
    form = QuestionForm()
    return _change(
        key,
        form,
        lambda content: add_question(
            content, form.question.data or "", form.answer.data or "", form.parent.data or ""
        ),
        "Question added.",
        "questions",
    )


@review_bp.post("/content/<key>/questions/<ref>")
@require_workspace
def edit_question(key: str, ref: str):
    """Save a reviewer's edit to one question; the content then needs approval again."""
    _check(_REF, ref)
    form = QuestionForm()
    return _change(
        key,
        form,
        lambda content: update_question(
            content, ref, form.question.data or "", form.answer.data or "", form.parent.data or ""
        ),
        "Question saved. Approve the content again.",
        f"question-{ref}",
    )


@review_bp.post("/content/<key>/questions/<ref>/remove")
@require_workspace
def delete_question(key: str, ref: str):
    """Remove one question from a sub-category's content."""
    _check(_REF, ref)
    return _change(
        key,
        BlobForm(),
        lambda content: remove_question(content, ref),
        "Question removed.",
        "questions",
    )


def _review(
    key: str, form: BlobForm, *, empty_decision: str = "", invalid: str = "Check the form."
) -> Response:
    _check(_NODE_KEY, key)
    workspace = _workspace()
    if not form.validate_on_submit():
        flash(invalid, "error")
        return _to_content(key, "approval")
    hia = services()
    state = hia.contents.state(workspace.key, key)
    structure_state = hia.structures.state(workspace.key)
    if state is None or content_status(state, structure_state, workspace) != "done":
        flash("Only current, generated content can be approved. Regenerate it first.", "error")
        return _to_content(key, "approval")
    try:
        hia.contents.review(
            workspace.key, key, form.blob.data or "", approve=True, empty_decision=empty_decision
        )
    except DraftingError as error:
        flash(error.message, "error")
        return _to_content(key, "approval")
    flash("Content approved." if empty_decision != "drop" else "Sub-category left out.", "success")
    return redirect(url_for("review.overview"))


@review_bp.post("/content/<key>/approve")
@require_workspace
def approve_content(key: str):
    """Approve current content; redirect to the review."""
    return _review(key, BlobForm())


@review_bp.post("/content/<key>/empty")
@require_workspace
def decide_empty(key: str):
    """Approve a sub-category without content, keeping it or leaving it out of the export."""
    form = EmptyDecisionForm()
    return _review(key, form, empty_decision=form.decision.data or "keep")


@review_bp.post("/gaps/<gap_id>")
@require_workspace
def update_gap(gap_id: str):
    """Update a gap's suggested action; 404 for gaps not in this session's plan.

    Redirects back to the sub-category page when a valid node key is given, else to the review.
    """
    _check(_REF, gap_id)
    workspace = _workspace()
    form = GapForm()
    autosave = request.accept_mimetypes.best == "application/json"
    back = form.back.data if _NODE_KEY.match(form.back.data or "") else ""
    target = (
        _to_content(back, "gaps") if back else redirect(url_for("review.overview", _anchor="gaps"))
    )
    if not form.validate_on_submit():
        if autosave:
            return jsonify(saved=False, message="Check the suggested action and try again."), 400
        flash("Check the form and try again.", "error")
        return target
    hia = services()
    gap = next((gap for gap in hia.exports.plan(workspace).gaps if gap.id == gap_id), None)
    if gap is None:
        abort(404)
    try:
        hia.gaps.update(
            workspace.key,
            gap_id,
            status=gap.status,
            action=form.suggested_action.data or "",
            contact="",
        )
    except ReviewError as error:
        if autosave:
            return jsonify(saved=False, message=error.message), 400
        flash(error.message, "error")
    else:
        if autosave:
            return jsonify(saved=True)
        flash("Gap updated.", "success")
    return target


@review_bp.get("/review")
@require_workspace
def overview():
    """Show the review overview: sub-categories to approve and one list of gaps.

    Items that still need attention come first; the export keeps the structure's order.
    """
    workspace = _workspace()
    plan = services().exports.plan(workspace)
    return render_template(
        "review/review.html",
        workspace=workspace,
        plan=plan,
        subcategories=sorted(plan.subcategories, key=lambda sub: sub.approved),
        reviewed=sum(sub.approved for sub in plan.subcategories),
        gaps=sorted(plan.gaps, key=lambda gap: gap.status != "open"),
    )


@review_bp.get("/download")
@require_workspace
def download():
    """Show the download step with the export plan and the latest snapshot."""
    workspace = _workspace()
    hia = services()
    return render_template(
        "review/download.html",
        workspace=workspace,
        plan=hia.exports.plan(workspace),
        latest=hia.exports.latest(workspace.key),
        action_form=ActionForm(),
        names=EXPORT_FILES,
        template_version=TEMPLATE_VERSION,
    )


@review_bp.post("/download")
@require_workspace
def create_download():
    """Build both workbooks as a new snapshot; refused while the export plan has blockers."""
    workspace = _workspace()
    if not ActionForm().validate_on_submit():
        abort(400)
    try:
        services().exports.create_snapshot(workspace)
    except ExportError as error:
        flash(error.message, "error")
    except TemplateError:
        logger.exception("The pinned HIA template failed its checks")
        flash("The HIA template could not be used. Contact the maintainers.", "error")
    else:
        flash("Both workbooks are ready to download.", "success")
    return redirect(url_for("review.download"))


@review_bp.get("/download/<snapshot>/<name>")
@require_workspace
def download_file(snapshot: str, name: str):
    """Send one workbook from a snapshot of this session as a non-cacheable attachment."""
    _check(re.compile(r"^[0-9a-f]{12}$"), snapshot)
    if name not in EXPORT_FILES:
        abort(404)
    workspace = _workspace()
    try:
        data = services().exports.file(workspace.key, snapshot, name)
    except ExportError as error:
        flash(error.message, "error")
        return redirect(url_for("review.download"))
    response = Response(data, mimetype=XLSX)
    response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    response.headers["Cache-Control"] = "no-store"
    return response
