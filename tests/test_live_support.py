"""The `/live` menu, filename and ffmpeg command builders, and the slot cap.

``bot/helper/download/live_support.py`` is loaded from disk so the tests never
import ``bot`` (uvloop, an event loop, ``log.txt``). Everything exercised here
is plain stdlib plus asyncio.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MODULE = _ROOT / "bot" / "helper" / "download" / "live_support.py"


@pytest.fixture(scope="module")
def support():
    spec = importlib.util.spec_from_file_location("live_support_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    # Python 3.14's dataclasses resolve annotations through sys.modules, so a
    # spec-loaded module has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class _Room:
    anchor_id: str
    nickname: str


def _rooms(count):
    return [
        _Room(anchor_id=str(1807111564595621800 + index), nickname=f"nick{index}")
        for index in range(count)
    ]


def test_the_menu_shows_at_most_ten_rooms_one_per_row(support):
    menu = support.room_menu(_rooms(14), 1, 2, 42)

    assert len(menu.rows) == support.MAX_ROOMS_PER_PAGE == 10
    assert [button.label for button in menu.rows] == [
        f"nick{index}" for index in range(10)
    ]
    assert all(button.data.startswith("live room 42 ") for button in menu.rows)


def test_pagination_buttons_stay_inside_the_range(support):
    first = support.room_menu(_rooms(10), 1, 3, 42)
    middle = support.room_menu(_rooms(10), 2, 3, 42)
    last = support.room_menu(_rooms(10), 3, 3, 42)

    assert [button.data for button in first.footer] == [
        "live page 42 1",
        "live noop 42",
        "live page 42 2",
        "live close 42",
    ]
    assert first.footer[1].label == "1/3"
    assert middle.footer[0].data == "live page 42 1"
    assert middle.footer[2].data == "live page 42 3"
    assert last.footer[2].data == "live page 42 3"
    assert last.footer[1].label == "3/3"


def test_callback_data_fits_telegram_sixty_four_bytes(support):
    room = _Room(anchor_id="1807111564595621889", nickname="a" * 80)
    menu = support.room_menu([room], 28, 28, 1234567890)

    assert all(len(button.data.encode()) <= 64 for button in menu.rows)
    assert all(len(button.data.encode()) <= 64 for button in menu.footer)


def test_long_nicknames_are_truncated_and_empty_ones_fall_back(support):
    long_room = _Room(anchor_id="1807111564595621889", nickname="n" * 80)
    empty_room = _Room(anchor_id="1807111564595621889", nickname="")

    long_label = support.room_menu([long_room], 1, 1, 1).rows[0].label
    empty_label = support.room_menu([empty_room], 1, 1, 1).rows[0].label

    assert len(long_label) <= support.BUTTON_LABEL_MAX
    assert long_label.endswith("…")
    assert "1807111564595621889" in empty_label


def test_menu_text_names_the_page_and_the_total(support):
    text = support.menu_text(279, 2, 28)
    assert "279" in text
    assert "2/28" in text


def test_the_recording_stem_is_path_safe(support):
    when = datetime(2026, 10, 20, 12, 34, 56)

    stem = support.safe_recording_stem("../../etc/pass wd", "1807", when)
    fallback = support.safe_recording_stem("", "1807", when)

    assert "/" not in stem and ".." not in stem and "\\" not in stem
    assert stem.endswith("_20261020-123456")
    assert fallback.startswith("1807_")


def test_the_record_command_streams_to_mpegts(support):
    cmd = support.build_record_command("rtmp://cdn.test/live/abc", "/tmp/a.ts")

    assert cmd[0] == "ffmpeg"
    assert cmd[-1] == "/tmp/a.ts"
    assert "-nostdin" in cmd
    assert "-rw_timeout" in cmd
    assert cmd[cmd.index("-protocol_whitelist") + 1] == support.STREAM_PROTOCOLS
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-f") + 1] == "mpegts"


def test_the_remux_command_copies_into_a_faststart_mp4(support):
    cmd = support.build_remux_command("/tmp/a.ts", "/tmp/a.mp4")

    assert cmd[-1] == "/tmp/a.mp4"
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero"


def test_the_concat_command_uses_the_concat_demuxer(support):
    cmd = support.build_concat_command("/tmp/list.txt", "/tmp/a.mp4")

    assert cmd[cmd.index("-f") + 1] == "concat"
    assert cmd[cmd.index("-safe") + 1] == "0"
    assert cmd[-1] == "/tmp/a.mp4"


def test_the_concat_list_quotes_paths(support, tmp_path):
    listing = tmp_path / "list.txt"
    parts = [tmp_path / "a.ts", tmp_path / "it's.ts"]

    support.write_concat_list([str(part) for part in parts], str(listing))

    lines = listing.read_text().splitlines()
    escaped = str(parts[1]).replace("'", "'\\''")
    assert lines[0] == f"file '{parts[0]}'"
    assert lines[1] == f"file '{escaped}'"


def test_concat_parts_reproduces_the_input_bytes(support, tmp_path):
    first = tmp_path / "part-001.ts"
    second = tmp_path / "part-002.ts"
    first.write_bytes(b"abc")
    second.write_bytes(b"defg")
    output = tmp_path / "recording.ts"

    total = support.concat_parts([str(first), str(second)], str(output))

    assert output.read_bytes() == b"abcdefg"
    assert total == 7


async def test_slots_bound_each_user_and_release(support):
    slots = support.LiveSlots(lambda: 2)

    first = await slots.claim(7)
    second = await slots.claim(7)

    assert first is not None and second is not None
    assert await slots.claim(7) is None
    assert await slots.claim(8) is not None

    await slots.release(7, first)

    assert slots.count(7) == 1
    assert await slots.claim(7) is not None

    await slots.release(7, "not-a-token")
    assert slots.count(7) == 2


async def test_a_zero_limit_means_unlimited(support):
    slots = support.LiveSlots(lambda: 0)

    assert all([await slots.claim(7) for _ in range(5)])


# ── the ffmpeg session ──────────────────────────────────────────────────
#
# A recording that cannot be stopped is worse than one that fails, and the
# stop path is all signal handling. These run a stand-in `ffmpeg` for real:
# the script takes the output path from the last argument, the same way the
# command builder puts it there.


def _fake_ffmpeg(directory, body):
    script = directory / "ffmpeg"
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(0o755)
    return f"{directory}:{os.environ['PATH']}"


def _alive(pid):
    """Whether a pid still exists; a reaped process is gone, not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_of(output):
    return int(Path(f"{output}.pid").read_text())


