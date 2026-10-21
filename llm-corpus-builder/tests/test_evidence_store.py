import pytest

from evidence.store import (
    Anchor,
    Block,
    BlockIndex,
    block_id,
    canonical_url,
    doc_uid,
    match_key,
    normalize_text,
    read_jsonl,
    resolve_anchor,
    write_jsonl,
)


# --------------------------------------------------------------------- normalize_text


def test_normalize_text_unifies_quotes_dashes_and_fullwidth_space():
    assert normalize_text("“Hello”　world – test") == '"Hello" world - test'


def test_normalize_text_collapses_whitespace_and_strips():
    assert normalize_text("  a\n\n b\t\tc  ") == "a b c"


def test_normalize_text_handles_none_and_non_string():
    assert normalize_text(None) == ""
    assert normalize_text(123) == "123"


def test_normalize_text_removes_zero_width_characters():
    # zero-width space / BOM / ZWJ / ZWNJ should disappear entirely
    assert normalize_text("a​b﻿c‍d‌e") == "abcde"


# --------------------------------------------------------------------- match_key


def test_match_key_strips_punctuation_and_casefolds():
    assert match_key("Hello, World!") == "helloworld"


def test_match_key_treats_full_and_half_width_equal():
    assert match_key("ABC－123") == match_key("ABC-123")


def test_match_key_of_empty_is_empty():
    assert match_key("   ") == ""
    assert match_key(None) == ""


# --------------------------------------------------------------------- canonical_url


def test_canonical_url_strips_scheme_www_trailing_slash_and_fragment():
    assert canonical_url("https://www.example.com/path/") == "example.com/path"
    assert canonical_url("http://example.com/path#frag") == "example.com/path"


def test_canonical_url_is_casefolded_on_host_only():
    assert canonical_url("HTTPS://Example.COM/Path") == "example.com/Path"


def test_canonical_url_of_bare_host():
    assert canonical_url("https://example.com") == "example.com"
    assert canonical_url("https://example.com/") == "example.com"


def test_canonical_url_equal_for_http_and_https_variants():
    a = canonical_url("http://www.zjui.edu.cn/news/1")
    b = canonical_url("https://zjui.edu.cn/news/1/")
    assert a == b == "zjui.edu.cn/news/1"


# --------------------------------------------------------------------- doc_uid


def test_doc_uid_stable_for_equivalent_urls():
    assert doc_uid("https://www.example.com/a/") == doc_uid("http://example.com/a")


def test_doc_uid_has_expected_prefix_and_length():
    uid = doc_uid("https://example.com/a")
    assert uid.startswith("d_")
    assert len(uid) == len("d_") + 16


def test_doc_uid_for_path_normalizes_backslashes():
    assert doc_uid("zju-sites/a/b.pdf") == doc_uid("zju-sites\\a\\b.pdf")


def test_doc_uid_differs_for_different_sources():
    assert doc_uid("https://example.com/a") != doc_uid("https://example.com/b")


# --------------------------------------------------------------------- block_id


def test_block_id_stable_across_whitespace_and_case_variants():
    doc = "d_abc"
    assert block_id(doc, "Hello World") == block_id(doc, "hello   world")


def test_block_id_includes_doc_prefix():
    assert block_id("d_abc", "text").startswith("d_abc:")


def test_block_id_salt_changes_id_for_duplicate_text():
    doc = "d_abc"
    first = block_id(doc, "Same text", salt=0)
    second = block_id(doc, "Same text", salt=1)
    assert first != second
    assert first.split(":")[0] == second.split(":")[0] == doc


# --------------------------------------------------------------------- jsonl round trip


