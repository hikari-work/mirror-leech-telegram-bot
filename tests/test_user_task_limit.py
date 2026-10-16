"""The per-user cap: who it counts, who it exempts, and where it bites.

Two values bound one non-owner user: ``USER_QUEUE_MAX`` tasks, and
``USER_QUEUE_SIZE_MAX`` bytes between them. Both are read in the same pass over
``task_dict`` -- downloading, uploading and queued all share that registry
(``_user_tasks``/``_tasks_size``) -- and a user has to be over *both* to be
turned away, which is what lets somebody with a handful of small tasks keep
adding. The refusal is raised in ``before_start``, which both task-entry classes
already run inside a ``try/except -> fail_task`` that turns the raise into a
message to the user. Restart recovery passes ``enforce_user_limit=False`` so a
task that was already admitted before the restart is never turned away on the
way back.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import bot.helper.task.settings_resolver as sr
import bot.helper.util.task_manager as tm
from bot import DOWNLOAD_DIR
from bot.core.config_manager import Config
from bot.helper.task.settings_resolver import SettingsResolverMixin

USER = 42
OTHER = 99
OWNER = 7

TASK_CAP = 3
SIZE_CAP = "1k"
SIZE_CAP_BYTES = 1024


def _dir(mid):
    """Where a task's own bytes land -- ``TaskConfig`` builds it the same way."""
    return f"{DOWNLOAD_DIR}{mid}"


def _task(user_id, size=0, mid=1):
    """A task as the counter sees it: the three listener fields it reads."""
    return SimpleNamespace(user_id=user_id, size=size, mid=mid)


def _status(user_id, size=0, mid=1, kind="download"):
    """A ``task_dict`` entry: one of those tasks wrapped in its status.

    ``kind`` is never read -- it stands in for the download/upload/queued state
    the real statuses carry, and documents that the count ignores it.
    """
    return SimpleNamespace(listener=_task(user_id, size, mid), kind=kind)


@pytest.fixture
def caps(monkeypatch):
    """A cap of three tasks and a thousand bytes, over an empty task_dict."""
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", TASK_CAP)
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", SIZE_CAP)
    monkeypatch.setattr(Config, "OWNER_ID", OWNER)
    monkeypatch.setattr(tm, "task_dict", {})
    return tm.task_dict


@pytest.fixture
def disk(monkeypatch):
    """What each mid occupies on disk, for the fallback path to read.

    Filled by the test that needs it; every other key measures 0, which is what
    a queued task with nothing downloaded yet really looks like.
    """
    sizes = {}
    walked = []

    async def fake_get_path_size(path):
        walked.append(path)
        return sizes[path]

    monkeypatch.setattr(tm, "get_path_size", fake_get_path_size)
    return SimpleNamespace(sizes=sizes, walked=walked)


# ── _user_tasks / _tasks_size ───────────────────────────────────────


async def test_only_the_asking_users_tasks_are_gathered(caps):
    caps.update({1: _status(OTHER, mid=1), 2: _status(USER, mid=2)})
    assert [task.mid for task in await tm._user_tasks(USER)] == [2]


async def test_tasks_size_adds_up_the_declared_sizes(caps):
    assert await tm._tasks_size([_task(USER, 10), _task(USER, 32)]) == 42


async def test_a_task_with_no_declared_size_is_measured_on_disk(disk):
    # aria2 and qbittorrent leave listener.size at 0 until the download ends
    disk.sizes[_dir(5)] = 700
    assert await tm._tasks_size([_task(USER, mid=5)]) == 700
    assert disk.walked == [_dir(5)]


async def test_a_declared_size_spares_the_disk_walk(disk):
    assert await tm._tasks_size([_task(USER, 700, 5)]) == 700
    assert disk.walked == []


# ── user_limit_refusal ──────────────────────────────────────────────


async def test_under_the_task_cap_is_allowed(caps):
    caps.update({i: _status(USER, 10_000, i) for i in range(2)})
    assert await tm.user_limit_refusal(USER) is None


async def test_over_the_task_cap_but_small_is_allowed(caps, disk):
    # five tasks, well under the size cap: this is the case the pair exists for
    caps.update({i: _status(USER, 10, i) for i in range(5)})
    assert await tm.user_limit_refusal(USER) is None


async def test_under_the_task_cap_never_touches_the_disk(caps, disk):
    # the sizes only decide once the task count has run out, and most tasks
    # never get that far
    caps.update({i: _status(USER, mid=i) for i in range(TASK_CAP - 1)})
    assert await tm.user_limit_refusal(USER) is None
    assert disk.walked == []


async def test_over_both_is_refused(caps):
    caps.update({i: _status(USER, SIZE_CAP_BYTES, i) for i in range(TASK_CAP)})
    refusal = await tm.user_limit_refusal(USER)
    assert refusal is not None
    assert "3 tasks totalling 3.00KB" in refusal
    assert "3 tasks and 1.00KB" in refusal


