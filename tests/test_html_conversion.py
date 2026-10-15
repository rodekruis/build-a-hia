"""Local static HTML regressions; no model downloads, rendering or network access."""

import socket
from types import SimpleNamespace

import pytest
from factories import make_png

from build_a_hia.services import conversion
from build_a_hia.services.chunking import ConvertedUnit
from build_a_hia.services.conversion import ConversionOutput, DoclingConverter
from build_a_hia.services.html_meta import extract_html_metadata
from build_a_hia.services.inspection import SourceError, SourceFormat
from build_a_hia.services.url_fetch import FetchResult, HttpResponse, SafeFetcher


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


@pytest.fixture
def embedded_ocr(tmp_path, monkeypatch):
    state = SimpleNamespace(
        responses={},
        fetches=[],
        ocr=[],
        text="Payment amount: 250 TL. Apply at the town hall.",
        error=None,
        limitations=[],
    )
    original = DoclingConverter._convert_whole

    def convert_whole(self, path, source_format, progress, base_url, ocr_language, **kwargs):
        if source_format == SourceFormat.HTML:
            return original(self, path, source_format, progress, base_url, ocr_language, **kwargs)
        state.ocr.append((source_format, ocr_language, kwargs.get("timeout")))
        assert path.exists()
        if state.error:
            raise state.error
        return ConversionOutput(
            units=[ConvertedUnit(page=1, markdown=state.text)],
            limitations=list(state.limitations),
        )

    monkeypatch.setattr(DoclingConverter, "_convert_whole", convert_whole)

    def fetch(url, max_bytes, timeout):
        state.fetches.append((url, max_bytes, timeout))
        data = state.responses.get(url, make_png((600, 400)))
        if isinstance(data, SourceError):
            raise data
        return FetchResult(url, "image/png", data, "")

    def run(html, *, language="ar", clock=lambda: 0.0, image_fetcher=fetch):
        path = tmp_path / "page.html"
        path.write_text(html, encoding="utf-8")
        converter = DoclingConverter(image_fetcher=image_fetcher, clock=clock)
        output = converter.convert(
            path,
            SourceFormat.HTML,
            lambda *_args: None,
            base_url="https://example.org/program/page.html",
            ocr_language=language,
        )
        assert not list(tmp_path.glob("hia-image-*"))
        return output

    state.run = run
    return state


@pytest.mark.parametrize("landmark", ["main", "article", 'div role="main"'])
def test_image_only_landmarks_are_preserved_and_ocrd(embedded_ocr, landmark):
    closing = landmark.split()[0]
    output = embedded_ocr.run(
        f'<html><body><{landmark}><img src="Images/amounts.png"></{closing}>'
        "<aside>Unrelated advertisement</aside></body></html>"
    )

    text = output.units[0].markdown
    assert "250 TL" in text
    assert "Unrelated advertisement" not in text
    assert "Text read from image using OCR" in text
    assert "https://example.org/program/Images/amounts.png" in text
    assert embedded_ocr.ocr == [(SourceFormat.PNG, "ar", conversion.HTML_IMAGE_OCR_TIMEOUT)]


def test_embedded_ocr_preserves_order_and_deduplicates_images(embedded_ocr):
    output = embedded_ocr.run(
        '<main><p>Before payment.</p><img src="amounts.png">'
        '<p>After payment.</p><img src="./amounts.png"></main>'
    )
    text = output.units[0].markdown
    assert text.index("Before payment.") < text.index("250 TL") < text.index("After payment.")
    assert text.count("250 TL") == 2
    assert len(embedded_ocr.fetches) == len(embedded_ocr.ocr) == 1


@pytest.mark.parametrize(
    ("size", "read"),
    [((100, 400), True), ((200, 200), True), ((99, 500), False), ((199, 200), False)],
)
def test_embedded_image_size_policy_uses_actual_pixels(embedded_ocr, size, read):
    embedded_ocr.responses["https://example.org/program/amounts.png"] = make_png(size)
    output = embedded_ocr.run(
        '<main><p>Native information.</p><img src="amounts.png" width="1" height="1"></main>'
    )
    assert bool(embedded_ocr.ocr) is read
    assert ("250 TL" in output.units[0].markdown) is read
    assert output.limitations == []


