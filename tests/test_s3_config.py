"""Tests for the S3 settings as configuration values.

Three of these keys are read from ``config.py`` or written through ``/bsetting``,
so how they are stored matters as much as what they hold. The type of the
default is what a string typed into the settings screen is converted to, and a
key that is not in ``get_all()`` never reaches that screen at all.
"""

from __future__ import annotations

import pytest

from bot.core.config_manager import Config

S3_KEYS = (
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "S3_KEY_PREFIX",
    "S3_BROWSER_URL",
    "S3_MULTIPART_CONCURRENCY",
)
"""Everything the bucket uploader reads, by name."""


def test_every_setting_the_uploader_reads_exists():
    """A name that is not a configuration key cannot be set, only read as None."""
    for key in S3_KEYS:
        assert Config.get(key) is not None, key


def test_every_setting_is_offered_to_the_settings_screen():
    """/bsetting is built from this mapping, and only from it."""
    offered = Config.get_all()

    for key in S3_KEYS:
        assert key in offered, key


def test_a_bucket_is_not_the_default_destination():
    """An upgrade must not start uploading somewhere nobody configured."""
    assert Config.UPLOAD_DESTINATION == "tg"


def test_a_settings_screen_string_becomes_the_number_it_means(monkeypatch):
    monkeypatch.setattr(Config, "S3_MULTIPART_CONCURRENCY", 4)

    Config.set("S3_MULTIPART_CONCURRENCY", "8")

    assert Config.S3_MULTIPART_CONCURRENCY == 8


@pytest.mark.parametrize(
    "key, typed, stored",
    [
        ("S3_KEY_PREFIX", "/mirror/", "mirror"),
        ("S3_KEY_PREFIX", "mirror/10032/", "mirror/10032"),
        ("S3_BROWSER_URL", "http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
        (
            "S3_ENDPOINT_URL",
            "https://acct.r2.cloudflarestorage.com/",
            "https://acct.r2.cloudflarestorage.com",
        ),
    ],
)
def test_a_slash_at_either_end_is_not_part_of_the_value(key, typed, stored):
    """The uploader joins these with ``/``, and would otherwise double it."""
    assert Config._process_config_value(key, typed) == stored


def test_a_secret_is_left_exactly_as_it_was_given():
    """The slash-stripping is an allowlist, not a rule about strings."""
    assert Config._process_config_value("S3_SECRET_ACCESS_KEY", "/k+/") == "/k+/"
