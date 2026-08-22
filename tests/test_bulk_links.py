"""Tests for the ``-b start:end`` range.

The range is 1-based and inclusive -- it counts links the way the person reading
the replied-to message counts lines. It used to be handed straight to a Python
slice, which shifted the window forward by one *and* dropped the last link, so
``-b 3401:3500`` quietly ran 99 links starting from the 3402nd. What is pinned
here is the arithmetic on both ends, plus the open-ended forms that must keep
behaving as they always did.
"""

from __future__ import annotations

import pytest

from bot.helper.ext_utils.bulk_links import extract_bulk_links, filter_links

LINKS = [f"link{i}" for i in range(1, 4001)]


class _Reply:
    document = None

    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, text):
        self.reply_to_message = _Reply(text)


@pytest.mark.parametrize(
    "start,end,count,first,last",
    [
        # the report: 3401 through 3500 is 100 links, not 99 from the 3402nd
        (3401, 3500, 100, "link3401", "link3500"),
        (1, 1, 1, "link1", "link1"),
        (1, 100, 100, "link1", "link100"),
        (2, 5, 4, "link2", "link5"),
    ],
)
def test_range_is_one_based_and_inclusive(start, end, count, first, last):
    picked = filter_links(LINKS, start, end)

    assert len(picked) == count
    assert picked[0] == first
    assert picked[-1] == last


def test_start_only_includes_the_start_link():
    picked = filter_links(LINKS, 3401, 0)

    assert picked[0] == "link3401"
    assert picked[-1] == "link4000"
    assert len(picked) == 600


def test_end_only_counts_from_the_first_link():
    """``-b :100`` already meant "the first 100" and must not shift."""
    picked = filter_links(LINKS, 0, 100)

    assert picked[0] == "link1"
    assert picked[-1] == "link100"
    assert len(picked) == 100


def test_no_bounds_takes_everything():
    assert filter_links(LINKS, 0, 0) == LINKS


def test_out_of_range_end_is_not_an_error():
    """A hand-typed end past the last link takes what exists."""
    picked = filter_links(LINKS, 3999, 9999)

    assert picked == ["link3999", "link4000"]


def test_start_past_the_end_is_empty():
    """``init_bulk`` reports "Bulk Empty!" on this -- it must not raise here."""
    assert filter_links(LINKS, 5000, 6000) == []


async def test_string_args_from_the_parser_survive_the_shift():
    """``parse_leech_args`` hands ``-b`` through as text, never as int."""
    message = _Message("\n".join(LINKS))

    picked = await extract_bulk_links(message, "3401", "3500")

    assert len(picked) == 100
    assert picked[0] == "link3401"
    assert picked[-1] == "link3500"