def test_decorative_images_and_site_chrome_are_not_downloaded(embedded_ocr):
    output = embedded_ocr.run(
        '<html><body><nav><img src="menu.png"></nav><main>'
        '<p>Native information.</p><img src="logo.svg"><img src="ico-f.png">'
        '<img src="large.png" class="service-logo"><img src="background.png" role="presentation">'
        '<img src="hidden.png" aria-hidden="true"><img src="amounts.png" alt="">'
        "</main></body></html>"
    )
    assert [url for url, *_ in embedded_ocr.fetches] == ["https://example.org/program/amounts.png"]
    assert "250 TL" in output.units[0].markdown


@pytest.mark.parametrize(
    "attribute",
    ['data-src="amounts.png" src="placeholder.png"', 'srcset="small.png 1x, amounts.png 2x"'],
)
def test_lazy_and_responsive_image_addresses_are_resolved(embedded_ocr, attribute):
    embedded_ocr.run(f"<main><img {attribute}></main>")
    assert embedded_ocr.fetches[0][0] == "https://example.org/program/amounts.png"


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/secret", "file:///secret.png", "http://[invalid", ""],
)
def test_invalid_image_addresses_warn_without_fetching(embedded_ocr, url):
    output = embedded_ocr.run(f'<main><p>Native information.</p><img src="{url}"></main>')
    assert embedded_ocr.fetches == []
    assert "Native information." in output.units[0].markdown
    assert any("Could not read" in note for note in output.limitations)


@pytest.mark.parametrize(
    "data",
    [b"not an image", SourceError("not_found", "The image was not found.")],
)
def test_unreadable_content_images_keep_native_text_with_warnings(embedded_ocr, data):
    embedded_ocr.responses["https://example.org/program/amounts.png"] = data
    output = embedded_ocr.run('<main><p>Native information.</p><img src="amounts.png"></main>')

    assert "Native information." in output.units[0].markdown
    assert any("Could not read content image" in note for note in output.limitations)
    assert embedded_ocr.ocr == []


def test_photos_without_text_do_not_warn_and_temporary_images_are_cleaned(embedded_ocr, tmp_path):
    embedded_ocr.error = SourceError("no_text", "No readable text was found in this image.")
    output = embedded_ocr.run('<main><p>Native information.</p><img src="amounts.png"></main>')

    assert output.limitations == []
    assert not list(tmp_path.glob("hia-image-*"))
    with pytest.raises(SourceError, match="little or no readable text"):
        embedded_ocr.run('<main><img src="amounts.png"></main>')


def test_inline_svg_placeholders_do_not_warn_or_consume_image_budget(embedded_ocr):
    placeholders = '<img src="data:image/svg+xml,%3Csvg%20viewBox=0%200%201024%20681%3E">' * 20
    output = embedded_ocr.run(f"<main><p>Native information.</p>{placeholders}</main>")

    assert embedded_ocr.fetches == []
    assert embedded_ocr.ocr == []
    assert output.limitations == []
    assert "Native information." in output.units[0].markdown


@pytest.mark.parametrize(
    "attribute",
    ['data-lazy-src="amounts.png"', 'data-lazy-srcset="small.png 1x, amounts.png 2x"'],
)
def test_lazy_raster_images_are_read_instead_of_their_svg_placeholder(embedded_ocr, attribute):
    output = embedded_ocr.run(f'<main><img src="data:image/svg+xml,%3Csvg%3E" {attribute}></main>')

    assert embedded_ocr.fetches[0][0] == "https://example.org/program/amounts.png"
    assert "250 TL" in output.units[0].markdown
    assert output.limitations == []


def test_short_successful_web_image_ocr_does_not_add_routine_warnings(embedded_ocr):
    embedded_ocr.text = "Short caption."
    output = embedded_ocr.run('<main><img src="photo.jpg"></main>')

    assert "Short caption." in output.units[0].markdown
    assert output.limitations == []


