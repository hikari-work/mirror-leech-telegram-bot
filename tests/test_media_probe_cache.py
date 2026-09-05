"""Tests for the shared ffprobe behind get_media_info/get_document_type.

Both helpers used to spawn their own ffprobe on the same file, back to back,
for every uploaded file. They now share one format+streams probe whose result
is remembered per file revision, so these tests pin down the subprocess count
and the invalidation rule.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _stub(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _pkg(name, path=None):
    mod = ModuleType(name)
    mod.__path__ = [] if path is None else [path]
    return mod


_PROBE_JSON = json.dumps(
    {
        "format": {"duration": "12.5", "tags": {"artist": "a", "title": "t"}},
        "streams": [{"codec_type": "video", "codec_name": "h264"}],
    }
)


@pytest.fixture
def media_utils(monkeypatch):
    """Import media_utils with only its module-level dependencies stubbed."""
    root = Path(__file__).resolve().parent.parent

    import os as _os

    aiofiles_os = _stub(
        "aiofiles.os",
        remove=AsyncMock(),
        makedirs=AsyncMock(),
        stat=AsyncMock(side_effect=lambda p: _os.stat(p)),
        path=SimpleNamespace(exists=AsyncMock(return_value=True)),
    )

    modules = {
        "PIL": _stub("PIL", Image=SimpleNamespace(open=lambda *_a, **_k: None)),
        "aiofiles": _pkg("aiofiles"),
        "aiofiles.os": aiofiles_os,
        "bot": _stub(
            "bot",
            LOGGER=logging.getLogger("test"),
            DOWNLOAD_DIR="/tmp/dl/",
            threads=1,
            cores="0",
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.util": _pkg(
            "bot.helper.util", str(root / "bot" / "helper" / "util")
        ),
        "bot.helper.util.bot_utils": _stub(
            "bot.helper.util.bot_utils",
            cmd_exec=AsyncMock(return_value=(_PROBE_JSON, "", 0)),
            sync_to_async=AsyncMock(return_value="video/mp4"),
        ),
        "bot.helper.util.files_utils": _stub(
            "bot.helper.util.files_utils",
            get_mime_type=lambda _p: "video/mp4",
            is_archive=lambda _p: False,
            is_archive_split=lambda _p: False,
        ),
        "bot.helper.util.shutil_helper": _stub(
            "bot.helper.util.shutil_helper", rmtree=AsyncMock()
        ),
        "bot.helper.util.status_utils": _stub(
            "bot.helper.util.status_utils", time_to_seconds=lambda _v: 0
        ),
    }
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    target = "bot.helper.util.media_utils"
    sys.modules.pop(target, None)
    module = importlib.import_module(target)
    module._PROBE_CACHE.clear()
    yield module
    sys.modules.pop(target, None)


def _cmd_exec(media_utils):
    return sys.modules["bot.helper.util.bot_utils"].cmd_exec


@pytest.mark.asyncio
async def test_one_ffprobe_answers_both_helpers(media_utils, tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"data")

    duration, artist, title = await media_utils.get_media_info(str(video))
    is_video, is_audio, is_image = await media_utils.get_document_type(str(video))

    assert (duration, artist, title) == (12, "a", "t")
    assert (is_video, is_audio, is_image) == (True, False, False)
    assert _cmd_exec(media_utils).await_count == 1


@pytest.mark.asyncio
async def test_probe_asks_for_format_and_streams_together(media_utils, tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"data")

    await media_utils.get_media_info(str(video))

    cmd = _cmd_exec(media_utils).await_args.args[0]
    assert "-show_format" in cmd and "-show_streams" in cmd


@pytest.mark.asyncio
async def test_rewritten_file_is_probed_again(media_utils, tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"data")
    await media_utils.get_media_info(str(video))

    video.write_bytes(b"different data")
    await media_utils.get_media_info(str(video))

    assert _cmd_exec(media_utils).await_count == 2


@pytest.mark.asyncio
async def test_a_missing_file_is_never_cached(media_utils, tmp_path):
    gone = str(tmp_path / "gone.mp4")

    await media_utils.get_media_info(gone)
    await media_utils.get_media_info(gone)

    assert _cmd_exec(media_utils).await_count == 2
    assert gone not in media_utils._PROBE_CACHE


@pytest.mark.asyncio
async def test_cache_stays_bounded(media_utils, tmp_path):
    limit = media_utils._PROBE_CACHE_LIMIT
    for i in range(limit * 2):
        f = tmp_path / f"{i}.mp4"
        f.write_bytes(b"data")
        await media_utils.get_media_info(str(f))

    assert len(media_utils._PROBE_CACHE) == limit
    # the oldest paths are the ones that got dropped
    assert str(tmp_path / "0.mp4") not in media_utils._PROBE_CACHE
    assert str(tmp_path / f"{limit * 2 - 1}.mp4") in media_utils._PROBE_CACHE
