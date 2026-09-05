"""Regression tests for bulk (``-b``) dispatch and same-dir merging.

A hundred-link bulk used to report "Total Files: 1" while its downloads landed
in twenty-odd folders: the merge target was a randomly picked sibling whose
directory could already have been cleaned, and the per-link command messages it
chained through earned a FloodWait that broke the chain, leaving the group
waiting for siblings that would never register.

These tests drive the real merge and batch-tracking code under concurrency with
the filesystem and Telegram calls replaced, so the ordering guarantees are
checked rather than assumed.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from bot.helper.util.task_args import (
    parse_folder_name,
    parse_leech_args,
    parse_ytdlp_args,
    strip_link_tokens,
)
import bot.helper.listeners.task_listener as tl
import bot.helper.task.batch_tracker as bt
from bot.helper.task.multi_link import MultiLinkMixin

FOLDER = "/bulk-tst"


# ── fake filesystem ─────────────────────────────────────────────────


class FakeFs:
    """Directory -> {filename: owner mid}, with the two calls the merge makes."""

    def __init__(self):
        self.dirs: dict[str, dict[str, int]] = {}
        self.fail_moves: set[str] = set()

    async def move_and_merge(self, spath, dpath, mid):
        if spath in self.fail_moves:
            raise OSError("simulated move failure")
        src = self.dirs.pop(spath, None)
        if src is None:
            return
        dst = self.dirs.setdefault(dpath, {})
        for name, owner in src.items():
            # mirrors the real collision handling in files_utils.move_and_merge
            dst[name if name not in dst else f"{mid}-{name}"] = owner
        await asyncio.sleep(0)

    async def clean_download(self, path):
        for p in [p for p in self.dirs if p == path or p.startswith(f"{path}/")]:
            del self.dirs[p]

    async def exists(self, path):
        return path in self.dirs

    def files_under(self, prefix):
        return sum(
            len(v) for p, v in self.dirs.items() if p == prefix or p.startswith(prefix)
        )

    def files_outside(self, prefix):
        return sum(len(v) for p, v in self.dirs.items() if not p.startswith(prefix))


class FakeTask:
    """Only the attributes ``_await_same_dir_merge`` actually reads."""

    _await_same_dir_merge = tl.TaskListener._await_same_dir_merge
    remove_from_same_dir = tl.TaskListener.remove_from_same_dir

    def __init__(self, mid, folder_name, same_dir):
        self.mid = mid
        self.folder_name = folder_name
        self.same_dir = same_dir
        self.dir = f"{tl.DOWNLOAD_DIR}{mid}"
        self.is_cancelled = False


@pytest.fixture
def fs(monkeypatch):
    fake = FakeFs()
    monkeypatch.setattr(tl, "move_and_merge", fake.move_and_merge)
    monkeypatch.setattr(tl, "clean_download", fake.clean_download)
    monkeypatch.setattr(tl, "aiopath", SimpleNamespace(exists=fake.exists))
    # the bot's locks are module-level singletons that bind to the first loop
    # they are used in; each test gets its own loop, so give it its own locks
    monkeypatch.setattr(tl, "task_dict_lock", asyncio.Lock())
    monkeypatch.setattr(tl, "same_directory_lock", asyncio.Lock())
    monkeypatch.setattr(
        tl,
        "LOGGER",
        SimpleNamespace(info=print, error=print, warning=print, debug=print),
    )
    return fake


def build_group(fs, count, *, total=None, dup_names=False, folder=FOLDER):
    mids = [10**9 + i for i in range(count)]
    same_dir = {
        folder: {
            "total": count if total is None else total,
            "tasks": set(mids),
            "stage": f"{tl.DOWNLOAD_DIR}sd{mids[0]}",
        }
    }
    tasks = [FakeTask(m, folder, same_dir) for m in mids]
    for i, task in enumerate(tasks):
        name = "same.mkv" if dup_names else f"file{i}.mkv"
        fs.dirs[f"{task.dir}{folder}"] = {name: task.mid}
    return tasks, same_dir


async def run_group(fs, tasks, *, cancel=(), seed=0):
    """Complete every task in a random order, as the event loop would."""
    random.seed(seed)

    async def drive(index, task):
        await asyncio.sleep(random.uniform(0, 0.02))
        if index in cancel:
            await task.remove_from_same_dir()
            await fs.clean_download(task.dir)
            return "cancelled"
        return await task._await_same_dir_merge()

    return await asyncio.gather(*(drive(i, t) for i, t in enumerate(tasks)))


# ── same-dir merge ──────────────────────────────────────────────────


class TestSameDirMerge:
    @pytest.mark.parametrize("seed", [1, 7, 42])
    async def test_one_task_uploads_everything(self, fs, seed):
        tasks, group = build_group(fs, 40)
        stage = group[FOLDER]["stage"]

        results = await run_group(fs, tasks, seed=seed)

        assert results.count(False) == 1, "exactly one task must upload"
        assert results.count(True) == 39, "the rest hand their files over"
        assert fs.files_under(stage) == 40
        assert fs.files_outside(stage) == 0

    async def test_the_uploader_switches_to_the_staging_dir(self, fs):
        tasks, group = build_group(fs, 5)
        stage = group[FOLDER]["stage"]

        results = await run_group(fs, tasks)

        uploader = tasks[results.index(False)]
        assert uploader.dir == stage
        assert fs.files_under(f"{stage}{FOLDER}") == 5

    async def test_the_uploader_runs_after_every_handover(self, fs):
        """The staging dir must be complete before anyone starts uploading."""
        tasks, group = build_group(fs, 30)
        stage_dir = f"{group[FOLDER]['stage']}{FOLDER}"
        seen = []

        async def drive(task):
            await asyncio.sleep(random.uniform(0, 0.02))
            result = await task._await_same_dir_merge()
            seen.append((result, len(fs.dirs.get(stage_dir, {}))))

        random.seed(3)
        await asyncio.gather(*(drive(t) for t in tasks))

        staged_at_upload = [count for result, count in seen if result is False]
        assert staged_at_upload == [30]

    async def test_colliding_filenames_all_survive(self, fs):
        tasks, group = build_group(fs, 25, dup_names=True)

        await run_group(fs, tasks)

        assert fs.files_under(group[FOLDER]["stage"]) == 25

    async def test_cancelled_siblings_release_their_slot(self, fs):
        tasks, group = build_group(fs, 20)
        stage = group[FOLDER]["stage"]

        results = await run_group(fs, tasks, cancel={2, 9, 19})

        assert results.count(False) == 1
        assert fs.files_under(stage) == 17
        assert fs.files_outside(stage) == 0
        assert group[FOLDER]["total"] == 0 and not group[FOLDER]["tasks"]

    async def test_a_failed_move_uploads_instead_of_stranding(self, fs):
        tasks, group = build_group(fs, 20)
        stage = group[FOLDER]["stage"]
        fs.fail_moves.add(f"{tasks[4].dir}{FOLDER}")

        results = await run_group(fs, tasks)

        # the task that could not move uploads its own folder, and the last
        # member still collects everything that did arrive
        assert results[4] is False
        assert results.count(False) == 2
        assert fs.files_under(stage) == 19
        assert fs.files_outside(stage) == 1

    async def test_a_lone_member_uploads_its_own_directory(self, fs):
        tasks, group = build_group(fs, 1)

        results = await run_group(fs, tasks)

        assert results == [False]
        assert tasks[0].dir == f"{tl.DOWNLOAD_DIR}{tasks[0].mid}"
        assert fs.files_under(group[FOLDER]["stage"]) == 0

    async def test_cancelling_every_member_clears_the_staging_dir(self, fs):
        tasks, _ = build_group(fs, 10)

        results = await run_group(fs, tasks, cancel=set(range(10)))

        assert results == ["cancelled"] * 10
        assert fs.dirs == {}

    async def test_a_dead_multi_chain_does_not_wait_forever(self, fs, monkeypatch):
        """``-i`` declares a total up front; the chain can die before reaching it."""
        monkeypatch.setattr(tl, "SAME_DIR_WAIT_TIMEOUT", 1)
        tasks, group = build_group(fs, 2, total=5)
        stage = group[FOLDER]["stage"]

        results = await asyncio.wait_for(
            asyncio.gather(*(t._await_same_dir_merge() for t in tasks)), timeout=20
        )

        assert results.count(False) == 1
        assert fs.files_under(stage) == 2
        assert fs.files_outside(stage) == 0

    async def test_a_folder_the_group_does_not_know_is_not_a_crash(self, fs):
        """A child parsing a different ``-m`` must not raise inside the listener."""
        tasks, _ = build_group(fs, 3)
        stranger = FakeTask(999, "/other", tasks[0].same_dir)

        assert await stranger._await_same_dir_merge() is False


# ── batch accounting ────────────────────────────────────────────────


class Recorder(bt.BatchTrackerMixin, MultiLinkMixin):
    def __init__(self, tag, mid):
        self.multi_tag = tag
        self.mid = mid
        self.name = f"task{mid}"
        self.link = f"https://example.invalid/{mid}"
        self.tag = "@user"
        self.same_dir = {}
        self.folder_name = ""

    async def remove_from_same_dir(self):
        return None


@pytest.fixture
def batch(monkeypatch):
    sent, edited = [], []

    async def edit_message(msg, text, *a, block=True, **kw):
        await asyncio.sleep(0)
        edited.append(text)

    async def send_message(msg, text, *a, **kw):
        await asyncio.sleep(0)
        sent.append(text)
        return SimpleNamespace(id=len(sent))

    async def delete_message(msg):
        return None

    monkeypatch.setattr(bt, "edit_message", edit_message)
    monkeypatch.setattr(bt, "send_message", send_message)
    monkeypatch.setattr(bt, "delete_message", delete_message)

    tag = "tst"
    monkeypatch.setitem(
        bt.multi_batches, tag, bt.new_batch(SimpleNamespace(id=1), 0, "bulk-tst")
    )
    bt.multi_tags.add(tag)
    yield SimpleNamespace(tag=tag, sent=sent, edited=edited)
    bt.multi_batches.pop(tag, None)
    bt.multi_tags.discard(tag)


def summaries(batch):
    return [t for t in batch.edited + batch.sent if "Complete" in t]


async def report(batch, total, *, failures=(), files=1, seed=0):
    bt.multi_batches[batch.tag]["total"] = total
    tasks = [Recorder(batch.tag, 1000 + i) for i in range(total)]
    random.seed(seed)

    async def drive(index, task):
        await asyncio.sleep(random.uniform(0, 0.02))
        if index in failures:
            await task.fail_task(f"link {index} is dead")
        else:
            await task.record_batch_result(
                {
                    "folders": files,
                    "corrupted": 0,
                    "size": 1024,
                    "files": {f"https://t.me/c/{index}": f"name{index}.mkv"},
                }
            )

    await asyncio.gather(*(drive(i, t) for i, t in enumerate(tasks)))
    return tasks


class TestBatchAccounting:
    @pytest.mark.parametrize("seed", [1, 5, 99])
    async def test_the_summary_is_published_exactly_once(self, batch, seed):
        await report(batch, 60, seed=seed)

        assert len(summaries(batch)) == 1
        assert "<b>Total Files:</b> 60" in summaries(batch)[0]
        assert batch.tag not in bt.multi_batches
        assert batch.tag not in bt.multi_tags

    async def test_progress_edits_are_coalesced(self, batch):
        await report(batch, 120)

        progress = [t for t in batch.edited if "Complete" not in t]
        assert len(progress) <= 3, "one edit per task is what earns a FloodWait"

    async def test_failures_are_counted_and_digested(self, batch):
        await report(batch, 40, failures=set(range(0, 40, 3)))

        summary = summaries(batch)[0]
        assert "<b>Failed:</b> 14" in summary
        assert "<b>Total Files:</b> 26" in summary
        assert "…and 4 more" in summary, "only the first 10 are listed by name"
        assert batch.sent == [] or "Complete" in batch.sent[0], (
            "a failed link must not answer with its own message"
        )

    async def test_file_counts_are_summed_not_overwritten(self, batch):
        await report(batch, 15, files=4)

        assert "<b>Total Files:</b> 60" in summaries(batch)[0]

    async def test_shrinking_the_target_settles_the_batch(self, batch):
        """A cancel mid-dispatch means some links never start."""
        bt.multi_batches[batch.tag]["total"] = 10
        tasks = [Recorder(batch.tag, 2000 + i) for i in range(7)]
        await asyncio.gather(
            *(
                t.record_batch_result(
                    {"folders": 1, "corrupted": 0, "size": 1, "files": {}}
                )
                for t in tasks
            )
        )
        assert not summaries(batch), "7 of 10 must not close the batch"

        await tasks[0]._shrink_batch(3)

        assert len(summaries(batch)) == 1
        assert "<b>Total Files:</b> 7" in summaries(batch)[0]


# ── the command strings handed to each child ────────────────────────


class TestBulkChildCommands:
    """``dispatch_bulk`` predicts the same-dir folder before any task starts.

    If a child parsed a different ``-m`` it would never join the group, so the
    dispatcher's prediction and the child's parse have to agree exactly.
    """

    LINKS = ["https://a.invalid/1.mkv", "https://b.invalid/2.mkv"]

    def _dispatch(self, command, *, ytdlp=False, ranged=False, tag="ab1"):
        input_list = command.split(" ")
        options = strip_link_tokens(input_list[1:], ytdlp=ytdlp)
        if not ranged:
            parse = parse_ytdlp_args if ytdlp else parse_leech_args
            args = parse(input_list[1:])
            index = options.index("-b")
            del options[index]
            if args.bulk_start or args.bulk_end:
                del options[index]
        if "-m" not in " ".join(options):
            options.append(f"-m bulk-{tag}")
        joined = " ".join(options)
        folder = parse_folder_name(f"{self.LINKS[0]} {joined}".split(), ytdlp=ytdlp)
        children = [f"{input_list[0]} {link} {joined}" for link in self.LINKS]
        parse = parse_ytdlp_args if ytdlp else parse_leech_args
        return folder, [parse(c.split(" ")[1:]) for c in children]

    @pytest.mark.parametrize(
        "command,ytdlp,ranged",
        [
            ("/leech -b", False, False),
            ("/leech -b :100", False, False),
            ("/leech -b 5:20 -doc -hl", False, False),
            ("/leech -b -m My Folder -sp 2000000000", False, False),
            ("/leech -b -n custom name -su", False, False),
            ("/ytdl -b -doc", True, False),
            ("/ytdl -b :50 -m Series One", True, False),
            ("/leech https://t.me/c/1/1-3 -doc", False, True),
            ("/leech https://t.me/c/1/1-3", False, True),
            ("/leech https://t.me/c/1/1-3 -m Shared", False, True),
        ],
    )
    def test_children_reparse_what_the_dispatcher_predicted(
        self, command, ytdlp, ranged
    ):
        folder, parsed = self._dispatch(command, ytdlp=ytdlp, ranged=ranged)

        assert folder, "bulk always merges into a folder"
        for link, child in zip(self.LINKS, parsed):
            assert child.link == link, "the original address must not leak through"
            assert child.folder_name == folder
            assert child.multi == 0, "children must not re-run the -i chain"
            assert not child.is_bulk, "children must not re-expand the bulk"

    def test_explicit_folder_beats_the_generated_one(self):
        folder, parsed = self._dispatch("/leech -b -m Shared Season 1")

        assert folder == "/Shared Season 1"
        assert all(p.folder_name == "/Shared Season 1" for p in parsed)

    def test_flags_survive_the_round_trip(self):
        _, parsed = self._dispatch("/leech -b -doc -hl -su -n custom name")

        for child in parsed:
            assert child.as_doc and child.hybrid_leech and child.stream_upload
            assert child.name == "custom name"
