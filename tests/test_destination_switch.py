"""Tests for choosing where a command's upload goes.

``/leech`` and ``/ytdl`` keep their names and their behaviour; what is new is
that the destination is a setting with a per-command override. These drive the
real listeners -- constructed, not stubbed -- because the thing most likely to
break is the *order* the two halves are applied in: a leech's own flags are
written before the destination is settled, and the two options that only a
telegram upload can honour are then turned off, which only works if the
destination is decided last.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.core.config_manager import Config
from bot.helper.util.help_messages import LEECH_HELP_DICT, YT_HELP_DICT, help_string
from bot.helper.util.task_args import (
    parse_leech_args,
    parse_ytdlp_args,
    strip_link_tokens,
)
from bot.modules.leech import Leech
from bot.modules.ytdlp import YtDlp

DEST = -1001234567890


def command_message(text="/leech http://example.com/a.mkv"):
    """The message fields ``TaskConfig.__init__`` reads off a real command."""
    return SimpleNamespace(
        id=7,
        text=text,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            id=DEST, type=SimpleNamespace(name="SUPERGROUP"), is_admin=True
        ),
    )


def leech(*tokens):
    listener = Leech(SimpleNamespace(), command_message())
    listener._apply_args(parse_leech_args(list(tokens)))
    return listener


def ytdlp(*tokens):
    listener = YtDlp(SimpleNamespace(), command_message("/ytdl http://example.com/v"))
    listener._apply_args(parse_ytdlp_args(list(tokens)))
    return listener


# ── the flag beats the setting ──────────────────────────────────────


@pytest.mark.parametrize("configured", ["tg", "s3"])
def test_without_a_flag_the_configured_destination_stands(monkeypatch, configured):
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", configured)

    assert leech("http://example.com/a.mkv").destination == configured


def test_s3_overrides_a_telegram_default(monkeypatch):
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", "tg")

    assert leech("http://example.com/a.mkv", "-s3").destination == "s3"


def test_tg_overrides_a_bucket_default(monkeypatch):
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", "s3")

    assert leech("http://example.com/a.mkv", "-tg").destination == "tg"


def test_giving_both_flags_takes_the_bucket(monkeypatch):
    """A rule that can be stated beats one that depends on token order."""
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", "tg")

    assert leech("http://example.com/a.mkv", "-tg", "-s3").destination == "s3"


def test_the_flag_survives_into_the_children_of_a_bulk():
    """A child re-parses the parent's option string, so ``-s3`` has to be in it."""
    options = " ".join(strip_link_tokens(["http://example.com/a.mkv", "-s3", "-z"]))

    assert options == "-s3 -z"
    assert parse_leech_args(["http://example.com/b.mkv", *options.split()]).is_s3


def test_ytdlp_honours_the_same_flags(monkeypatch):
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", "tg")

    assert ytdlp("http://example.com/v", "-s3").destination == "s3"


# ── the flags are booleans, not a value slot ────────────────────────


@pytest.mark.parametrize(
    "parse, flag, attr",
    [
        (parse_leech_args, "-s3", "is_s3"),
        (parse_leech_args, "-tg", "is_tg"),
        (parse_ytdlp_args, "-s3", "is_s3"),
        (parse_ytdlp_args, "-tg", "is_tg"),
    ],
)
def test_a_flag_with_a_link_after_it_is_still_a_boolean(parse, flag, attr):
    """``-s3 http://x`` must not read the link as the flag's value."""
    assert getattr(parse([flag, "http://example.com/a.mkv"]), attr) is True


def test_neither_flag_leaves_a_link_alone():
    args = parse_leech_args(["http://example.com/a.mkv", "-s3"])

    assert args.link == "http://example.com/a.mkv"
    assert args.is_tg is False


# ── the options a bucket cannot honour ──────────────────────────────


def test_stream_upload_alone_is_kept():
    assert leech("http://example.com/a.mkv", "-su").stream_upload is True


def test_a_bucket_task_does_not_stream_through_telegram():
    """``-su`` picks a telegram uploader mid-download, before this is decided."""
    assert leech("http://example.com/a.mkv", "-su", "-s3").stream_upload is False


def test_the_telegram_destination_keeps_stream_upload():
    """The guard keys off the destination, not off the presence of a flag."""
    assert leech("http://example.com/a.mkv", "-su", "-tg").stream_upload is True


def test_a_screenshot_run_is_dropped_for_a_bucket_task():
    """``-ss`` moves the download into a folder the completion link misses."""
    assert leech("http://example.com/a.mkv", "-ss", "-s3").screen_shots is False


def test_screenshots_are_kept_for_telegram():
    assert leech("http://example.com/a.mkv", "-ss").screen_shots is True


# ── the help screen ─────────────────────────────────────────────────


@pytest.mark.parametrize("help_dict", [LEECH_HELP_DICT, YT_HELP_DICT])
def test_both_commands_document_the_destination_option(help_dict):
    """Both commands take the flags, so both have to say so."""
    entry = help_dict["Destination"]

    assert "-s3" in entry
    assert "-tg" in entry
    # the options a bucket cannot honour are named where they are dropped
    assert "stream uploader" in entry or "-su" in entry


def test_the_command_list_mentions_the_bucket():
    assert "-s3" in help_string
