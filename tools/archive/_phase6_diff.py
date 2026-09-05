"""Differential harness: task_listener.on_download_complete before vs after Fase 6.

Loads the pre-refactor module out of git and the working-tree module under the
same stubs, drives both through identical scenarios, and compares everything
observable: the ordered call log with arguments, the state left on the
listener, the shared globals (task_dict, queues, same_dir bookkeeping), the
LOGGER output, and the exception raised.

Exit code 0 means every scenario matched. Usage: python tools/_phase6_diff.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TARGET = "bot/helper/listeners/task_listener.py"

# Deviations we accept on purpose. Empty: Fase 6 is a pure refactor.
EXPECTED: dict[str, str] = {}


# --------------------------------------------------------------------------
# recording world
# --------------------------------------------------------------------------


class World:
    """Shared recorder + knobs driving one run of one scenario."""

    def __init__(self):
        self.reset()

    def reset(self, spec=None):
        spec = spec or {}
        self.calls: list = []
        self.logs: list[str] = []
        self.existing: set[str] = set(spec.get("existing", []))
        self.files: list[str] = list(spec.get("listdir", []))
        self.listdir_raises = spec.get("listdir_raises")
        self.isfile: set[str] = set(spec.get("isfile", []))
        self.sizes = spec.get("sizes", {})
        self.step_returns = spec.get("step_returns", {})
        self.cancel_after = spec.get("cancel_after")
        self.add_to_queue = spec.get("add_to_queue", False)
        self.cancel_on_wait = spec.get("cancel_on_wait", False)
        self.sleep_hook = spec.get("sleep_hook")
        self.sleep_count = 0
        self.listener = None

    def record(self, name, *args):
        self.calls.append((name, *args))
        if self.cancel_after == name and self.listener is not None:
            self.listener.is_cancelled = True

    def log(self, msg):
        self.logs.append(str(msg))


WORLD = World()


# --------------------------------------------------------------------------
# stub environment
# --------------------------------------------------------------------------


async def _exists(p):
    WORLD.record("exists", p)
    return p in WORLD.existing


async def _isfile(p):
    WORLD.record("isfile", p)
    return p in WORLD.isfile


async def _listdir(p):
    WORLD.record("listdir", p)
    if WORLD.listdir_raises:
        raise OSError(WORLD.listdir_raises)
    return list(WORLD.files)


async def _remove(p):
    WORLD.record("remove", p)


async def _sleep(secs):
    WORLD.record("sleep", round(secs, 4))
    WORLD.sleep_count += 1
    if WORLD.sleep_hook:
        WORLD.sleep_hook(WORLD)
    if WORLD.sleep_count > 200:
        raise RuntimeError("sleep loop did not converge")


async def _get_path_size(p):
    WORLD.record("get_path_size", p)
    return WORLD.sizes.get(p, 100)


async def _clean_download(p):
    WORLD.record("clean_download", p)


async def _clean_target(p):
    WORLD.record("clean_target", p)


async def _join_files(p):
    WORLD.record("join_files", p)


async def _create_recursive_symlink(a, b):
    WORLD.record("create_recursive_symlink", a, b)


async def _remove_excluded_files(d, ext):
    WORLD.record("remove_excluded_files", d, tuple(ext or ()))


async def _remove_non_included_files(d, ext):
    WORLD.record("remove_non_included_files", d, tuple(ext or ()))


async def _move_and_merge(a, b, mid):
    WORLD.record("move_and_merge", a, b, mid)


async def _start_from_queued():
    WORLD.record("start_from_queued")


class _Event:
    async def wait(self):
        WORLD.record("event.wait")
        if WORLD.cancel_on_wait and WORLD.listener is not None:
            WORLD.listener.is_cancelled = True


async def _check_running_tasks(listener, kind):
    WORLD.record("check_running_tasks", kind)
    return WORLD.add_to_queue, _Event()


async def _send_message(msg, text, button=None):
    WORLD.record("send_message", str(text)[:60])


async def _delete_message(msg):
    WORLD.record("delete_message")


async def _delete_status():
    WORLD.record("delete_status")


async def _update_status_message(chat_id):
    WORLD.record("update_status_message", chat_id)


class _QueueStatus:
    def __init__(self, listener, gid, kind):
        WORLD.record("QueueStatus", gid, kind)
        self.gid = gid


class _TelegramStatus:
    def __init__(self, listener, tg, gid, kind):
        WORLD.record("TelegramStatus", gid, kind)
        self.gid = gid


class _TelegramUploader:
    def __init__(self, listener, up_dir):
        WORLD.record("TelegramUploader.__init__", up_dir)

    async def upload(self):
        WORLD.record("TelegramUploader.upload")


def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _pkg(name, path=None):
    m = _stub(name)
    m.__path__ = [path] if path else []
    return m


class FakeTaskConfig:
    """Stands in for TaskConfig: the media pipeline and batch bookkeeping."""

    def __init__(self):
        pass

    async def _step(self, label, up_path):
        WORLD.record(label, up_path)
        return WORLD.step_returns.get(label, up_path)

    async def proceed_extract(self, up_path, gid):
        return await self._step("proceed_extract", up_path)

    async def proceed_ffmpeg(self, up_path, gid):
        return await self._step("proceed_ffmpeg", up_path)

    async def substitute(self, up_path):
        return await self._step("substitute", up_path)

    async def generate_screenshots(self, up_path):
        return await self._step("generate_screenshots", up_path)

    async def convert_media(self, up_path, gid):
        return await self._step("convert_media", up_path)

    async def generate_sample_video(self, up_path, gid):
        return await self._step("generate_sample_video", up_path)

    async def proceed_compress(self, up_path, gid):
        return await self._step("proceed_compress", up_path)

    async def proceed_split(self, up_path, gid):
        WORLD.record("proceed_split", up_path)

    def _batch(self):
        return getattr(self, "_batch_obj", None)

    async def record_batch_result(self, result):
        WORLD.record("record_batch_result", result)

    async def record_batch_done(self):
        WORLD.record("record_batch_done")

    async def register_batch_failure(self, error):
        WORLD.record("register_batch_failure", error)


TASK_DICT: dict = {}
NON_QUEUED_UP: set = set()
NON_QUEUED_DL: set = set()
QUEUED_UP: dict = {}
QUEUED_DL: dict = {}
INTERVALS: dict = {"status": {}}
MULTI_BATCHES: dict = {}


def install_stubs():
    aiofiles_os = _stub(
        "aiofiles.os",
        listdir=_listdir,
        remove=_remove,
        path=SimpleNamespace(exists=_exists, isfile=_isfile),
    )
    bot_pkg = _pkg("bot")
    for k, v in {
        "intervals": INTERVALS,
        "task_dict": TASK_DICT,
        "task_dict_lock": asyncio.Lock(),
        "LOGGER": SimpleNamespace(
            info=WORLD.log, error=WORLD.log, warning=WORLD.log, debug=WORLD.log
        ),
        "non_queued_up": NON_QUEUED_UP,
        "non_queued_dl": NON_QUEUED_DL,
        "queued_up": QUEUED_UP,
        "queued_dl": QUEUED_DL,
        "queue_dict_lock": asyncio.Lock(),
        "same_directory_lock": asyncio.Lock(),
        "multi_batches": MULTI_BATCHES,
        "DOWNLOAD_DIR": "/downloads/",
    }.items():
        setattr(bot_pkg, k, v)

    mods = {
        "aiofiles": _pkg("aiofiles"),
        "aiofiles.os": aiofiles_os,
        "bot": bot_pkg,
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": _stub(
            "bot.core.config_manager",
            Config=SimpleNamespace(
                QUEUE_ALL=False, INCOMPLETE_TASK_NOTIFIER=False, DATABASE_URL=""
            ),
        ),
        "bot.core.torrent_manager": _stub(
            "bot.core.torrent_manager",
            TorrentManager=SimpleNamespace(
                aria2=SimpleNamespace(purgeDownloadResult=lambda: _noop())
            ),
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.common": _stub("bot.helper.common", TaskConfig=FakeTaskConfig),
        "bot.helper.ext_utils": _pkg("bot.helper.ext_utils"),
        "bot.helper.ext_utils.db_handler": _stub(
            "bot.helper.ext_utils.db_handler",
            database=SimpleNamespace(
                add_incomplete_task=lambda *a: _noop(),
                rm_complete_task=lambda *a: _noop(),
            ),
        ),
        "bot.helper.ext_utils.files_utils": _stub(
            "bot.helper.ext_utils.files_utils",
            get_path_size=_get_path_size,
            clean_download=_clean_download,
            clean_target=_clean_target,
            join_files=_join_files,
            create_recursive_symlink=_create_recursive_symlink,
            remove_excluded_files=_remove_excluded_files,
            remove_non_included_files=_remove_non_included_files,
            move_and_merge=_move_and_merge,
        ),
        "bot.helper.ext_utils.status_utils": _stub(
            "bot.helper.ext_utils.status_utils",
            get_readable_file_size=lambda n: f"{n}B",
        ),
        "bot.helper.ext_utils.task_manager": _stub(
            "bot.helper.ext_utils.task_manager",
            start_from_queued=_start_from_queued,
            check_running_tasks=_check_running_tasks,
        ),
        "bot.helper.listeners": _pkg(
            "bot.helper.listeners", str(ROOT / "bot" / "helper" / "listeners")
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        "bot.helper.mirror_leech_utils.status_utils": _pkg(
            "bot.helper.mirror_leech_utils.status_utils"
        ),
        "bot.helper.mirror_leech_utils.status_utils.queue_status": _stub(
            "bot.helper.mirror_leech_utils.status_utils.queue_status",
            QueueStatus=_QueueStatus,
        ),
        "bot.helper.mirror_leech_utils.status_utils.telegram_status": _stub(
            "bot.helper.mirror_leech_utils.status_utils.telegram_status",
            TelegramStatus=_TelegramStatus,
        ),
        "bot.helper.mirror_leech_utils.upload_utils": _pkg(
            "bot.helper.mirror_leech_utils.upload_utils"
        ),
        "bot.helper.mirror_leech_utils.upload_utils.telegram_uploader": _stub(
            "bot.helper.mirror_leech_utils.upload_utils.telegram_uploader",
            TelegramUploader=_TelegramUploader,
        ),
        "bot.helper.telegram_helper": _pkg("bot.helper.telegram_helper"),
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils",
            send_message=_send_message,
            delete_message=_delete_message,
            delete_status=_delete_status,
            update_status_message=_update_status_message,
        ),
    }
    sys.modules.update(mods)
    asyncio.sleep = _sleep


async def _noop():
    return None


def _load(source: str, modname: str) -> ModuleType:
    mod = ModuleType(modname)
    mod.__package__ = "bot.helper.listeners"
    mod.__file__ = str(ROOT / TARGET)
    sys.modules[modname] = mod
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def load_old() -> ModuleType:
    src = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return _load(src, "_old_task_listener")


def load_new() -> ModuleType:
    return _load((ROOT / TARGET).read_text(), "_new_task_listener")


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


class _Download:
    def __init__(self, name, gid):
        self._n, self._g = name, gid

    def name(self):
        return self._n

    def gid(self):
        return self._g


def base_spec(**over):
    spec = {
        "attrs": {},
        "existing": {"/downloads/1/movie.mkv"},
        "isfile": {"/downloads/1/movie.mkv"},
        "listdir": ["movie.mkv"],
        "in_task_dict": True,
        "same_dir": {},
    }
    spec.update(over)
    return spec


def make_listener(mod, spec):
    lis = mod.TaskListener()
    attrs = {
        "is_cancelled": False,
        "folder_name": "",
        "same_dir": spec.get("same_dir", {}),
        "mid": 1,
        "dir": "/downloads/1",
        "name": "movie.mkv",
        "size": 0,
        "is_file": False,
        "seed": False,
        "up_dir": "",
        "subproc": "leftover",
        "is_torrent": False,
        "is_qbit": False,
        "is_super_chat": False,
        "included_extensions": [],
        "excluded_extensions": [],
        "join": False,
        "extract": False,
        "ffmpeg_cmds": None,
        "name_sub": "",
        "screen_shots": False,
        "convert_audio": False,
        "convert_video": False,
        "sample_video": False,
        "compress": False,
        "message": SimpleNamespace(chat=SimpleNamespace(id=99), link="L"),
        "tag": "@u",
        "thumb": "",
        "subname": "SUB",
        "subsize": 7,
        "files_to_proceed": ["stale"],
        "proceed_count": 3,
        "progress": False,
        "_batch_obj": None,
    }
    attrs.update(spec.get("attrs", {}))
    for k, v in attrs.items():
        # deep-copy so the old run's mutations (same_dir bookkeeping, batch
        # counters) cannot leak into the new run's starting state
        setattr(lis, k, v if k == "message" else deepcopy(v))

    # clear() resets to exactly the values a fresh listener already holds, and
    # the trailing split block calls it anyway — so final state cannot reveal
    # a stage that clears when it should not. Record each call instead.
    real_clear = lis.clear

    def _clear_recorded():
        WORLD.record("clear")
        real_clear()

    lis.clear = _clear_recorded
    return lis


STATE_KEYS = (
    "name",
    "size",
    "is_file",
    "seed",
    "up_dir",
    "subproc",
    "is_cancelled",
    "subname",
    "subsize",
    "files_to_proceed",
    "proceed_count",
    "progress",
)


async def run_once(mod, spec):
    WORLD.reset(spec)
    TASK_DICT.clear()
    NON_QUEUED_UP.clear()
    NON_QUEUED_DL.clear()
    QUEUED_UP.clear()
    QUEUED_DL.clear()
    lis = make_listener(mod, spec)
    WORLD.listener = lis
    if spec.get("in_task_dict", True):
        TASK_DICT[lis.mid] = _Download(spec.get("dl_name", "movie.mkv"), "GID1")
    NON_QUEUED_DL.add(lis.mid)

    err = None
    try:
        await mod.TaskListener.on_download_complete(lis)
    except Exception as e:  # noqa: BLE001 - compared, not handled
        err = f"{type(e).__name__}: {e}"

    return {
        "calls": list(WORLD.calls),
        "logs": list(WORLD.logs),
        "state": {k: getattr(lis, k, "<missing>") for k in STATE_KEYS},
        "task_dict": sorted(
            (k, type(v).__name__) for k, v in TASK_DICT.items()
        ),
        "non_queued_dl": sorted(NON_QUEUED_DL),
        "non_queued_up": sorted(NON_QUEUED_UP),
        "same_dir": repr(lis.same_dir),
        "error": err,
    }


def _sd(total, tasks):
    return {"/f": {"total": total, "tasks": list(tasks)}}


def _drop_from_group(w):
    w.listener.same_dir["/f"]["tasks"].remove(w.listener.mid)


def _become_last(w):
    w.listener.same_dir["/f"]["tasks"].append(999)


def scenarios():
    s = []
    add = lambda n, **kw: s.append((n, base_spec(**kw)))  # noqa: E731

    add("plain-file")
    add("cancelled-upfront", attrs={"is_cancelled": True})
    add("not-in-task-dict", in_task_dict=False)
    add("cancel-during-initial-sleep", cancel_after="sleep")

    # name resolution
    add("missing-path-listdir-fallback", existing=set(), listdir=["a.mkv", "b.mkv"])
    add(
        "missing-path-ytdlp-thumb",
        existing=set(),
        listdir=["first.mkv", "yt-dlp-thumb"],
    )
    add("listdir-raises", existing=set(), listdir_raises="boom")
    add("folder-name", attrs={"folder_name": "/pack/inner"}, existing=set(),
        listdir=["pack"])

    # seed / symlink
    add("seed-torrent", attrs={"seed": True, "is_torrent": True},
        existing={"/downloads/1/movie.mkv", "/downloads/110000/movie.mkv"})
    add("seed-but-not-torrent", attrs={"seed": True})

    # up_dir pre-set on a non-seed task: the extension filter must read
    # self.up_dir or self.dir, not the resolved upload dir, so a pre-set
    # up_dir (e.g. a leftover from seeding) is NOT filtered.
    add("preset-updir-nonseed", attrs={"up_dir": "/downloads/9/stale"},
        existing={"/downloads/1/movie.mkv", "/downloads/9/stale/movie.mkv"},
        isfile={"/downloads/1/movie.mkv"})

    # extension filters
    add("included-extensions", attrs={"included_extensions": ["mkv"]})
    add("excluded-extensions", attrs={"excluded_extensions": ["nfo"]})
    add("no-files-after-filter", existing={"/downloads/1/movie.mkv"},
        attrs={"folder_name": ""}, isfile=set(), sizes={})

    # queue
    add("queue-all", attrs={}, in_task_dict=True)
    add("add-to-queue", add_to_queue=True)
    add("add-to-queue-cancelled", add_to_queue=True, cancel_on_wait=True)

    # join
    add("join-folder", attrs={"join": True}, isfile=set())

    # each stage alone
    for attr, call in [
        ("extract", "proceed_extract"),
        ("ffmpeg_cmds", "proceed_ffmpeg"),
        ("name_sub", "substitute"),
        ("screen_shots", "generate_screenshots"),
        ("convert_audio", "convert_media"),
        ("convert_video", "convert_media"),
        ("sample_video", "generate_sample_video"),
        ("compress", "proceed_compress"),
    ]:
        val = ["x"] if attr == "ffmpeg_cmds" else True
        add(f"stage-{attr}", attrs={attr: val})
        add(f"stage-{attr}-cancelled", attrs={attr: val}, cancel_after=call)
        add(
            f"stage-{attr}-newpath",
            attrs={attr: val},
            step_returns={call: "/downloads/1/out/new.mkv"},
        )
        # cancel right after the step returns a DIFFERENT path: the stage's
        # own name/size/is_file writes are then the final observable state,
        # so per-stage set_name/refresh_size flags cannot be masked by the
        # trailing name/size assignment.
        add(
            f"stage-{attr}-newpath-cancelled",
            attrs={attr: val},
            step_returns={call: "/downloads/1/out/new.mkv"},
            cancel_after=call,
        )

    # combinations
    add("all-stages", attrs={
        "extract": True, "ffmpeg_cmds": ["x"], "name_sub": "s",
        "screen_shots": True, "convert_audio": True, "sample_video": True,
    })
    add("all-stages-plus-compress", attrs={
        "extract": True, "ffmpeg_cmds": ["x"], "name_sub": "s",
        "screen_shots": True, "convert_video": True, "sample_video": True,
        "compress": True,
    })
    add("extract-with-included", attrs={"extract": True,
                                        "included_extensions": ["mkv"]})
    add("split-cancelled", cancel_after="proceed_split")

    # A stage's own post-step writes (set_name / refresh_size / clear) are
    # normally overwritten by the trailing name+size assignment. Cancelling
    # inside the NEXT stage stops the pipeline in between, making the earlier
    # stage's writes the final observable state.
    for name, first, nxt, call, nxt_call in [
        ("extract", {"extract": True}, {"ffmpeg_cmds": ["x"]},
         "proceed_extract", "proceed_ffmpeg"),
        ("namesub", {"name_sub": "s"}, {"screen_shots": True},
         "substitute", "generate_screenshots"),
        ("screenshots", {"screen_shots": True}, {"convert_audio": True},
         "generate_screenshots", "convert_media"),
        ("convert", {"convert_audio": True}, {"sample_video": True},
         "convert_media", "generate_sample_video"),
        ("sample", {"sample_video": True}, {"compress": True},
         "generate_sample_video", "proceed_compress"),
    ]:
        add(
            f"{name}-then-cancel-in-next",
            attrs={**first, **nxt},
            step_returns={call: f"/downloads/1/{name}/new.mkv"},
            cancel_after=nxt_call,
        )

    # same_dir
    add("same-dir-solo", attrs={"folder_name": "/f", "same_dir": _sd(1, [1])})
    # same-dir solo but the task was seeding: the `elif self.same_dir:
    # seed = False` branch must still clear the seed flag.
    add("same-dir-solo-seeding", attrs={
        "folder_name": "/f", "same_dir": _sd(1, [1]),
        "seed": True, "is_torrent": True,
    })
    add("same-dir-merge", attrs={"folder_name": "/f", "same_dir": _sd(2, [1, 2])})
    add("same-dir-not-member", attrs={"folder_name": "/f",
                                      "same_dir": _sd(2, [7, 8])})
    add("same-dir-wait-then-drop", attrs={"folder_name": "/f",
                                          "same_dir": _sd(3, [1])},
        sleep_hook=_drop_from_group)
    add("same-dir-wait-then-merge", attrs={"folder_name": "/f",
                                           "same_dir": _sd(3, [1])},
        sleep_hook=_become_last)
    add("same-dir-no-folder-name", attrs={"same_dir": _sd(2, [1, 2])})
    add("same-dir-merge-with-batch", attrs={
        "folder_name": "/f", "same_dir": _sd(2, [1, 2]),
        "_batch_obj": {"done": 0, "anchor": None, "errors": [], "results": []},
    })
    return s


# --------------------------------------------------------------------------


def diff(a, b):
    out = []
    for key in a:
        if a[key] != b[key]:
            out.append(f"    {key}:\n      old={a[key]!r}\n      new={b[key]!r}")
    return out


async def main():
    install_stubs()
    old, new = load_old(), load_new()

    mismatches = 0
    total = 0
    for name, spec in scenarios():
        total += 1
        res_old = await run_once(old, spec)
        res_new = await run_once(new, spec)
        d = diff(res_old, res_new)
        if not d:
            continue
        if name in EXPECTED:
            print(f"[expected] {name}: {EXPECTED[name]}")
            continue
        mismatches += 1
        print(f"[MISMATCH] {name}")
        print("\n".join(d))

    print(f"\n{total} scenarios, {mismatches} unexpected mismatch(es)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
