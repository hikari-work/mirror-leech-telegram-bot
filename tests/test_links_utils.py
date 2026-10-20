"""Tests for :func:`extract_urls`, which feeds telegram links mode.

The messages such a scan reads are prose: a link may end at a full stop, sit
inside parentheses, or be repeated across several posts. Every one of those
cases decides whether a real download starts, so the boundaries are pinned
here rather than guessed at by the caller.
"""

from __future__ import annotations

from bot.helper.util.links_utils import extract_urls


def test_urls_keep_the_order_they_appeared_in():
    text = "first https://a.test/1 then https://b.test/2"

    assert extract_urls(text) == ["https://a.test/1", "https://b.test/2"]


def test_duplicates_are_dropped_first_seen_wins():
    text = "https://a.test/1 and again https://a.test/1"

    assert extract_urls(text) == ["https://a.test/1"]


def test_sentence_punctuation_is_not_part_of_the_url():
    text = (
        "See https://a.test/file.mkv. Also (https://b.test/x), "
        "and https://c.test/y!"
    )

    assert extract_urls(text) == [
        "https://a.test/file.mkv",
        "https://b.test/x",
        "https://c.test/y",
    ]


def test_balanced_parentheses_are_part_of_the_url():
    text = "https://en.wikipedia.org/wiki/Foo_(bar)"

    assert extract_urls(text) == [text]


def test_none_and_empty_text_have_no_urls():
    assert extract_urls(None) == []
    assert extract_urls("") == []


def test_only_http_and_https_are_extracted():
    text = "ftp://a.test/1 magnet:?xt=urn:btih:abc www.a.test"

    assert extract_urls(text) == []
