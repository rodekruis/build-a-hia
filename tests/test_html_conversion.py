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
        "infer_furniture": False,
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


def test_main_content_excludes_site_chrome_but_keeps_article_header_and_footer(convert_html):
    output = convert_html("""<html><head><title>Service guide</title></head><body>
      <header><h1>Site navigation</h1><p>Desktop menu item</p></header>
      <div role="banner">Mobile menu item</div>
      <main><article><header><h1>Financial support</h1>
        <p>Support for families with low income.</p></header>
        <p>Apply at the town hall.</p>
        <footer><p>Service information updated in October.</p></footer></article>
        <aside><h2>Service contact</h2><p>Call 0800 1234.</p></aside>
      </main>
      <section><h2>Quick links</h2><p>Donate to our organisation</p></section>
      <footer><p>Newsletter subscription and social links</p></footer>
      </body></html>""")
    text = output.units[0].markdown
    for expected in (
        "Financial support",
        "Support for families with low income.",
        "Apply at the town hall.",
        "Service information updated in October.",
        "Call 0800 1234.",
    ):
        assert expected in text
    for excluded in (
        "Site navigation",
        "Desktop menu item",
        "Mobile menu item",
        "Quick links",
        "Donate",
        "Newsletter subscription",
    ):
        assert excluded not in text


@pytest.mark.parametrize("landmark", ["main", 'div role="main"', "article"])
def test_content_landmark_preserves_hidden_faq_answers_and_relative_links(convert_html, landmark):
    closing = landmark.split()[0]
    output = convert_html(f"""<html><body><header>Unrelated menu</header>
      <{landmark}><h1>Family links</h1><dl><div><div>
        <dt><button type="button">Can I apply from abroad?</button></dt>
        <dd hidden><p>Contact the team in your country.</p>
        <a href="../../contacts">Contact details</a></dd>
      </div></div></dl></{closing}>
      <footer>Unrelated newsletter</footer></body></html>""")
    text = output.units[0].markdown
    assert "Can I apply from abroad?" in text
    assert "Contact the team in your country." in text
    assert "https://example.org/contacts" in text
    assert "Unrelated" not in text


@pytest.mark.parametrize(
    "widget",
    [
        'div class="c-contact-flyout"',
        'div class="cookie-banner"',
        'section id="newsletter-signup"',
        'div role="dialog"',
        'div aria-modal="true"',
        'div class="breadcrumbs"',
        'div role="search"',
    ],
)
def test_embedded_widgets_are_removed_even_inside_main(convert_html, widget):
    closing = widget.split()[0]
    output = convert_html(f"""<html><body><main>
      <h1>Cash assistance</h1><p>Apply with proof of income.</p>
      <{widget} hidden><p>Enter your postcode into this widget.</p>
        <form><label>Postcode widget field</label><input placeholder="Type postcode">
        <button>Widget submit</button></form>
      </{closing}>
      <div class="contact-details"><p>Service hotline: 0800 1234.</p></div>
      </main></body></html>""")
    text = output.units[0].markdown
    assert "Apply with proof of income." in text
    assert "Service hotline: 0800 1234." in text
    assert "widget" not in text.casefold()


def test_no_landmark_fallback_keeps_body_and_faq_not_site_chrome(convert_html):
    output = convert_html("""<html><body><header><p>Site menu</p></header>
      <h1>Questions</h1><h2>Who can apply?</h2>
      <div style="display:none"><p>Families registered with the municipality.</p></div>
      <footer><p>Site footer</p></footer></body></html>""")
    text = output.units[0].markdown
    assert "Who can apply?" in text
    assert "Families registered with the municipality." in text
    assert "Site menu" not in text and "Site footer" not in text


def test_empty_main_does_not_discard_static_content_elsewhere(convert_html):
    output = convert_html("""<html><body><main><script>Empty shell</script></main>
      <div><h1>FAQ</h1><h2>Where do I register?</h2>
        <div hidden><p>Visit the town hall.</p></div></div></body></html>""")
    assert "Visit the town hall." in output.units[0].markdown


def test_multiple_and_nested_landmarks_keep_all_content_once(convert_html):
    output = convert_html("""<html><body><header>Site menu</header>
      <div role="main"><main><h1>Registration</h1><p>Bring an identity card.</p></main></div>
      <main><h2>Payment</h2><p>Payment is free.</p></main>
      <footer>Newsletter</footer></body></html>""")
    text = output.units[0].markdown
    assert text.count("Bring an identity card.") == 1
    assert text.count("Payment is free.") == 1
    assert "Site menu" not in text and "Newsletter" not in text


