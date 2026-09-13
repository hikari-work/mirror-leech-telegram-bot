"""A tree is measured and pruned without an ``await`` per file.

``get_path_size`` walks a finished download and adds up what it holds, and
``remove_excluded_files`` / ``remove_non_included_files`` walk one and unlink
what a filter names. All three used to spell that walk out with an ``await`` per
file -- ``aiopath.islink`` and ``aiopath.getsize`` for the size, ``aiopath``'s
``remove`` for the unlink.

Every one of those is a trip to the thread pool. Nothing is wrong by itself; the
price is, because each hop is paid to reach a syscall that costs microseconds.
Measured on this machine, two hops per file came to roughly 46x the price of the
stat itself. ``get_path_size`` has thirteen call sites and four of them run once
per post-processing stage over every file a task downloaded, so a
five-hundred-file task paid about 1.4 seconds of thread hops per call.

What makes this worth its own test file is that the *answer* is identical on
both sides of the change, and identical to what the whole rest of the suite
already pins. Only the number of trips changes, so the trips are the thing that
has to be counted.

Two instruments, because each alone is blind to half of it:

* the offload seam, which shows the whole tree reaching the pool as one call
  rather than one per file -- this is about *our* hops;
* a spy over the async filesystem API, which shows the per-file ``await`` gone
  entirely -- this is about the hops that never went through the seam at all,
  since ``aiofiles`` reaches the pool on its own.

The second is the load-bearing one. The first would also pass on the old code
for a tree, because ``walk_files`` already drained the walk in a single hop, so
counting only the seam misses every hop the stat loop paid.

``walk_files`` is pinned alongside because the fix is only meaningful if its
callers get the walking half rather than the eager wrapper.

The imports are the real modules rather than stubs. ``files_utils`` is a leaf
for these three functions -- nothing in their path needs TorrentManager -- and
the cost of importing the chain is already paid by ``test_destination_dispatch``,
which imports ``task_listener`` for real.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bot.helper.util import files_utils as fu


class _HopCounter:
    """The offload seam observed instead of faked, and still hopping.

    Every other test that touches this module can run the offloaded call inline;
    these cannot, because the property under test is *how many* of them the walk
    pays for. Keeping the hop also lets a test ask whether the work left the
    event loop at all, rather than only that it happened.
    """

    def __init__(self) -> None:
        self.calls: list = []
        self.threads: list[threading.Thread] = []

    async def __call__(self, func, *args, **kwargs):
        self.calls.append(func)

        def record(*inner_args, **inner_kwargs):
            self.threads.append(threading.current_thread())
            return func(*inner_args, **inner_kwargs)

        return await asyncio.to_thread(record, *args, **kwargs)


class _AsyncFsSpy:
    """Counts every call made through an async filesystem object.

    ``aiofiles`` reaches the thread pool on its own -- its wrappers call
    ``loop.run_in_executor`` directly -- so no amount of watching this module's
    own offload seam will see them. Wrapping the object catches every one.
    """

    def __init__(self, inner, names: list[str]) -> None:
        self._inner = inner
        self.names = names

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        return _counting(attr, name, self.names)


def _counting(attr, name: str, names: list[str]):
    """An awaitable stand-in for one async filesystem call that records itself.

    A function rather than an object, because ``listdir`` and friends are called
    as bare names here -- ``await listdir(p)`` -- not as attributes of anything,
    so patching the module attribute has to leave something callable behind.
    """

    async def counted(*args, **kwargs):
        names.append(name)
        return await attr(*args, **kwargs)

    return counted


@pytest.fixture(autouse=True)
def _offload_inline(monkeypatch) -> None:
    """Run the offload seam inline, for every test in this file.

    ``sync_to_async`` hands work to ``bot_loop`` -- the loop ``bot/__init__``
    built for the running bot -- and awaiting a future from that loop inside a
    pytest task is what raises "attached to a different loop". This file is
    about *how many* hops a walk pays for, not about the bot's loop, so the seam
    is stubbed everywhere and :func:`hops` overrides it with a counting version
    in the tests where the count is the assertion.
    """

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(fu, "sync_to_async", inline)


@pytest.fixture
def hops(monkeypatch) -> _HopCounter:
    counter = _HopCounter()
    monkeypatch.setattr(fu, "sync_to_async", counter)
    return counter


@pytest.fixture
def fs_calls(monkeypatch) -> _AsyncFsSpy:
    """Every ``aiofiles`` call the module makes, by name, however it makes it."""
    names: list[str] = []
    spy = _AsyncFsSpy(fu.aiopath, names)
    monkeypatch.setattr(fu, "aiopath", spy)
    for name in ("remove", "listdir", "rmdir", "aiormtree", "aiomakedirs"):
        monkeypatch.setattr(fu, name, _counting(getattr(fu, name), name, names))
    return spy


class _CountingExts:
    """An extension list that says how many times it was made into a tuple.

    ``remove_excluded_files`` took ``tuple(ee)`` while building its predicate,
    and the predicate is what the walk calls per file -- so the tuple was rebuilt
    once per file over a tree that can hold thousands. Counting ``__iter__`` is
    the narrowest way to see that: hoisting it costs one iteration per tree.
    """

    def __init__(self, exts: list[str]) -> None:
        self._exts = exts
        self.iterations = 0

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        return iter(self._exts)


def tree(root: Path, count: int, suffix: str = ".mkv", size: int = 10) -> Path:
    """*count* files of *size* bytes each under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f"f{i}{suffix}").write_bytes(b"x" * size)
    return root


