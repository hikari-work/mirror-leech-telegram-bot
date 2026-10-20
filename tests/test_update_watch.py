"""An update that arrives while the bot is running.

``update.py`` only runs at process start, so a commit pushed to the tracked
branch is invisible to a running bot. The watcher closes that gap in two
steps, and both are pinned here:

* the remote head is compared with the local one, and a difference -- and
  only a difference -- puts the bot into the "no new tasks" state;
* once that state is set, a restart happens as soon as ``task_dict`` is
  empty, and not a moment sooner.

The refusals themselves live in ``before_start`` and in the early gate the
command entry points call, so the last tests check that a pending update
reaches a user and that restart recovery is exempt from it.
"""

from __future__ import annotations

import asyncio

import pytest

import bot.helper.util.update_watch as uw
from bot.core.config_manager import Config
from bot.helper.listeners.command_task import CommandTask
from bot.helper.task.settings_resolver import SettingsResolverMixin

LOCAL = "a" * 40
REMOTE = "b" * 40


@pytest.fixture(autouse=True)
def watch(monkeypatch):
    """A pending-free watcher with a repo configured, over an empty ledger."""
    monkeypatch.setattr(uw, "_pending_commit", "")
    monkeypatch.setattr(uw, "_pending_branch", "")
    monkeypatch.setattr(uw, "_restart_started", False)
    monkeypatch.setattr(uw, "_reported_error", "")
    monkeypatch.setattr(uw, "restart_hook", None)
    monkeypatch.setattr(uw, "task_dict", {})
    monkeypatch.setattr(Config, "UPSTREAM_REPO", "https://example.test/repo")
    monkeypatch.setattr(Config, "UPSTREAM_BRANCH", "main")
    return uw


def _git(monkeypatch, *, local=LOCAL, remote=REMOTE, local_code=0, remote_code=0):
    """Script the two git calls and return what was asked of them."""
    calls = []

    async def fake(cmd, shell=False):
        calls.append(cmd)
        if cmd[1] == "rev-parse":
            return local, "fatal" if local_code else "", local_code
        if remote_code:
            return "", "cannot reach", remote_code
        return f"{remote}\trefs/heads/main", "", 0

    monkeypatch.setattr(uw, "cmd_exec", fake)
    return calls


def _failures():
    """An async restart hook that records its calls instead of exiting."""

    called = []

    async def hook():
        called.append(True)

    return hook, called


def _pending(monkeypatch):
    """Put the watcher in the state a detected update leaves it in.

    Detection has its own tests above; these ones are about what the rest of
    the bot does with the result, so they start from the result.
    """
    monkeypatch.setattr(uw, "_pending_commit", REMOTE)
    monkeypatch.setattr(uw, "_pending_branch", "main")


class _Task:
    """The slice of a listener ``refuse_if_pending`` uses."""

    def __init__(self):
        self.errors = []

    async def fail_task(self, error, *, notify=True):
        self.errors.append(error)


# ── detection ───────────────────────────────────────────────────────


async def test_without_an_upstream_branch_nothing_is_looked_up(monkeypatch):
    monkeypatch.setattr(Config, "UPSTREAM_REPO", "")
    calls = _git(monkeypatch)

    assert await uw.check_once() is None
    assert calls == []


async def test_a_different_remote_head_marks_the_update_pending(monkeypatch):
    _git(monkeypatch)

    assert await uw.check_once() == REMOTE
    assert uw.update_refusal() is not None


async def test_the_same_head_is_not_an_update(monkeypatch):
    _git(monkeypatch, remote=LOCAL)

    assert await uw.check_once() is None
    assert uw.update_refusal() is None


async def test_an_unreachable_remote_is_not_an_update(monkeypatch):
    _git(monkeypatch, remote_code=128)

    assert await uw.check_once() is None
    assert uw.update_refusal() is None


async def test_a_checkout_without_a_commit_is_not_an_update(monkeypatch):
    _git(monkeypatch, local="", local_code=128)

    assert await uw.check_once() is None
    assert uw.update_refusal() is None


async def test_the_lookup_stops_once_a_commit_is_pending(monkeypatch):
    calls = _git(monkeypatch)
    await uw.check_once()
    asked = len(calls)

    assert await uw.check_once() == REMOTE
    assert len(calls) == asked


