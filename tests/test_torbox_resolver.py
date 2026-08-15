"""Tests for the TorBox resolver.

TorBox had no tests before Phase 4 of the refactor, which is awkward given
the resolver drives real money-costing API calls and a poll loop with three
different ways to give up. These pin the behaviour the differential harness
compared against the pre-refactor version: the ready/error vocabulary, the
stall and timeout guards, cleanup on failure, and the shape of the payload
handed to ``add_direct_download``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def torbox(monkeypatch):
    """Import ``torbox_resolver`` with the bot package stubbed out.

    The real ``bot/__init__.py`` reads env and opens sockets; the resolver
    only needs LOGGER, Config and the exception type.
    """
    project_root = Path(__file__).resolve().parent.parent

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        @staticmethod
        def warning(msg):
            pass

    bot_pkg.LOGGER = _Logger()

    core_pkg = ModuleType("bot.core")
    core_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        TORBOX_API_KEY = "test-key"

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = []
    exceptions_mod = ModuleType("bot.helper.ext_utils.exceptions")

    class DirectDownloadLinkException(Exception):
        pass

    exceptions_mod.DirectDownloadLinkException = DirectDownloadLinkException

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    download_utils_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    download_utils_pkg.__path__ = [
        str(project_root / "bot" / "helper" / "mirror_leech_utils" / "download_utils")
    ]

    for name, mod in {
        "bot": bot_pkg,
        "bot.core": core_pkg,
        "bot.core.config_manager": config_manager,
        "bot.helper": helper_pkg,
        "bot.helper.ext_utils": ext_utils_pkg,
        "bot.helper.ext_utils.exceptions": exceptions_mod,
        "bot.helper.mirror_leech_utils": mlu_pkg,
        "bot.helper.mirror_leech_utils.download_utils": download_utils_pkg,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    for name in list(sys.modules):
        if name.startswith("bot.helper.mirror_leech_utils.download_utils.debrid"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    sys.modules.pop(
        "bot.helper.mirror_leech_utils.download_utils.torbox_resolver", None
    )

    module = importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.torbox_resolver"
    )
    # Never sleep between polls in tests.
    monkeypatch.setattr(module, "_POLL_INTERVAL", 0)
    return module


def fake_api(torbox, monkeypatch, script):
    """Drive ``_api`` from a list of results; record the calls made.

    A list entry is returned as-is, raised if it is an exception, or called
    if it is a callable. The last entry repeats once the script runs out.
    """
    calls: list[tuple] = []
    state = {"i": 0}

    async def _api(method, endpoint, *, params=None, data=None, files=None):
        calls.append((method, endpoint, params, data, files))
        entry = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry()
        return entry

    monkeypatch.setattr(torbox, "_api", _api)
    return calls


def files(n=2):
    return [
        {
            "id": i,
            "name": f"dir/f{i}.mkv",
            "short_name": f"f{i}.mkv",
            "size": 100 * (i + 1),
        }
        for i in range(n)
    ]


def ready(n=2, **extra):
    return {
        "torrent_id": 55,
        "name": "Some.Torrent",
        "download_finished": True,
        "download_state": "completed",
        "seeds": 4,
        "peers": 2,
        "files": files(n),
        **extra,
    }


def downloading(**extra):
    return {
        "torrent_id": 55,
        "name": "Some.Torrent",
        "download_state": "downloading",
        "seeds": 3,
        "peers": 1,
        "files": [],
        **extra,
    }


# ── magnet route ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_magnet_resolves_to_direct_links(torbox, monkeypatch):
    fake_api(
        torbox,
        monkeypatch,
        [{"torrent_id": 55}, ready(), "https://cdn/f0", "https://cdn/f1"],
    )

    out = await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    assert out["title"] == "Some.Torrent"
    assert out["torbox_torrent_id"] == 55
    assert out["total_size"] == 300
    assert {c["filename"] for c in out["contents"]} == {"f0.mkv", "f1.mkv"}
    # ``path`` keeps the full name so nested layouts survive the download.
    assert {c["path"] for c in out["contents"]} == {"dir/f0.mkv", "dir/f1.mkv"}
    assert all(c["url"].startswith("https://cdn/") for c in out["contents"])


@pytest.mark.asyncio
async def test_magnet_polls_until_ready(torbox, monkeypatch):
    calls = fake_api(
        torbox,
        monkeypatch,
        [
            {"torrent_id": 55},
            downloading(),
            downloading(),
            ready(n=1),
            "https://cdn/only",
        ],
    )

    await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    status_calls = [c for c in calls if c[1] == "/torrents/mylist"]
    assert len(status_calls) == 3


@pytest.mark.asyncio
async def test_magnet_without_id_fails_before_polling(torbox, monkeypatch):
    calls = fake_api(torbox, monkeypatch, [{"name": "no id here"}])

    with pytest.raises(Exception, match="did not return torrent_id"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    assert len(calls) == 1  # only the create call


@pytest.mark.asyncio
async def test_error_state_deletes_the_torrent(torbox, monkeypatch):
    calls = fake_api(
        torbox,
        monkeypatch,
        [
            {"torrent_id": 55},
            downloading(download_state="failed"),
            {"ok": True},
        ],
    )

    with pytest.raises(Exception, match="TorBox torrent failed: failed"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    assert calls[-1][1] == "/torrents/controltorrent"
    assert calls[-1][3] == {"torrent_id": "55", "operation": "Delete"}


@pytest.mark.asyncio
async def test_explicit_error_field_wins_over_state(torbox, monkeypatch):
    fake_api(
        torbox,
        monkeypatch,
        [{"torrent_id": 55}, downloading(error="disk full"), {"ok": True}],
    )

    with pytest.raises(Exception, match="disk full"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")


@pytest.mark.asyncio
async def test_cancellation_stops_before_the_first_poll(torbox, monkeypatch):
    calls = fake_api(torbox, monkeypatch, [{"torrent_id": 55}, ready(), {"ok": True}])

    with pytest.raises(Exception, match="cancelled"):
        await torbox.torbox_resolve_magnet(
            "magnet:?xt=urn:btih:abc", is_cancelled=lambda: True
        )

    assert not [c for c in calls if c[1] == "/torrents/mylist"]
    assert calls[-1][1] == "/torrents/controltorrent"


@pytest.mark.asyncio
async def test_empty_swarm_times_out(torbox, monkeypatch):
    monkeypatch.setattr(torbox, "_NO_SEED_WAIT", 0)
    fake_api(
        torbox,
        monkeypatch,
        [{"torrent_id": 55}, downloading(seeds=0, peers=0), {"ok": True}],
    )

    with pytest.raises(Exception, match="no seed / no peer timeout"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")


@pytest.mark.asyncio
async def test_returning_peers_reset_the_stall_timer(torbox, monkeypatch):
    """A quiet spell followed by a peer must not count toward the timeout."""
    monkeypatch.setattr(torbox, "_NO_SEED_WAIT", 0)
    fake_api(
        torbox,
        monkeypatch,
        [
            {"torrent_id": 55},
            downloading(seeds=0, peers=0),
            downloading(seeds=0, peers=2),  # resets
            downloading(seeds=0, peers=0),
            ready(n=1),
            "https://cdn/only",
        ],
    )

    out = await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")
    assert len(out["contents"]) == 1


@pytest.mark.asyncio
async def test_overall_timeout(torbox, monkeypatch):
    monkeypatch.setattr(torbox, "_MAX_WAIT", 0)
    fake_api(torbox, monkeypatch, [{"torrent_id": 55}, downloading(), {"ok": True}])

    with pytest.raises(Exception, match="max wait timeout"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")


@pytest.mark.asyncio
async def test_ready_but_no_files(torbox, monkeypatch):
    item = ready()
    item["files"] = []
    fake_api(torbox, monkeypatch, [{"torrent_id": 55}, item, {"ok": True}])

    with pytest.raises(Exception, match="returned no files"):
        await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")


@pytest.mark.asyncio
async def test_files_without_ids_are_skipped(torbox, monkeypatch):
    item = ready()
    item["files"] = [
        {"id": 0, "name": "a.mkv", "short_name": "a.mkv", "size": 5},
        {"name": "no-id.mkv", "size": 6},
    ]
    fake_api(torbox, monkeypatch, [{"torrent_id": 55}, item, "https://cdn/a"])

    out = await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    assert [c["filename"] for c in out["contents"]] == ["a.mkv"]
    assert out["total_size"] == 5


@pytest.mark.asyncio
async def test_missing_short_name_falls_back_to_basename(torbox, monkeypatch):
    item = ready()
    item["files"] = [{"id": 0, "name": "deep/dir/movie.mkv", "size": 42}]
    fake_api(torbox, monkeypatch, [{"torrent_id": 55}, item, "https://cdn/x"])

    out = await torbox.torbox_resolve_magnet("magnet:?xt=urn:btih:abc")

    assert out["contents"][0]["filename"] == "movie.mkv"
    assert out["contents"][0]["path"] == "deep/dir/movie.mkv"


@pytest.mark.asyncio
async def test_progress_callback_receives_torrent_snapshots(torbox, monkeypatch):
    fake_api(
        torbox,
        monkeypatch,
        [
            {"torrent_id": 55},
            downloading(progress=0.5, eta=30),
            ready(n=1),
            "https://cdn/only",
        ],
    )
    seen: list[dict] = []

    async def progress(snapshot):
        seen.append(snapshot)

    await torbox.torbox_resolve_magnet(
        "magnet:?xt=urn:btih:abc", progress_callback=progress
    )

    assert {s["phase"] for s in seen} == {"torrent"}
    assert seen[0]["progress"] == 0.5
    assert seen[0]["eta"] == 30
    assert seen[0]["seeds"] == 3


# ── .torrent file route ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_torrent_file_upload_and_resolve(torbox, monkeypatch):
    calls = fake_api(
        torbox, monkeypatch, [{"torrent_id": 55}, ready(n=1), "https://cdn/only"]
    )

    out = await torbox.torbox_resolve_torrent(b"d8:announce", "my file.torrent")

    assert out["torbox_torrent_id"] == 55
    upload = calls[0]
    assert upload[1] == "/torrents/createtorrent"
    assert upload[4]["file"][0] == "my file.torrent"


# ── web download route ────────────────────────────────────────────────


def ready_webdl(n=1):
    return {
        "webdownload_id": 88,
        "name": "WebFile",
        "download_finished": True,
        "download_state": "completed",
        "files": files(n),
    }


@pytest.mark.asyncio
async def test_web_download_resolves(torbox, monkeypatch):
    calls = fake_api(
        torbox, monkeypatch, [{"webdownload_id": 88}, ready_webdl(), "https://cdn/w"]
    )

    out = await torbox.torbox_resolve("https://host/file.bin")

    assert out["torbox_web_id"] == 88
    assert out["title"] == "WebFile"
    # The webdl endpoints are used throughout, never the torrent ones.
    assert all("/webdl/" in c[1] for c in calls)


@pytest.mark.asyncio
async def test_web_download_id_falls_back_across_key_names(torbox, monkeypatch):
    for key in ("webdownload_id", "web_id", "id"):
        fake_api(torbox, monkeypatch, [{key: 88}, ready_webdl(), "https://cdn/w"])
        out = await torbox.torbox_resolve("https://host/file.bin")
        assert out["torbox_web_id"] == 88, key


@pytest.mark.asyncio
async def test_web_download_never_stalls_on_an_empty_swarm(torbox, monkeypatch):
    """A web download has no seeds by definition; the stall guard must not fire."""
    monkeypatch.setattr(torbox, "_NO_SEED_WAIT", 0)
    fake_api(
        torbox,
        monkeypatch,
        [
            {"webdownload_id": 88},
            {"webdownload_id": 88, "download_state": "downloading", "files": []},
            {"webdownload_id": 88, "download_state": "downloading", "files": []},
            ready_webdl(),
            "https://cdn/w",
        ],
    )

    out = await torbox.torbox_resolve("https://host/file.bin")
    assert len(out["contents"]) == 1


@pytest.mark.asyncio
async def test_web_download_failure_deletes_the_download(torbox, monkeypatch):
    calls = fake_api(
        torbox,
        monkeypatch,
        [
            {"webdownload_id": 88},
            {"webdownload_id": 88, "download_state": "error", "files": []},
            {"ok": True},
        ],
    )

    with pytest.raises(Exception, match="TorBox webdl failed"):
        await torbox.torbox_resolve("https://host/file.bin")

    assert calls[-1][1] == "/webdl/controlwebdownload"
    assert calls[-1][3] == {"web_id": "88", "operation": "Delete"}


# ── status vocabulary ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"download_finished": True}, True),
        ({"download_present": True}, True),
        ({"download_state": "cached", "files": [1]}, True),
        ({"download_state": "completed", "files": [1]}, True),
        ({"download_state": "uploading", "files": [1]}, True),
        # A ready-looking state with no file list yet is not ready.
        ({"download_state": "cached", "files": []}, False),
        ({"download_state": "downloading", "files": [1]}, False),
        ({}, False),
    ],
)
def test_is_ready(torbox, item, expected):
    assert torbox._is_ready(item) is expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"error": "boom"}, "boom"),
        ({"download_state": "failed"}, "failed"),
        ({"download_state": "stalled (no seeds)"}, "stalled (no seeds)"),
        ({"download_state": "downloading"}, ""),
        ({}, ""),
    ],
)
def test_has_error(torbox, item, expected):
    assert torbox._has_error(item) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ([{"a": 1}], {"a": 1}),
        ([], {}),
        ({"torrent": {"x": 1}}, {"x": 1}),
        ({"webdl": {"x": 2}}, {"x": 2}),
        ({"plain": 5}, {"plain": 5}),
        ("string", {}),
        (None, {}),
    ],
)
def test_first_item(torbox, data, expected):
    assert torbox._first_item(data) == expected


def test_missing_api_key_is_reported(torbox, monkeypatch):
    monkeypatch.setattr(torbox.Config, "TORBOX_API_KEY", "  ")
    with pytest.raises(Exception, match="TORBOX_API_KEY is not configured"):
        torbox._token()