async def test_both_boundaries_have_to_be_reached(caps):
    # exactly at the task cap, one byte under the size cap
    caps.update({i: _status(USER, 341, i) for i in range(TASK_CAP)})
    assert await tm.user_limit_refusal(USER) is None


async def test_only_the_asking_user_is_counted(caps):
    # five large tasks, but four belong to someone else
    caps.update({i: _status(OTHER, SIZE_CAP_BYTES, i) for i in range(4)})
    caps[9] = _status(USER, SIZE_CAP_BYTES, 9)
    assert await tm.user_limit_refusal(USER) is None
    assert await tm.user_limit_refusal(OTHER) is not None


async def test_download_upload_and_queued_count_together(caps):
    # one of each state reaches the cap of three; the kind is not consulted
    caps.update(
        {
            1: _status(USER, SIZE_CAP_BYTES, 1, "download"),
            2: _status(USER, SIZE_CAP_BYTES, 2, "upload"),
            3: _status(USER, SIZE_CAP_BYTES, 3, "queued"),
        }
    )
    assert await tm.user_limit_refusal(USER) is not None


async def test_owner_is_never_capped(caps):
    caps.update({i: _status(OWNER, SIZE_CAP_BYTES, i) for i in range(10)})
    assert await tm.user_limit_refusal(OWNER) is None


async def test_zero_task_cap_disables_the_check(caps, monkeypatch):
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", 0)
    caps.update({i: _status(USER, SIZE_CAP_BYTES, i) for i in range(10)})
    assert await tm.user_limit_refusal(USER) is None


async def test_an_empty_size_cap_leaves_the_task_count_deciding(caps, monkeypatch):
    # the size side off means the cap is the old count-only one, however small
    # the tasks are
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", "")
    caps.update({i: _status(USER, 1, i) for i in range(TASK_CAP)})
    refusal = await tm.user_limit_refusal(USER)
    assert refusal is not None
    assert "3 tasks at once" in refusal


async def test_a_big_size_cap_spares_a_user_the_count_cap(caps, monkeypatch):
    # the point of writing 100GB: two dozen 1GB tasks are nowhere near it
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", "100GB")
    caps.update({i: _status(USER, 1073741824, i) for i in range(24)})
    assert await tm.user_limit_refusal(USER) is None


# ── _size_cap_bytes ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("100GB", 107374182400),
        ("1TB", 1099511627776),
        ("500MB", 524288000),
        ("100MB", 104857600),
        ("1k", 1024),
        ("1073741824", 1073741824),  # a plain number is bytes
        ("0", 0),  # ...and 0 is the setting's own off
        ("", 0),
    ],
)
def test_a_size_cap_is_read_the_way_it_is_written(monkeypatch, written, expected):
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", written)
    assert tm._size_cap_bytes() == expected


@pytest.mark.parametrize("written", ["100 elephants", "100b", "half a terabyte"])
def test_an_unreadable_size_cap_is_off_and_complains(
    monkeypatch, caplog, written
):
    # every one of these would otherwise read as 0, i.e. as "no cap at all",
    # without a word to whoever typo'd it
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", written)
    with caplog.at_level(logging.WARNING, logger="bot"):
        assert tm._size_cap_bytes() == 0
    assert "which is not a size this bot understands" in caplog.text


@pytest.mark.parametrize("written", [0, "0", ""])
def test_a_size_cap_of_zero_is_off_without_a_complaint(
    monkeypatch, caplog, written
):
    # 0 is how this file writes "no cap" everywhere else, so it is not a typo
    monkeypatch.setattr(Config, "USER_QUEUE_SIZE_MAX", written)
    with caplog.at_level(logging.WARNING, logger="bot"):
        assert tm._size_cap_bytes() == 0
    assert caplog.text == ""


# ── before_start gating ─────────────────────────────────────────────


class _Reached(Exception):
    """Raised by the first resolve step, to prove the cap let the task past."""


class _Listener(SettingsResolverMixin):
    """Just enough to reach -- or be stopped before -- the first resolve step."""

    def __init__(self, user_id=USER):
        self.user_id = user_id

    def _resolve_name_substitutions(self):
        raise _Reached


async def _refusal(_user_id):
    return "too much"


async def test_before_start_refuses_an_over_limit_user(monkeypatch):
    monkeypatch.setattr(sr, "user_limit_refusal", _refusal)
    with pytest.raises(ValueError, match="too much"):
        await _Listener().before_start()


async def test_before_start_lets_an_under_limit_user_past(monkeypatch):
    async def _allowed(_user_id):
        return None

    monkeypatch.setattr(sr, "user_limit_refusal", _allowed)
    with pytest.raises(_Reached):
        await _Listener().before_start()


async def test_recovery_skips_the_cap_even_when_over(monkeypatch):
    # enforce_user_limit=False must not even consult the counter, so a user who
    # is over the cap still gets resumed (reaching the first resolve step).
    monkeypatch.setattr(sr, "user_limit_refusal", _refusal)
    with pytest.raises(_Reached):
        await _Listener().before_start(enforce_user_limit=False)
