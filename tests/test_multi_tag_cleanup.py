"""The delayed cleanup in ``run_multi`` must not cancel a bulk it did not start.

``/leech https://t.me/c/…/1-40 terabox`` schedules ``run_multi`` first -- it is a
fire-and-forget ``@new_task`` -- and only then resolves the range. While
``_run_multi`` sits in its three-second ``sleep``, ``_handle_bulk`` installs the
bulk's tag on the *same* listener. The cleanup used to re-read
``self.multi_tag`` after waking, find that tag, and discard it; ``dispatch_bulk``
then saw its tag gone and stopped after the handful of tasks it had already
spawned, so the rest of the range never ran.
"""

from __future__ import annotations

import bot.helper.task.multi_link as ml

COMMAND = ["/leech", "https://t.me/c/1/1-40", "terabox"]


class _Listener(ml.MultiLinkMixin):
    """The state ``_run_multi`` reads on a one-task path, plus the real method."""

    def __init__(self, multi: int = 1, multi_tag: str | None = None) -> None:
        self.multi = multi
        self.multi_tag = multi_tag
        self.user_id = 1
        # ``run_multi`` returns early for a bulk child; this is a command.
        self.bulk_child = False


async def test_cleanup_keeps_a_tag_a_bulk_installed_while_it_slept(monkeypatch):
    tag = "Race"
    listener = _Listener(multi=1)
    monkeypatch.setattr(ml, "multi_tags", {})
    monkeypatch.setattr(ml, "multi_batches", {})

    async def sleep_then_start_bulk(_seconds):
        # exactly what ``_handle_bulk`` does during the wait
        listener.multi_tag = tag
        ml.multi_tags[tag] = listener.user_id

    monkeypatch.setattr(ml, "sleep", sleep_then_start_bulk)

    await listener._run_multi(COMMAND, object)

    assert tag in ml.multi_tags, "the bulk's tag was cancelled by run_multi's timer"


async def test_cleanup_still_releases_the_tag_a_chained_task_was_given(monkeypatch):
    """The chained last task of ``-i`` owns its tag and ends it, as before."""

    tag = "Givn"
    listener = _Listener(multi=1, multi_tag=tag)
    tags = {tag: listener.user_id}
    monkeypatch.setattr(ml, "multi_tags", tags)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ml, "sleep", no_sleep)

    await listener._run_multi(COMMAND, object)

    assert tag not in tags