# --------------------------------------------------------------- get_path_size


async def test_a_tree_is_sized_without_a_call_per_file(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """The instrument that matters: forty files, no async filesystem call."""
    root = tree(tmp_path / "Album", 40, size=7)

    size = await fu.get_path_size(str(root))

    assert fs_calls.names == []
    assert size == 40 * 7


async def test_the_whole_tree_reaches_the_pool_as_one_call(
    hops: _HopCounter, tmp_path: Path
) -> None:
    root = tree(tmp_path / "Album", 40)

    await fu.get_path_size(str(root))

    assert hops.calls == [fu._path_size_sync]


async def test_the_count_does_not_grow_with_the_files(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """Two trees forty times apart in size cost the same to measure."""
    small = tree(tmp_path / "Small", 2)
    large = tree(tmp_path / "Large", 300)

    await fu.get_path_size(str(small))
    await fu.get_path_size(str(large))

    assert fs_calls.names == []
    assert len(hops.calls) == 2


async def test_the_measurement_leaves_the_event_loop(
    hops: _HopCounter, tmp_path: Path
) -> None:
    """The walk and the stats are blocking, so they must not run in the loop."""
    root = tree(tmp_path / "Album", 5)

    await fu.get_path_size(str(root))

    assert hops.threads
    assert all(t is not threading.main_thread() for t in hops.threads)


async def test_a_single_file_is_sized_without_async_filesystem_calls(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """The file branch skips the walk and was three hops on its own before."""
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 123)

    size = await fu.get_path_size(str(target))

    assert size == 123
    assert fs_calls.names == []
    assert hops.calls == [fu._path_size_sync]


async def test_a_symlinked_directory_is_sized_as_what_it_points_at(
    fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """Links are resolved, which is why the walk does not just stat each path."""
    root = tree(tmp_path / "Album", 3, size=11)
    linked = tmp_path / "linked"
    linked.symlink_to(root)

    assert await fu.get_path_size(str(linked)) == 3 * 11


async def test_a_symlinked_file_is_sized_as_its_target(tmp_path: Path) -> None:
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 55)
    link = tmp_path / "link.mkv"
    link.symlink_to(target)

    assert await fu.get_path_size(str(link)) == 55


async def test_a_link_inside_the_tree_is_followed(tmp_path: Path) -> None:
    """A link is measured as the file it points at."""
    root = tree(tmp_path / "Album", 2, size=13)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 100)
    (root / "linked.bin").symlink_to(outside)

    # the two real files, plus the link counted as the target it points at
    assert await fu.get_path_size(str(root)) == 2 * 13 + 100


async def test_a_broken_link_is_reported_against_its_target(tmp_path: Path) -> None:
    """What the ``islink``/``readlink`` pair buys, and the only thing it buys.

    A stat resolves a link on its own, so for a link that resolves the pair is
    dead weight -- which is what makes it tempting to drop. It is kept for the
    link that does *not* resolve: the size is then asked of the target's own
    path, so the error names the file that is missing rather than the link
    somebody handed over, and that name is what a human has to go and look at.
    Pinned here because nothing else in this file fails if the pair goes.
    """
    root = tree(tmp_path / "Album", 1)
    (root / "dangling.bin").symlink_to(tmp_path / "gone.bin")

    with pytest.raises(FileNotFoundError) as caught:
        await fu.get_path_size(str(root))

    assert caught.value.filename == str(tmp_path / "gone.bin")


async def test_an_empty_tree_is_zero(fs_calls: _AsyncFsSpy, tmp_path: Path) -> None:
    root = tmp_path / "Empty"
    root.mkdir()

    assert await fu.get_path_size(str(root)) == 0


async def test_an_absent_path_answers_zero(
    fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """Pinned as-is rather than as-it-should-be: ``os.walk`` is silent about a
    directory that is not there, so an absent path sums to nothing and raises
    nothing. That is what this answered before the walk was batched, and the
    thirteen call sites were written against it.
    """
    assert await fu.get_path_size(str(tmp_path / "nope")) == 0


# ------------------------------------------------------------------ walk_files


async def test_walk_files_returns_every_path_in_one_hop(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    root = tree(tmp_path / "Album", 12)

    found = await fu.walk_files(str(root))

    assert len(hops.calls) == 1
    assert sorted(os.path.basename(p) for p in found) == sorted(
        f"f{i}.mkv" for i in range(12)
    )


async def test_walk_files_still_walks_deepest_first(tmp_path: Path) -> None:
    """Order is load-bearing: the callers move and delete through this list."""
    root = tree(tmp_path / "Album", 1)
    deep = tree(root / "a" / "b", 1)

    found = await fu.walk_files(str(root))

    assert found[0] == str(deep / "f0.mkv")
    assert found[-1] == str(root / "f0.mkv")


# ------------------------------------------------------- the extension filters


async def test_the_extension_tuple_is_built_once_for_a_whole_tree(
    hops: _HopCounter, tmp_path: Path
) -> None:
    """Once per tree, not once per file -- the predicate ran per file."""
    root = tree(tmp_path / "Album", 30)
    exts = _CountingExts([".mkv"])

    await fu.remove_excluded_files(str(root), exts)

    assert exts.iterations == 1


async def test_excluded_files_are_removed_without_a_call_per_file(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    root = tree(tmp_path / "Album", 30)
    (root / "keep.srt").write_bytes(b"x")

    await fu.remove_excluded_files(str(root), [".mkv"])

    assert fs_calls.names == []
    assert [p.name for p in root.iterdir()] == ["keep.srt"]


async def test_non_included_files_are_removed_without_a_call_per_file(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """The complement of the other filter, and it bats in the same single call."""
    root = tree(tmp_path / "Album", 30, suffix=".srt")
    (root / "keep.mkv").write_bytes(b"x")

    await fu.remove_non_included_files(str(root), [".mkv"])

    assert fs_calls.names == []
    assert [p.name for p in root.iterdir()] == ["keep.mkv"]


async def test_the_thumbnail_directory_is_left_alone(tmp_path: Path) -> None:
    """Both callers mean the payload.

    The guard earns its place on the keep-list filter, not the block-list one:
    yt-dlp writes its thumbs as ``.jpg`` beside media whose extension is what
    "keep only these" names, so without the skip every thumb of a video download
    would be deleted as a non-video.
    """
    root = tree(tmp_path / "Album", 3)
    thumbs = tree(root / "yt-dlp-thumb", 3, suffix=".jpg")

    await fu.remove_non_included_files(str(root), [".mkv"])

    assert sorted(p.name for p in thumbs.iterdir()) == ["f0.jpg", "f1.jpg", "f2.jpg"]
    assert sorted(p.name for p in root.iterdir() if p.is_file()) == [
        "f0.mkv",
        "f1.mkv",
        "f2.mkv",
    ]


async def test_filtering_leaves_the_event_loop(hops: _HopCounter, tmp_path: Path):
    root = tree(tmp_path / "Album", 5)

    await fu.remove_excluded_files(str(root), [".mkv"])

    assert hops.threads
    assert all(t is not threading.main_thread() for t in hops.threads)


# ---------------------------------------------------------------- clean_unwanted


async def test_clean_unwanted_drops_markers_and_unwanted_trees(
    hops: _HopCounter, tmp_path: Path
) -> None:
    """One hop for the walk that deletes; the marker and the tree both go."""
    root = tree(tmp_path / "Album", 3)
    (root / ".something.parts").write_bytes(b"x")
    unwanted = tree(root / "junk.unwanted", 2)

    await fu.clean_unwanted(str(root))

    assert not (root / ".something.parts").exists()
    assert not unwanted.exists()
    assert (root / "f0.mkv").exists()


async def test_clean_unwanted_unlinks_without_a_call_per_file(
    hops: _HopCounter, fs_calls: _AsyncFsSpy, tmp_path: Path
) -> None:
    """The first pass is off the async filesystem API entirely.

    Pass two still lists and rmdirs through ``aiopath``, which is why this asks
    about the two calls pass one used to make rather than about the whole spy.
    """
    root = tree(tmp_path / "Album", 3)
    for i in range(20):
        (root / f".part{i}.parts").write_bytes(b"x")
    tree(root / "junk.unwanted", 2)

    await fu.clean_unwanted(str(root))

    assert "remove" not in fs_calls.names
    assert "aiormtree" not in fs_calls.names


async def test_clean_unwanted_removes_directories_left_empty(
    hops: _HopCounter, tmp_path: Path
) -> None:
    """The second pass is a separate walk, so it is a second hop."""
    root = tree(tmp_path / "Album", 2)
    nested = root / "nested"
    nested.mkdir()
    (nested / ".gone.parts").write_bytes(b"x")

    await fu.clean_unwanted(str(root))

    assert not nested.exists()
    assert (root / "f0.mkv").exists()