def test_real_partial_image_conversion_still_warns(embedded_ocr):
    embedded_ocr.limitations = ["Part of this file could not be converted."]
    output = embedded_ocr.run('<main><img src="amounts.png"></main>')

    assert any("Part of this file could not be converted" in note for note in output.limitations)


def test_image_ocr_text_is_inserted_as_data_not_html(embedded_ocr):
    embedded_ocr.text = "<script>not executable</script>\nPayment amount: 250 TL."
    output = embedded_ocr.run('<main><img src="amounts.png"></main>')

    assert "250 TL" in output.units[0].markdown
    assert "not executable" in output.units[0].markdown


def test_image_processing_limits_warn_and_keep_available_text(embedded_ocr, monkeypatch):
    monkeypatch.setattr(conversion, "MAX_HTML_IMAGES", 1)
    output = embedded_ocr.run('<main><img src="first.png"><img src="second.png"></main>')

    assert len(embedded_ocr.fetches) == 1
    assert "250 TL" in output.units[0].markdown
    assert any("1 content image(s) were not read" in note for note in output.limitations)


def test_image_download_allowance_is_shared_and_bounded(embedded_ocr, monkeypatch):
    data = make_png((600, 400))
    monkeypatch.setattr(conversion, "MAX_HTML_IMAGE_TOTAL_BYTES", len(data))
    output = embedded_ocr.run('<main><img src="first.png"><img src="second.png"></main>')

    assert embedded_ocr.fetches[0][1] == len(data)
    assert len(embedded_ocr.fetches) == 1
    assert any("download or processing-time limit" in note for note in output.limitations)


def test_over_limit_image_data_is_rejected_before_ocr(embedded_ocr, monkeypatch):
    monkeypatch.setattr(conversion, "MAX_HTML_IMAGE_BYTES", 1)
    output = embedded_ocr.run('<main><p>Native information.</p><img src="amounts.png"></main>')

    assert embedded_ocr.fetches[0][1] == 1
    assert embedded_ocr.ocr == []
    assert any("download limit" in note for note in output.limitations)


def test_failed_downloads_conservatively_consume_the_shared_allowance(embedded_ocr, monkeypatch):
    monkeypatch.setattr(conversion, "MAX_HTML_IMAGE_TOTAL_BYTES", 100)
    embedded_ocr.responses["https://example.org/program/first.png"] = SourceError(
        "timeout", "The image download timed out."
    )
    output = embedded_ocr.run(
        '<main><p>Native information.</p><img src="first.png"><img src="second.png"></main>'
    )

    assert len(embedded_ocr.fetches) == 1
    assert any("timed out" in note for note in output.limitations)
    assert any("1 content image(s) were not read" in note for note in output.limitations)


def test_image_processing_deadline_prevents_additional_downloads(embedded_ocr):
    times = iter([0.0, conversion.HTML_IMAGE_TIME_BUDGET])
    output = embedded_ocr.run(
        '<main><p>Native information.</p><img src="amounts.png"></main>',
        clock=lambda: next(times),
    )

    assert embedded_ocr.fetches == []
    assert any("processing-time limit" in note for note in output.limitations)


def test_image_redirects_use_the_safe_fetcher_and_warn_when_blocked(embedded_ocr):
    connected = []

    def connector(target, _address, _timeout):
        connected.append(target.url)
        return HttpResponse(
            302, {"location": "http://169.254.169.254/secret"}, iter([]), lambda: None
        )

    def fetch(url, max_bytes, timeout):
        return SafeFetcher(
            max_bytes=max_bytes,
            max_redirects=3,
            timeout_seconds=timeout,
            resolver=lambda *_args: ["93.184.216.34"],
            connector=connector,
        ).fetch(url)

    output = embedded_ocr.run(
        '<main><p>Native information.</p><img src="amounts.png"></main>',
        image_fetcher=fetch,
    )

    assert connected == ["https://example.org/program/amounts.png"]
    assert any("Only public websites" in note for note in output.limitations)
    assert embedded_ocr.ocr == []
