"""WTForms forms for the workspace steps; all forms carry a CSRF token via Flask-WTF."""

from flask_wtf import FlaskForm
from flask_wtf.file import MultipleFileField
from wtforms import (
    BooleanField,
    DateField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
    URLField,
)
from wtforms.validators import (
    DataRequired,
    InputRequired,
    Length,
    Optional,
    Regexp,
    ValidationError,
)

from ..services.languages import MAX_LANGUAGE_INPUT, resolve_language
from ..services.structure import MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH

DIRECTION_CHOICES = [
    ("", "Default for the language"),
    ("ltr", "Left to right"),
    ("rtl", "Right to left"),
    ("auto", "Automatic"),
]


class ContextForm(FlaskForm):
    """Submits the required project context: country, situation, target group and language."""

    country = StringField("Country", validators=[DataRequired(), Length(max=100)])
    situation = TextAreaField("Crisis or situation", validators=[DataRequired(), Length(max=1000)])
    target_group = TextAreaField("Target group", validators=[DataRequired(), Length(max=1000)])
    locations = TextAreaField("Regions or locations", validators=[DataRequired(), Length(max=1000)])
    output_language = StringField(
        "Output language",
        validators=[DataRequired(), Length(max=MAX_LANGUAGE_INPUT)],
        default="English",
        render_kw={"list": "language-options", "autocomplete": "off"},
    )
    locale_dir = SelectField("Text direction", choices=DIRECTION_CHOICES, default="")
    source_languages = StringField(
        "Languages of your sources", validators=[Optional(), Length(max=300)]
    )
    reference_date = DateField("Reference date", validators=[Optional()])

    def validate_output_language(self, field: StringField) -> None:
        """Accept a known language name (English or Dutch) or a well-formed language code."""
        if field.data and resolve_language(field.data) is None:
            raise ValidationError(
                "Unknown language. Choose one from the suggestions or enter a language code, "
                "such as “am” or “pt-BR”."
            )


class UploadForm(FlaskForm):
    """Submits one or more source files; size and type are checked by the sources service."""

    files = MultipleFileField("Files")


class UrlForm(FlaskForm):
    """Submits a web address to fetch as a source; title and date come from the page."""

    url = URLField("Web page or document address", validators=[DataRequired(), Length(max=2048)])


class SourceDetailsForm(FlaskForm):
    """Submits the editable title and document date of a source."""

    title = StringField("Title", validators=[DataRequired(), Length(max=200)])
    document_date = DateField("Document date", validators=[Optional()])


class ApproveForm(FlaskForm):
    """Submits source approval, optionally accepting that some sources failed."""

    accept_failed = BooleanField("Continue without the sources that failed")


class ActionForm(FlaskForm):
    """Carries only the CSRF token for single-button actions."""


NODE_KEY = Regexp(r"^[0-9a-f]{8}$")


class ProposeForm(FlaskForm):
    """Submits a structure proposal request with optional instructions for the AI."""

    instructions = TextAreaField(
        "Instructions for the AI (optional)", validators=[Optional(), Length(max=2000)]
    )


class ReviseForm(FlaskForm):
    """Submits a structure revision request; the instructions are required."""

    instructions = TextAreaField(
        "Describe the changes you want", validators=[DataRequired(), Length(max=2000)]
    )


class VersionForm(FlaskForm):
    """Submits the structure version the user saw, so stale edits can be rejected."""

    version = IntegerField(validators=[InputRequired()])


class NodeForm(VersionForm):
    """Submits a structure version and a node key (8 lowercase hex characters)."""

    key = StringField(validators=[DataRequired(), NODE_KEY])


class RenameForm(NodeForm):
    """Submits a new name and description for a structure node."""

    name = StringField("Name", validators=[DataRequired(), Length(max=MAX_NAME_LENGTH)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=MAX_DESCRIPTION_LENGTH)]
    )


class AddNodeForm(VersionForm):
    """Submits a new category, or a sub-category when a parent node key is given."""

    parent = StringField(validators=[Optional(), NODE_KEY])
    name = StringField("Name", validators=[DataRequired(), Length(max=MAX_NAME_LENGTH)])
    description = TextAreaField(
        "Description", validators=[Optional(), Length(max=MAX_DESCRIPTION_LENGTH)]
    )


class TargetForm(NodeForm):
    """Submits a node and a target node key to move or merge into, with a confirm flag."""

    target = StringField(validators=[DataRequired(), NODE_KEY])
    confirm = BooleanField()


class BlobForm(FlaskForm):
    """Submits the content blob id (16 lowercase hex) the user saw, to reject stale edits."""

    blob = StringField(validators=[DataRequired(), Regexp(r"^[0-9a-f]{16}$")])


def _long_text(label: str) -> TextAreaField:
    return TextAreaField(label, validators=[Optional(), Length(max=5000)])


def _list_text(label: str) -> TextAreaField:
    return TextAreaField(f"{label} (one per line)", validators=[Optional(), Length(max=2000)])


class OfferForm(BlobForm):
    """Submits the fields of one offer; list fields take one value per line."""

    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    description = _long_text("Description")
    phone_numbers = _list_text("Phone numbers")
    emails = _list_text("Emails")
    web_urls = _list_text("Websites")
    address = _long_text("Address")
    open_weekdays = _long_text("Opening hours (weekdays)")
    open_weekend = _long_text("Opening hours (weekend)")
    need_to_know = _long_text("What you need to know")
    more_info = _long_text("More information")
    chapter = StringField("Chapter", validators=[Optional(), Length(max=200)])


class QuestionForm(BlobForm):
    """Submits a question and answer, optionally as a follow-up to another question."""

    question = StringField("Question", validators=[DataRequired(), Length(max=200)])
    answer = TextAreaField("Answer", validators=[DataRequired(), Length(max=5000)])
    parent = StringField("Follow-up to", validators=[Optional(), Regexp(r"^[A-Za-z0-9_-]{1,40}$")])


class EmptyDecisionForm(BlobForm):
    """Submits whether to keep an empty sub-category or leave it out of the export."""

    decision = SelectField(
        choices=[("keep", "Keep it without content"), ("drop", "Leave it out of the export")]
    )


class GapForm(FlaskForm):
    """Submits a gap's status, suggested action and contact, and the page to return to."""

    status = SelectField(
        "Status", choices=[("open", "Open"), ("resolved", "Resolved"), ("wont_fix", "Won't fix")]
    )
    suggested_action = TextAreaField("Suggested action", validators=[Optional(), Length(max=500)])
    suggested_contact = StringField("Suggested contact", validators=[Optional(), Length(max=200)])
    back = StringField(validators=[Optional(), NODE_KEY])