async def test_the_refusal_names_the_branch_and_the_commit(monkeypatch):
    _git(monkeypatch)
    await uw.check_once()

    message = uw.update_refusal()

    assert "main" in message
    assert REMOTE[:7] in message


# ── restarting ──────────────────────────────────────────────────────


async def test_a_current_checkout_never_restarts(monkeypatch):
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    assert called == []


async def test_an_active_task_keeps_the_restart_waiting(monkeypatch):
    _pending(monkeypatch)
    uw.task_dict[1] = object()
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is False
    assert called == []


async def test_the_restart_happens_as_soon_as_no_task_is_left(monkeypatch):
    _pending(monkeypatch)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    assert await uw.restart_if_idle() is True
    assert called == [True]
    # the second caller -- the periodic pass, or another finishing task --
    # must not start a second restart
    assert await uw.restart_if_idle() is False
    assert called == [True]


async def test_a_failed_restart_is_left_to_be_tried_again(monkeypatch):
    _pending(monkeypatch)

    async def hook():
        raise RuntimeError("the restart did not take")

    monkeypatch.setattr(uw, "restart_hook", hook)

    with pytest.raises(RuntimeError):
        await uw.restart_if_idle()
    assert uw._restart_started is False


async def test_finishing_the_last_task_schedules_the_idle_check(monkeypatch):
    _pending(monkeypatch)
    monkeypatch.setattr(uw, "bot_loop", asyncio.get_running_loop())
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    uw.schedule_restart_if_idle()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert called == [True]


async def test_nothing_is_scheduled_without_a_pending_update(monkeypatch):
    calls = []
    monkeypatch.setattr(
        uw,
        "bot_loop",
        type(
            "Loop",
            (),
            {"create_task": lambda self, coro: calls.append(coro)},
        )(),
    )

    uw.schedule_restart_if_idle()

    assert calls == []


async def test_the_loop_ends_after_handing_over_to_the_restart(monkeypatch):
    _pending(monkeypatch)
    monkeypatch.setattr(uw, "CHECK_INTERVAL", 0)
    hook, called = _failures()
    monkeypatch.setattr(uw, "restart_hook", hook)

    await asyncio.wait_for(uw.watch_updates(), 1)

    assert called == [True]


# ── refusing tasks ──────────────────────────────────────────────────


async def test_the_early_gate_turns_a_command_away(monkeypatch):
    _pending(monkeypatch)
    task = _Task()

    assert await uw.refuse_if_pending(task) is True
    assert task.errors and "main" in task.errors[0]


async def test_the_early_gate_lets_a_command_through_when_current(monkeypatch):
    task = _Task()

    assert await uw.refuse_if_pending(task) is False
    assert task.errors == []


class _Listener(SettingsResolverMixin):
    """What ``before_start`` needs before it reaches the first decision."""

    def __init__(self):
        self.user_id = 42
        self.destination = "s3"


def _stub_resolution(monkeypatch, listener):
    """Make ``before_start`` stop at the gate instead of resolving settings."""
    for name in (
        "_resolve_name_substitutions",
        "_resolve_extension_filters",
        "_resolve_ffmpeg_commands",
        "_resolve_s3_destination",
    ):
        monkeypatch.setattr(listener, name, lambda: None)


async def test_before_start_refuses_a_new_task_while_pending(monkeypatch):
    _pending(monkeypatch)
    listener = _Listener()
    _stub_resolution(monkeypatch, listener)

    with pytest.raises(ValueError, match="update"):
        await listener.before_start()


async def test_restart_recovery_is_not_refused_by_a_pending_update(monkeypatch):
    """A task that was already admitted comes back even mid-update."""
    _pending(monkeypatch)
    listener = _Listener()
    _stub_resolution(monkeypatch, listener)

    await listener.before_start(enforce_user_limit=False)


async def test_a_task_admitted_earlier_is_stopped_before_its_engine(monkeypatch):
    """The last admission check is the one right before the engine call.

    A task can be admitted, then spend a while resolving its link. If the
    update is detected in that window it must not start the download the
    restart is about to kill.
    """
    _pending(monkeypatch)
    task = _Task()

    admitted = await CommandTask.record_active_task(task, None, "leech", "aria2")

    assert admitted is False
    assert task.errors and "main" in task.errors[0]