async def test_a_session_returns_the_bytes_ffmpeg_wrote(support, tmp_path, monkeypatch):
    path = _fake_ffmpeg(tmp_path, 'printf "abc" > "${@: -1}"\n')
    monkeypatch.setenv("PATH", path)
    output = tmp_path / "part.ts"

    written = await support.RecordSession().run(
        "rtmp://cdn.test/live/x", str(output), 5, asyncio.Event()
    )

    assert written == 3
    assert output.read_bytes() == b"abc"


async def test_a_stop_event_ends_a_running_session(support, tmp_path, monkeypatch):
    # `exec` so the signal reaches the sleeper itself instead of a shell that
    # would only handle it after its child returns. The pid is what proves the
    # process is really gone; `session.running` only says the handle was let go.
    path = _fake_ffmpeg(
        tmp_path, 'echo $$ > "${@: -1}.pid"\nexec sleep 30\n'
    )
    monkeypatch.setenv("PATH", path)
    stop = asyncio.Event()
    output = tmp_path / "p.ts"
    session = support.RecordSession()

    run = asyncio.create_task(
        session.run("rtmp://cdn.test/live/x", str(output), 60, stop)
    )
    await asyncio.sleep(0.3)
    pid = _pid_of(output)
    stop.set()

    assert await asyncio.wait_for(run, 10) == 0
    assert not session.running
    assert not _alive(pid), "the recording process outlived the stop"


async def test_a_session_stops_itself_when_the_window_ends(
    support, tmp_path, monkeypatch
):
    path = _fake_ffmpeg(
        tmp_path, 'echo $$ > "${@: -1}.pid"\nexec sleep 30\n'
    )
    monkeypatch.setenv("PATH", path)
    # MIN_SESSION_SECONDS is the floor the real callers rely on; the test needs
    # the expiry path without waiting thirty seconds for it.
    monkeypatch.setattr(support, "MIN_SESSION_SECONDS", 0.5)
    output = tmp_path / "p.ts"
    started = monotonic()

    written = await support.RecordSession().run(
        "rtmp://cdn.test/live/x", str(output), 0.5, asyncio.Event()
    )

    assert written == 0
    assert monotonic() - started < 10
    assert not _alive(_pid_of(output)), "the recording process outlived its window"


async def test_releasing_an_unknown_user_is_harmless(support):
    slots = support.LiveSlots(lambda: 1)

    await slots.release(9, "nothing")

    assert slots.count(9) == 0
