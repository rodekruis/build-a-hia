"""Local static HTML regressions; no model downloads, rendering or network access."""

import socket
from types import SimpleNamespace

import pytest

from build_a_hia.services import conversion
from build_a_hia.services.conversion import DoclingConverter
from build_a_hia.services.html_meta import extract_html_metadata
from build_a_hia.services.inspection import SourceError, SourceFormat


@pytest.fixture
def convert_html(tmp_path):
    def convert(html):
        path = tmp_path / "page.html"
        path.write_text(html, encoding="utf-8")
        return DoclingConverter().convert(
            path,
            SourceFormat.HTML,
            lambda *_args: None,
            base_url="https://example.org/program/faq/",
        )

    return convert


@pytest.mark.parametrize(
    "marker",
    [
        'style="display:none"',
        'style=" DISPLAY : NONE !important; color: red"',
        "hidden",
        'hidden="until-found"',
        'aria-hidden="true"',
        'aria-hidden=" TRUE "',
        'style="visibility:hidden"',
        'style="opacity:0"',
    ],
)
def test_hidden_answers_are_citable_without_including_excluded_content(convert_html, marker):
    html = f"""<html><body><main>
      <h1>Frequently asked questions</h1>
      <h2>Where can I register?</h2>
      <div {marker}><p>Register at the town hall every Monday.</p>
        <p>Read <a href="../../contacts">the contacts</a>.</p>
        <div hidden><p>Bring your registration card.</p></div>
      </div>
      <script>Untrusted script content</script>
      <template><p>Unused template content</p></template>
      <nav hidden><p>Navigation content</p></nav>
      <div role="navigation"><p>Navigation role content</p></div>
      </main></body></html>"""
    output = convert_html(html)
    text = "\n\n".join(unit.markdown for unit in output.units)

    assert "Register at the town hall every Monday." in text
    assert "Bring your registration card." in text
    assert "https://example.org/contacts" in text
    assert "Untrusted script content" not in text
    assert "Unused template content" not in text
    assert "Navigation content" not in text
    assert "Navigation role content" not in text
    assert output.limitations == []


def test_nested_description_list_accordion_keeps_questions_and_answers(convert_html):
    output = convert_html("""<html><body><h1>Family links</h1>
      <dl class="accordion" role="presentation"><div class="accordion-wrapper">
        <div class="accordion-inner-wrapper">
          <dt><button type="button"><div>Can I apply from abroad?</div></button></dt>
          <dd class="accordion-panel"><div><p>Contact the team in your country.</p></div></dd>
        </div></div>
        <div><div>
          <dt><button>How do I contact the team?</button></dt>
          <dd><p>See <a href="/contacts/">our contact details</a>.</p></dd>
        </div></div>
      </dl></body></html>""")
    text = output.units[0].markdown
    assert "Can I apply from abroad?" in text
    assert "Contact the team in your country." in text
    assert "How do I contact the team?" in text
    assert "https://example.org/contacts/" in text


def test_visible_content_and_metadata_remain_intact(convert_html):
    html = """<html><head><title>Family FAQ</title>
      <meta name="date" content="2026-10-09"></head><body>
      <h1>Family FAQ</h1><p>Contact the national team for support.</p>
      <dl><dt>Registration</dt><dd>Bring an identity document.</dd></dl>
      </body></html>"""
    output = convert_html(html)
    assert "Bring an identity document." in output.units[0].markdown
    assert "Contact the national team" in output.units[0].markdown
    assert output.limitations == []
    metadata = extract_html_metadata(html.encode())
    assert metadata.title == "Family FAQ"
    assert metadata.date == "2026-10-09"


def test_heading_only_html_warns_instead_of_failing(convert_html):
    output = convert_html("""<html><body><h1>Questions</h1>
      <h2>Where can displaced people apply?</h2>
      <h2>Which documents should families bring?</h2>
      <h2>When is registration open?</h2></body></html>""")
    assert output.units
    assert any("headings but very little body text" in note for note in output.limitations)


def test_heading_rich_html_with_answers_does_not_warn(convert_html):
    questions = "".join(
        f"<h2>How do I register for service {index}?</h2>"
        "<p>Visit the town hall on Monday with an identity document.</p>"
        for index in range(5)
    )
    assert convert_html(f"<html><body>{questions}</body></html>").limitations == []


def test_empty_html_retains_no_text_error(convert_html):
    with pytest.raises(SourceError) as error:
        convert_html("<html><body><script>Only script text.</script></body></html>")
    assert error.value.code == "no_text"


def test_html_converter_keeps_fetching_and_rendering_disabled(monkeypatch):
    options = {}

    def backend_options(**kwargs):
        options.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        conversion,
        "_docling",
        lambda: SimpleNamespace(
            HTMLBackendOptions=backend_options,
            InputFormat=SimpleNamespace(HTML="html"),
            HTMLFormatOption=lambda **kwargs: kwargs,
            DocumentConverter=lambda **kwargs: kwargs,
        ),
    )
    DoclingConverter._html_converter("https://example.org/faq")
    assert options == {
        "enable_remote_fetch": False,
        "enable_local_fetch": False,
        "fetch_images": False,
        "render_page": False,
        "source_uri": "https://example.org/faq",
    }


@pytest.mark.parametrize(
    ("heading_count", "body", "warns"),
    [
        (2, "", False),
        (3, "", True),
        (3, "a" * 29, True),
        (3, "a" * 30, True),
        (3, "a" * 31, False),
        (20, "a" * 88, False),
    ],
)
def test_sparse_body_policy_boundaries(heading_count, body, warns):
    markdown = "\n".join(["## " + "q" * 40] * heading_count) + "\n\n" + body
    assert conversion._html_has_sparse_body(markdown) is warns


def test_static_conversion_never_fetches_assets_or_runs_scripts(convert_html, monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("HTML conversion attempted to connect to a network resource")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    output = convert_html("""<html><head>
      <link rel="stylesheet" href="https://example.org/secret.css">
      </head><body><h1>Registration</h1>
      <div hidden><p>Register at the town hall.</p></div>
      <script src="https://example.org/secret.js">Forbidden script text</script>
      <img src="http://169.254.169.254/latest/meta-data/">
      <img src="file:///missing-local-image.png">
      </body></html>""")
    assert "Register at the town hall." in output.units[0].markdown
    assert "Forbidden script text" not in output.units[0].markdown


def test_nested_description_lists_remain_separate(convert_html):
    output = convert_html("""<html><body><h1>Support</h1>
      <dl><div><div><dt>Registration</dt><dd><p>Visit the office.</p>
        <dl><div><div><dt>Documents</dt><dd>Bring an identity card.</dd></div></div></dl>
      </dd></div></div></dl></body></html>""")
    text = output.units[0].markdown
    assert text.count("Visit the office.") == 1
    assert text.count("Bring an identity card.") == 1


def test_hidden_styles_are_removed_without_changing_visible_styles():
    html = b"""<html><body><div hidden aria-hidden="true"
      style="display : none!important; color: blue; visibility:collapse; opacity:0; width:900px">
      <p>Answer with an &amp; entity.</p></div></body></html>"""
    normalized = conversion._normalize_html(html).decode()
    assert "color: blue" in normalized
    assert "width:900px" in normalized
    assert "display" not in normalized and "visibility" not in normalized
    assert "hidden" not in normalized and "opacity" not in normalized
    assert "Answer with an &amp; entity." in normalized
