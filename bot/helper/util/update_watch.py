"""Apply a new commit on the tracked branch without losing running work.

The bot's code is replaced by ``update.py``, which ``start.sh`` runs once per
process. A commit pushed to ``UPSTREAM_REPO``/``UPSTREAM_BRANCH`` while the bot
is up therefore stays invisible until someone sends ``/restart``. This module
closes that gap: it compares the branch's remote head with the local ``HEAD``,
turns new tasks away once they differ, and restarts as soon as no task is left.
The restarted process runs ``update.py`` again, which resets the checkout to the
branch and starts the new code.

Only the difference is watched, never the commit message or its author: the two
hashes are the whole question. A failed lookup is not an update either -- an
unreachable remote must not put the bot into a mode where it refuses work.
"""

from asyncio import sleep
from collections.abc import Awaitable, Callable
from typing import Protocol

from ... import LOGGER, bot_loop, task_dict, task_dict_lock
from ...core.config_manager import Config
from .bot_utils import cmd_exec

CHECK_INTERVAL = 60
"""Seconds between two lookups of the tracked branch."""

RestartHook = Callable[[], Awaitable[None]]


class _Refusable(Protocol):
    """What ``refuse_if_pending`` needs from the task it turns away."""

    async def fail_task(
        self, error: str | Exception, *, notify: bool = True
    ) -> None: ...


restart_hook: RestartHook | None = None
"""What ``restart_if_idle`` calls once the bot may go down.

``None`` means the real restart (``modules.restart.perform_restart``). It is a
module-level slot so a test can substitute one that records the call instead of
replacing the process.
"""

_pending_commit = ""
_pending_branch = ""
_restart_started = False
_reported_error = ""
"""The last failed lookup already logged, so a dead remote is said once."""


def _branch() -> str:
    """The branch ``update.py`` resets to, with the same default it uses."""
    return str(Config.UPSTREAM_BRANCH or "master").strip() or "master"


def _sha(text: str) -> str | None:
    """The commit hash in *text*, or None when git answered something else."""
    candidate = text.strip().split()[0] if text.strip() else ""
    if len(candidate) != 40:
        return None
    if not all(char in "0123456789abcdef" for char in candidate):
        return None
    return candidate


async def _local_commit() -> str | None:
    """``HEAD`` of the checkout this process is running, or None without one.

    ``update.py`` rebuilds the repository at every start, so in a deployment
    this is the commit the branch had when the process came up.
    """
    out, err, code = await cmd_exec(["git", "rev-parse", "HEAD"])
    if code != 0:
        LOGGER.debug(f"Update check: no local commit to compare ({err})")
        return None
    return _sha(out)


async def _remote_commit(repo: str, branch: str) -> str | None:
    """The branch's head on *repo*, or None when it cannot be read.

    ``ls-remote`` rather than ``fetch``: this only asks a question, and it
    leaves the local refs untouched -- which matters because a failed fetch
    part-way through would otherwise rewrite the state the next comparison
    reads.
    """
    global _reported_error

    out, err, code = await cmd_exec(
        ["git", "ls-remote", repo, f"refs/heads/{branch}"]
    )
    if code != 0:
        _report_failure(f"Update check could not reach {repo} for {branch}: {err}")
        return None
    _reported_error = ""
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return _sha(sha)
    LOGGER.debug(f"Update check: {repo} has no branch {branch!r}")
    return None


def _report_failure(message: str) -> None:
    """Log a failed lookup once, not once per check interval.

    ``watch_updates`` asks again every minute; a remote that stays unreachable
    would otherwise write the same warning sixty times an hour until someone
    reads the log and fixes it.
    """
    global _reported_error

    if message != _reported_error:
        LOGGER.warning(message)
        _reported_error = message


async def check_once() -> str | None:
    """Look for a commit newer than the running one, and remember it.

    Returns the pending commit's hash, or None when the checkout is current (or
    when that cannot be determined). Once a commit is pending the lookup stops:
    it is the update to apply, and asking again every minute would only repeat
    a network round trip whose answer cannot change the decision.
    """
    global _pending_commit, _pending_branch

    if _pending_commit:
        return _pending_commit

    repo = str(Config.UPSTREAM_REPO or "").strip()
    if not repo:
        return None

    branch = _branch()
    remote = await _remote_commit(repo, branch)
    if remote is None:
        return None
    local = await _local_commit()
    if local is None or local == remote:
        return None

    _pending_commit = remote
    _pending_branch = branch
    LOGGER.info(
        f"Update pending: {branch}@{remote[:7]} differs from the running"
        f" {local[:7]}; no new task will start until the bot restarts"
    )
    return remote


def update_refusal() -> str | None:
    """Why a new task cannot start right now, or None when it can.

    A refusal rather than a queue: the task would outlive the restart, and the
    user would be waiting for a download that the next process never starts.
    """
    if not _pending_commit:
        return None
    return (
        f"An update to {_pending_branch} ({_pending_commit[:7]}) is waiting for"
        " the running tasks to finish, so the bot is not taking new ones."
        " Try again after it restarts."
    )


async def refuse_if_pending(task: _Refusable) -> bool:
    """Turn *task* away while an update is pending.

    The early gate the command entry points call, so a user hears about the
    update before the option keyboard and the link resolution rather than
    after them. ``before_start`` checks again for the paths that do not come
    through a command -- a live recording, for one.
    """
    reason = update_refusal()
    if reason is None:
        return False
    await task.fail_task(reason)
    return True


async def _default_restart() -> None:
    """The restart ``/restart`` performs, imported late to keep this light."""
    from ...modules.restart import perform_restart

    await perform_restart()


async def restart_if_idle() -> bool:
    """Restart once an update is pending and nothing is running.

    ``task_dict`` is the whole ledger: downloading, uploading and queued tasks
    are all in it, so an empty dict is the one moment a restart costs nobody a
    transfer. A seeding task stays in it by design -- it is still active, and
    the restart waits for it like it waits for any other task.

    Returns whether a restart was started. The flag makes the several callers
    -- the periodic pass and the completion hooks of finishing tasks -- agree
    on a single restart.
    """
    global _restart_started

    if _restart_started or not _pending_commit:
        return False
    async with task_dict_lock:
        if task_dict:
            return False

    _restart_started = True
    LOGGER.info(
        f"Restarting for {_pending_branch}@{_pending_commit[:7]}:"
        " no task is active"
    )
    try:
        await (restart_hook or _default_restart)()
    except Exception:
        # The process is still here, so the next pass may try again.
        _restart_started = False
        raise
    return True


def schedule_restart_if_idle() -> None:
    """Check for idleness now, from a synchronous caller.

    A task that has just left ``task_dict`` is the earliest moment worth
    looking; waiting for the next periodic pass would delay the restart by up
    to ``CHECK_INTERVAL`` seconds for no reason. The check is skipped entirely
    when no update is pending, so the common completion path pays one truth
    test and creates nothing.
    """
    if _pending_commit and not _restart_started:
        bot_loop.create_task(restart_if_idle())


async def watch_updates() -> None:
    """Poll the branch, then restart when the last task is gone.

    Errors are logged and the loop carries on: a restart is an optimisation,
    and a transient failure of one lookup must not take down the watcher. A
    restart that returns without replacing the process ends the loop instead
    of retrying every minute -- ``perform_restart`` only returns at all when
    something is wrong with it, and hammering it would not help.
    """
    while True:
        try:
            await check_once()
            await restart_if_idle()
        except Exception as error:
            LOGGER.error(f"Update watch failed: {error}")
        if _restart_started:
            return
        await sleep(CHECK_INTERVAL)