def test_write_and_read_jsonl_round_trip(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = [{"a": 1, "text": "中文"}, {"a": 2, "text": "line2"}]
    count = write_jsonl(path, rows)
    assert count == 2
    loaded = list(read_jsonl(path))
    assert loaded == rows


def test_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a": 1}\n\n   \n{"a": 2}\n', encoding="utf-8")
    assert list(read_jsonl(path)) == [{"a": 1}, {"a": 2}]


# --------------------------------------------------------------------- resolve_anchor


def _index(texts, doc="d_doc1"):
    blocks = [
        {"doc_uid": doc, "block_id": f"{doc}:{i}", "ordinal": i, "text": text}
        for i, text in enumerate(texts)
    ]
    return BlockIndex(blocks)


def test_resolve_anchor_exact_match_within_one_block():
    index = _index(["The deadline is September 1st.", "Other paragraph."])
    result = resolve_anchor(index, {"doc_uid": "d_doc1", "quote": "deadline is September 1st"})
    assert result["status"] == "exact"
    assert result["block_ids"] == ["d_doc1:0"]
    assert result["score"] == 1.0


def test_resolve_anchor_accepts_anchor_dataclass():
    index = _index(["Hello world block text here."])
    anchor = Anchor(doc_uid="d_doc1", quote="Hello world")
    result = resolve_anchor(index, anchor)
    assert result["status"] == "exact"


def test_resolve_anchor_missing_doc():
    index = _index(["Some text"], doc="d_doc1")
    result = resolve_anchor(index, {"doc_uid": "d_other", "quote": "Some text"})
    assert result["status"] == "missing_doc"
    assert result["block_ids"] == []
    assert result["score"] == 0.0


def test_resolve_anchor_empty_quote():
    index = _index(["Some text"], doc="d_doc1")
    result = resolve_anchor(index, {"doc_uid": "d_doc1", "quote": "   "})
    assert result["status"] == "empty_quote"


def test_resolve_anchor_span_across_consecutive_blocks():
    index = _index([
        "The committee will meet to discuss the",
        "budget proposal for next year.",
        "Unrelated paragraph.",
    ])
    result = resolve_anchor(index, {"doc_uid": "d_doc1", "quote": "discuss the budget proposal"})
    assert result["status"] == "span"
    assert result["block_ids"] == ["d_doc1:0", "d_doc1:1"]


def test_resolve_anchor_fuzzy_match_for_slightly_altered_quote():
    stored = (
        "The registration form must be submitted to the registrar office before the posted "
        "deadline for this semester and late submissions will not be accepted under any circumstances."
    )
    index = _index([stored])
    # one word changed near the start ("registration" -> "application"); the long matching
    # suffix keeps coverage above the resolver's 0.5 threshold.
    quote = (
        "The application form must be submitted to the registrar office before the posted "
        "deadline for this semester and late submissions will not be accepted under any circumstances."
    )
    result = resolve_anchor(index, {"doc_uid": "d_doc1", "quote": quote})
    assert result["status"] == "fuzzy"
    assert result["block_ids"] == ["d_doc1:0"]
    assert 0.86 <= result["score"] <= 1.0


def test_resolve_anchor_unresolved_when_quote_not_present():
    index = _index(["Totally unrelated content about weather."])
    result = resolve_anchor(index, {"doc_uid": "d_doc1", "quote": "completely different subject matter text here"})
    assert result["status"] == "unresolved"
    assert result["block_ids"] == []


def test_block_and_anchor_to_json_drop_empty_fields():
    block = Block(block_id="b1", doc_uid="d1", ordinal=0, kind="paragraph", text="hi")
    data = block.to_json()
    assert "heading_path" not in data
    assert "page" not in data
    assert data["text"] == "hi"

    anchor = Anchor(doc_uid="d1", quote="hi")
    assert anchor.to_json() == {"doc_uid": "d1", "quote": "hi", "role": "support"}


def test_resolve_anchor_fuzzy_with_centered_substitution():
    index = BlockIndex([{"block_id": "b1", "doc_uid": "d", "ordinal": 0, "text": "Students must submit the registration form before the deadline."}])
    result = resolve_anchor(index, {"doc_uid": "d", "quote": "Students must submit the application form before the deadline."})
    assert result["status"] == "fuzzy"
    assert result["block_ids"] == ["b1"]