def test_article_fallback_keeps_all_articles_and_ignores_page_sidebar(convert_html):
    output = convert_html("""<html><body>
      <div><article><h1>Service A</h1><p>Register on Monday.</p></article></div>
      <article><h2>Service B</h2><p>Register on Tuesday.</p></article>
      <aside><p>Site advertisements</p></aside></body></html>""")
    text = output.units[0].markdown
    assert "Register on Monday." in text and "Register on Tuesday." in text
    assert "Site advertisements" not in text


def test_normalization_keeps_metadata_and_does_not_treat_generic_contacts_as_widgets():
    normalized = conversion._normalize_html(b"""<html><head><title>Service FAQ</title>
      <meta name="date" content="2026-10-09"></head><body><main>
      <h1>Contact</h1><div class="contact"><p>Call 0800 1234.</p></div>
      <aside><p>Bring proof of income.</p></aside></main></body></html>""")
    metadata = extract_html_metadata(normalized)
    assert metadata.title == "Service FAQ" and metadata.date == "2026-10-09"
    assert "Call 0800 1234." in normalized.decode()
    assert "Bring proof of income." in normalized.decode()


def test_nested_navigation_and_widgets_are_removed_without_touching_faq(convert_html):
    output = convert_html("""<html><body><main>
      <nav><div role="navigation"><nav>Nested menu</nav></div></nav>
      <div role="dialog"><div class="cookie-banner">Nested widget</div></div>
      <h1>Registration</h1><details><summary>What do I need?</summary>
      <p>Bring your registration card.</p></details>
      </main></body></html>""")
    text = output.units[0].markdown
    assert "What do I need?" in text
    assert "Bring your registration card." in text
    assert "Nested" not in text


def test_cleaned_article_keeps_introductory_text_before_first_heading(convert_html):
    output = convert_html("""<html><body><header>Site menu</header><main><article>
      <header><p>Financial assistance for families</p><h1>Emergency grants</h1></header>
      <p>Apply at the town hall.</p></article></main></body></html>""")
    text = output.units[0].markdown
    assert "Financial assistance for families" in text
    assert "Emergency grants" in text and "Apply at the town hall." in text
    assert "Site menu" not in text


def test_interactive_form_fields_are_not_public_service_content(convert_html):
    output = convert_html("""<html><body><main><article>
      <h1>Financial support</h1><p>Send the application to your local office.</p>
      <form><h2>Your contact details</h2><p>Enter your postcode here.</p>
        <label>Postcode</label><input name="postcode">
        <select><option>Your city</option></select><textarea>Message</textarea>
        <button>Send online</button>
      </form>
      <h2>Who can apply?</h2><div hidden><p>Households with low income can apply.</p></div>
      </article></main></body></html>""")
    text = output.units[0].markdown
    assert "Send the application to your local office." in text
    assert "Households with low income can apply." in text
    for excluded in ("Your contact details", "Enter your postcode", "Your city", "Send online"):
        assert excluded not in text


def test_static_content_in_form_without_interactive_fields_is_not_removed(convert_html):
    output = convert_html("""<html><body><form><h1>Registration guide</h1>
      <p>Bring an identity card.</p><dl><dt><button>Where?</button></dt>
      <dd hidden>Visit the town hall.</dd></dl></form></body></html>""")
    text = output.units[0].markdown
    assert "Bring an identity card." in text and "Visit the town hall." in text


def test_content_images_are_neither_fetched_nor_read(convert_html, monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("HTML conversion attempted to connect to a network resource")

    def no_ocr(*_args, **_kwargs):
        pytest.fail("HTML conversion attempted to OCR a content image")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(DoclingConverter, "_converter", no_ocr)
    output = convert_html(
        '<main><p>Before payment.</p><img src="Images/amounts.png" alt="">'
        '<img src="photo.jpg" alt="Volunteers at a tent"><p>After payment.</p></main>'
    )

    text = output.units[0].markdown
    assert "Before payment." in text and "After payment." in text
    assert "OCR" not in text
    assert output.limitations == []


@pytest.mark.parametrize("landmark", ["main", "article", 'div role="main"'])
def test_image_only_main_content_fails_and_asks_for_the_images(convert_html, landmark):
    closing = landmark.split()[0]
    with pytest.raises(SourceError) as error:
        convert_html(
            f'<html><body><{landmark}><img src="Images/amounts.png"></{closing}>'
            "<aside>Unrelated advertisement</aside></body></html>"
        )

    assert error.value.code == "no_text"
    assert "content appears to be images" in error.value.message
    assert "Add the image addresses directly as sources" in error.value.message


@pytest.mark.parametrize(
    "image",
    [
        '<img src="logo.svg">',
        '<img src="background.png" role="presentation">',
        '<img src="data:image/svg+xml,%3Csvg%3E">',
    ],
)
def test_decorative_only_pages_keep_the_generic_no_text_message(convert_html, image):
    with pytest.raises(SourceError, match="little or no readable text"):
        convert_html(f"<main>{image}</main>")
