"""The per-user task cap: who it counts, who it exempts, and where it bites.

``USER_QUEUE_MAX`` bounds how many tasks one non-owner user may hold at once --
downloading, uploading or queued, all of which share the ``task_dict`` registry.
The count is taken there (``user_over_task_limit``) and the refusal is raised in
``before_start``, which both task-entry classes already run inside a
``try/except -> fail_task`` that turns the raise into a message to the user.
Restart recovery passes ``enforce_user_limit=False`` so a task that was already
admitted before the restart is never turned away on the way back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.helper.task.settings_resolver as sr
import bot.helper.util.task_manager as tm
from bot.core.config_manager import Config
from bot.helper.task.settings_resolver import SettingsResolverMixin

USER = 42
OTHER = 99
OWNER = 7


def _status(user_id, kind="download"):
    """A task_dict entry as the counter sees it: a status with a listener.

    ``kind`` is never read -- it stands in for the download/upload/queued state
    the real statuses carry, and documents that the count ignores it.
    """
    return SimpleNamespace(listener=SimpleNamespace(user_id=user_id), kind=kind)


@pytest.fixture
def limit(monkeypatch):
    """A cap of three, no owner, and a task_dict each test fills itself."""
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", 3)
    monkeypatch.setattr(Config, "OWNER_ID", OWNER)
    monkeypatch.setattr(tm, "task_dict", {})
    return tm.task_dict


# ── user_over_task_limit ────────────────────────────────────────────


async def test_under_the_cap_is_allowed(limit):
    limit.update({1: _status(USER), 2: _status(USER)})
    assert await tm.user_over_task_limit(USER) is False


async def test_at_the_cap_is_refused(limit):
    limit.update({1: _status(USER), 2: _status(USER), 3: _status(USER)})
    assert await tm.user_over_task_limit(USER) is True


async def test_only_the_asking_user_is_counted(limit):
    # five tasks, but four belong to someone else
    limit.update({i: _status(OTHER) for i in range(4)})
    limit[9] = _status(USER)
    assert await tm.user_over_task_limit(USER) is False
    assert await tm.user_over_task_limit(OTHER) is True


async def test_download_upload_and_queued_count_together(limit):
    # one of each state reaches the cap of three; the kind is not consulted
    limit.update(
        {
            1: _status(USER, "download"),
            2: _status(USER, "upload"),
            3: _status(USER, "queued"),
        }
    )
    assert await tm.user_over_task_limit(USER) is True


async def test_owner_is_never_capped(limit):
    limit.update({i: _status(OWNER) for i in range(10)})
    assert await tm.user_over_task_limit(OWNER) is False


async def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", 0)
    monkeypatch.setattr(Config, "OWNER_ID", OWNER)
    monkeypatch.setattr(tm, "task_dict", {i: _status(USER) for i in range(10)})
    assert await tm.user_over_task_limit(USER) is False


# ── before_start gating ─────────────────────────────────────────────


class _Reached(Exception):
    """Raised by the first resolve step, to prove the cap let the task past."""


class _Listener(SettingsResolverMixin):
    """Just enough to reach -- or be stopped before -- the first resolve step."""

    def __init__(self, user_id=USER):
        self.user_id = user_id

    def _resolve_name_substitutions(self):
        raise _Reached


async def _over(_user_id):
    return True


async def _under(_user_id):
    return False


async def test_before_start_refuses_an_over_limit_user(monkeypatch):
    monkeypatch.setattr(sr, "user_over_task_limit", _over)
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", 3)
    with pytest.raises(ValueError, match="running or queued"):
        await _Listener().before_start()


async def test_before_start_lets_an_under_limit_user_past(monkeypatch):
    monkeypatch.setattr(sr, "user_over_task_limit", _under)
    with pytest.raises(_Reached):
        await _Listener().before_start()


async def test_recovery_skips_the_cap_even_when_over(monkeypatch):
    # enforce_user_limit=False must not even consult the counter, so a user who
    # is over the cap still gets resumed (reaching the first resolve step).
    monkeypatch.setattr(sr, "user_over_task_limit", _over)
    monkeypatch.setattr(Config, "USER_QUEUE_MAX", 3)
    with pytest.raises(_Reached):
        await _Listener().before_start(enforce_user_limit=False)
