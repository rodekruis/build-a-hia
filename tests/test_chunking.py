from build_a_hia.services.chunking import ConvertedUnit, build_chunks, estimate_tokens


def test_chunk_ids_carry_source_and_page():
    chunks = build_chunks(
        3,
        [
            ConvertedUnit(page=12, markdown="First paragraph.\n\nSecond paragraph."),
            ConvertedUnit(page=13, markdown="Next page."),
        ],
    )

    assert [chunk.label for chunk in chunks] == ["[S3-p12-c1]", "[S3-p13-c1]"]
    assert chunks[0].text == "First paragraph.\n\nSecond paragraph."
    assert chunks[0].page == 12


def test_long_pages_are_split_into_numbered_chunks():
    paragraph = "word " * 100
    chunks = build_chunks(1, [ConvertedUnit(page=2, markdown="\n\n".join([paragraph] * 3))], 1100)

    assert [chunk.id for chunk in chunks] == ["S1-p2-c1", "S1-p2-c2"]
    assert all(len(chunk.text) <= 1100 for chunk in chunks)


def test_oversized_paragraph_is_cut():
    chunks = build_chunks(1, [ConvertedUnit(page=None, markdown="x" * 2500)], 1000)

    assert [chunk.id for chunk in chunks] == ["S1-c1", "S1-c2", "S1-c3"]


def test_sheets_use_sheet_labels():
    chunks = build_chunks(2, [ConvertedUnit(page=1, markdown="| a |", sheet="Prices")])

    assert chunks[0].id == "S2-s1-c1"
    assert chunks[0].sheet == "Prices"


def test_empty_units_produce_no_chunks():
    assert build_chunks(1, [ConvertedUnit(page=1, markdown="  \n\n ")]) == []


def test_token_estimate_is_conservative_for_non_latin_scripts():
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("مرحبا") == 4
