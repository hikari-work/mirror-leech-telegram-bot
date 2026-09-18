"""What the ``-su`` help screen promises has to match what the mode does.

The flags are not written down twice: they are read from the component's own
table, so one added there without a word in the help -- which is what a user
reads before asking for it -- fails here instead of being dropped in silence.
"""

from __future__ import annotations

from bot.helper.upload.stream_uploader import _DROPPED_BY_STREAM
from bot.helper.util.help_messages import stream_upload


def _dropped_flags() -> set[str]:
    """Every flag the mode clears, plus the merge it leaves out."""
    flags = {flag for _, entries in _DROPPED_BY_STREAM for _, flag, _ in entries}
    # same-directory is dropped by leaving the group, so it is not in the table
    return flags | {"-m"}


def test_the_help_names_every_flag_the_mode_drops():
    for flag in sorted(_dropped_flags()):
        assert flag in stream_upload, f"the help never mentions {flag}"


def test_the_help_says_the_task_message_reports_what_was_dropped():
    assert "task message" in stream_upload.lower()


def test_the_help_says_a_streamed_task_skips_the_upload_queue():
    assert "QUEUE_UPLOAD" in stream_upload


def test_the_help_says_torrents_are_unaffected():
    assert "torrent" in stream_upload.lower()
