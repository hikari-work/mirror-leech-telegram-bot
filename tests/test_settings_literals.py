"""Tests for `bot.modules.settings.literals`.

The settings screens used to `eval()` whatever a user typed. These tests pin
down the replacement: everything the bot documents as valid still parses, and
anything that would have *executed* is refused.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

# `literals` imports nothing from the bot package, so load it straight from its
# path: going through `bot.modules.settings` would drag in every handler module
# and need a live Telegram/DB environment to import at all.
_PATH = (
    Path(__file__).resolve().parents[1] / "bot/modules/settings/literals.py"
)
_spec = importlib.util.spec_from_file_location("settings_literals", _PATH)
literals = importlib.util.module_from_spec(_spec)
sys.modules["settings_literals"] = literals
_spec.loader.exec_module(literals)

parse_dict = literals.parse_dict
parse_literal = literals.parse_literal

# The YT_DLP_OPTIONS example from help_messages.py, verbatim. If this stops
# parsing, the bot's own documentation has become a lie.
DOCUMENTED_YT_DLP = (
    '{"format": "bv*+mergeall[vcodec=none]", "nocheckcertificate": True, '
    '"playliststart": 10, "fragment_retries": float("inf"), "matchtitle": "S13", '
    '"writesubtitles": True, "live_from_start": True, '
    '"postprocessor_args": {"ffmpeg": ["-threads", "4"]}, '
    '"wait_for_video": (5, 100), '
    '"download_ranges": [{"start_time": 0, "end_time": 10}]}'
)


def test_documented_yt_dlp_options_example_parses():
    value = parse_literal(DOCUMENTED_YT_DLP)
    assert value["format"] == "bv*+mergeall[vcodec=none]"
    assert value["nocheckcertificate"] is True
    assert value["playliststart"] == 10
    assert value["fragment_retries"] == math.inf
    assert value["postprocessor_args"] == {"ffmpeg": ["-threads", "4"]}
    assert value["wait_for_video"] == (5, 100)
    assert value["download_ranges"] == [{"start_time": 0, "end_time": 10}]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{}", {}),
        ("[]", []),
        ('{"a": 1}', {"a": 1}),
        ("[1, 2, 3]", [1, 2, 3]),
        ("[-1, 2.5]", [-1, 2.5]),
        ('{"a": None, "b": False}', {"a": None, "b": False}),
        ('{"nested": {"deep": ["x"]}}', {"nested": {"deep": ["x"]}}),
        ('  {"a": 1}  ', {"a": 1}),
        ("{'single': 'quotes'}", {"single": "quotes"}),
    ],
)
def test_plain_literals(text, expected):
    assert parse_literal(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('float("inf")', math.inf),
        ('float("-inf")', -math.inf),
        ("float(5)", 5.0),
        ('float("2.5")', 2.5),
    ],
)
def test_float_call_is_folded(text, expected):
    assert parse_literal(text) == expected


def test_float_nan_is_folded():
    assert math.isnan(parse_literal('float("nan")'))


@pytest.mark.parametrize(
    "text",
    [
        '__import__("os").system("id")',
        'eval("1+1")',
        "open('/etc/passwd').read()",
        '{"x": __import__("os")}',
        '{"x": [1, exec("pass")]}',
        "int('3')",
        "str(1)",
        "print(1)",
    ],
)
def test_calls_other_than_float_are_rejected(text):
    with pytest.raises(ValueError):
        parse_literal(text)


@pytest.mark.parametrize(
    "text",
    [
        'float("inf", 2)',
        "float()",
        'float(x="inf")',
        'float("inf", base=2)',
        "float(True)",
        "float(None)",
        "float(1j)",
        'float(b"1")',
        '{"a": float([1])}',
    ],
)
def test_float_call_shape_is_enforced(text):
    with pytest.raises(ValueError):
        parse_literal(text)


def test_nested_float_calls_fold_inside_out():
    """Pointless but harmless: the inner call is a constant by the time the
    outer one is checked, so it folds too."""
    assert parse_literal('float(float("inf"))') == math.inf


def test_float_call_with_unparsable_argument_is_rejected():
    with pytest.raises(ValueError):
        parse_literal('float("not a number")')


@pytest.mark.parametrize(
    "text",
    [
        "Config",
        "Config.DATABASE_URL",
        '{"a": Config}',
        "[x for x in range(3)]",
        "lambda: 1",
    ],
)
def test_names_and_comprehensions_are_rejected(text):
    with pytest.raises(ValueError):
        parse_literal(text)


def test_bare_name_error_names_the_name_and_hides_the_ast():
    """`literal_eval` reports `Name(id='bad', ctx=Load())`; users see that text,
    so it is rewritten into the wording the old `eval` produced."""
    with pytest.raises(ValueError) as excinfo:
        parse_literal('{"a": bad}')
    message = str(excinfo.value)
    assert "name 'bad' is not defined" in message
    assert "Name(" not in message
    assert "ctx=" not in message


@pytest.mark.parametrize("text", ['{"a": 1', "", "   ", "}{", "1 +", "a b c"])
def test_syntax_errors_become_value_errors(text):
    # One exception type, because the callers report `str(e)` back to the user.
    with pytest.raises(ValueError):
        parse_literal(text)


def test_arithmetic_is_rejected():
    """`eval` accepted this; `literal_eval` does not, and that is the point."""
    with pytest.raises(ValueError):
        parse_literal("[1 + 2]")


def test_negative_numbers_still_work():
    """A unary minus is not arithmetic as far as `literal_eval` is concerned."""
    assert parse_literal("[-3]") == [-3]


def test_parse_dict_accepts_a_dict():
    assert parse_dict('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("text", ["{1, 2}", "[1]", '"str"', "5"])
def test_parse_dict_rejects_non_dicts(text):
    with pytest.raises(ValueError, match="must be dict"):
        parse_dict(text)


def test_parse_dict_error_names_the_actual_type():
    with pytest.raises(ValueError, match="got set"):
        parse_dict("{1, 2}")
