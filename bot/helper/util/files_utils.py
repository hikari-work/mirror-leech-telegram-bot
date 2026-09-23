from asyncio import create_subprocess_exec, wait_for
from collections.abc import Callable
from magic import Magic
from os import walk, path as ospath, readlink, remove as os_remove
from shutil import rmtree as shutil_rmtree
from re import split as re_split, I, search as re_search, escape
from aiofiles.os import (
    remove,
    path as aiopath,
    listdir,
    rmdir,
    symlink,
    makedirs as aiomakedirs,
)

from ... import LOGGER, DOWNLOAD_DIR, intervals
from ...core.torrent_manager import TorrentManager
from .bot_utils import sync_to_async, cmd_exec
from .exceptions import NotSupportedExtractionArchive
from .shutil_helper import rmtree as aiormtree, move
from .subproc_runner import SubprocRunner, run_subproc

ARCH_EXT = [
    ".7z",
    ".apfs",
    ".apk",
    ".apm",
    ".appx",
    ".ar",
    ".arj",
    ".asc",
    ".avhdx",
    ".b64",
    ".bz2",
    ".bzip2",
    ".cab",
    ".cbz",
    ".chm",
    ".cpio",
    ".cramfs",
    ".crc32",
    ".crc64",
    ".deb",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".elf",
    ".epub",
    ".esd",
    ".exe",
    ".fat",
    ".gpt",
    ".gz",
    ".gzip",
    ".hfs",
    ".ihex",
    ".img",
    ".iso",
    ".jar",
    ".lha",
    ".lzh",
    ".lzma",
    ".lzma2",
    ".lzma86",
    ".macho",
    ".mbr",
    ".md5",
    ".msi",
    ".mslz",
    ".msm",
    ".msp",
    ".nsis",
    ".ntfs",
    ".obj",
    ".ods",
    ".odt",
    ".pkg",
    ".pmd",
    ".ppt",
    ".pptx",
    ".qcow",
    ".qcow2",
    ".qcow2c",
    ".rar",
    ".rpm",
    ".sha1",
    ".sha224",
    ".sha256",
    ".sha384",
    ".sha512",
    ".simg",
    ".squashfs",
    ".swf",
    ".swfc",
    ".swm",
    ".sys",
    ".tar",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".taz",
    ".tbz",
    ".tbz2",
    ".tgz",
    ".tpz",
    ".txz",
    ".tzst",
    ".udeb",
    ".udf",
    ".vdi",
    ".vhd",
    ".vhdx",
    ".vmdk",
    ".wim",
    ".xar",
    ".xip",
    ".xls",
    ".xlsx",
    ".xpi",
    ".xz",
    ".z",
    ".zip",
    ".zipx",
    ".zst",
    ".zstd",
]


FIRST_SPLIT_REGEX = (
    r"\.part0*1\.rar$|\.7z\.0*1$|\.zip\.0*1$|^(?!.*\.part\d+\.rar$).*\.rar$"
)

SPLIT_REGEX = r"\.r\d+$|\.7z\.\d+$|\.z\d+$|\.zip\.\d+$|\.part\d+\.rar$"


def is_first_archive_split(file):
    return bool(re_search(FIRST_SPLIT_REGEX, file.lower(), I))


def is_archive(file):
    return file.strip().lower().endswith(tuple(ARCH_EXT))


def is_archive_split(file):
    return bool(re_search(SPLIT_REGEX, file.lower(), I))


async def clean_target(opath):
    if await aiopath.exists(opath):
        LOGGER.info(f"Cleaning Target: {opath}")
        try:
            if await aiopath.isdir(opath):
                await aiormtree(opath, ignore_errors=True)
            else:
                await remove(opath)
        except Exception as e:
            LOGGER.error(str(e))


async def clean_download(opath):
    if await aiopath.exists(opath):
        LOGGER.info(f"Cleaning Download: {opath}")
        try:
            await aiormtree(opath, ignore_errors=True)
        except Exception as e:
            LOGGER.error(str(e))


async def clean_all():
    await TorrentManager.remove_all()
    LOGGER.info("Cleaning Download Directory")
    await (await create_subprocess_exec("rm", "-rf", DOWNLOAD_DIR)).wait()
    await aiomakedirs(DOWNLOAD_DIR, exist_ok=True)


async def sweep_unless_stopping(clean, opath):
    """Run *clean* on *opath*, unless the bot is on its way down.

    ``/restart`` sets ``stopAll`` and then spends several seconds running
    ``update.py`` before it execs. A task that finishes, or fails, inside that
    window would otherwise delete the directory a recovery pass is about to
    look for -- and it is the one window in which deleting anything is exactly
    the wrong thing to do, because a row on disk claims that directory. Left
    alone, it is swept by the boot after this one, where nothing claims it.

    The same-dir paths -- ``remove_from_same_dir`` clearing a group nobody
    finished, ``_await_same_dir_merge`` clearing the directory it just moved its
    files out of -- are deliberately left ungated. A same-dir member is not
    resumable either way (its files live in the shared staging directory once
    they move), so holding the delete back would not save a single byte; it
    would only strand an empty directory, and the boot sweep cannot take the
    staging one because it is named ``sd<mid>`` rather than a plain mid.
    """
    if intervals["stopAll"]:
        return
    await clean(opath)


async def clean_unwanted(opath):
    LOGGER.info(f"Cleaning unwanted files/folders: {opath}")
    await sync_to_async(_clean_unwanted_sync, opath)
    walk_data = await sync_to_async(lambda: list(walk(opath, topdown=False)))
    for dirpath, _, files in walk_data:
        if not await listdir(dirpath):
            await rmdir(dirpath)


def _clean_unwanted_sync(opath: str) -> None:
    """Drop the leftover markers and ``.unwanted`` trees under *opath*.

    One thread hop for the whole tree, like ``_remove_walked``: the walk, the
    deletions and the ``rmtree`` are all blocking, and paying a hop each for
    them -- which is what the ``await``s here used to do -- was the bulk of what
    this function cost.
    """
    for dirpath, _, files in walk(opath, topdown=False):
        for filee in files:
            if filee.strip().endswith(".parts") and filee.startswith("."):
                os_remove(ospath.join(dirpath, filee))
        if dirpath.strip().endswith(".unwanted"):
            shutil_rmtree(dirpath, ignore_errors=True)


async def walk_files(opath, topdown=False):
    """Every file under *opath*, deepest directory first by default.

    ``os.walk`` blocks, so the whole tree is drained into a list off the event
    loop before any of the paths are touched -- which is also what makes the
    deepest-first order safe to move or delete through.
    """
    return await sync_to_async(_walk_files_sync, opath, topdown)


def _walk_files_sync(opath: str, topdown: bool = False) -> list[str]:
    """The blocking half of ``walk_files``, for callers that are already off."""
    return [
        ospath.join(dirpath, file_)
        for dirpath, _, files in walk(opath, topdown=topdown)
        for file_ in files
    ]


def _path_size_sync(opath: str) -> int:
    """The size of *opath* in bytes, following a symlink to what it names.

    The walk and every stat live in here so that one thread hop covers the whole
    tree. Spelled out with an ``await`` per file -- through ``aiopath.islink``
    and ``aiopath.getsize``, which is how this ran -- two hops were paid for
    every file just to hand it a stat that costs microseconds: measured at
    roughly 46x the price of the stat itself, over thirteen call sites per task.

    ``islink``/``readlink`` in front of a stat look redundant, since ``os.stat``
    resolves a link by itself. They are kept for what a *broken* link does
    without them: the size is then asked of a path that is not there, so the
    error names the target rather than the link the caller handed over. That
    target is the path a human has to go and look at, which is the whole value
    of the message.
    """
    if ospath.isfile(opath):
        if ospath.islink(opath):
            opath = readlink(opath)
        return ospath.getsize(opath)
    total_size = 0
    for abs_path in _walk_files_sync(opath):
        if ospath.islink(abs_path):
            abs_path = readlink(abs_path)
        total_size += ospath.getsize(abs_path)
    return total_size


async def get_path_size(opath):
    return await sync_to_async(_path_size_sync, opath)


async def count_files_and_folders(opath):
    total_files = 0
    total_folders = 0
    walk_data = await sync_to_async(lambda: list(walk(opath)))
    for _, dirs, files in walk_data:
        total_files += len(files)
        total_folders += len(dirs)
    return total_folders, total_files


def get_base_name(orig_path):
    extension = next(
        (ext for ext in ARCH_EXT if orig_path.strip().lower().endswith(ext)), ""
    )
    if extension != "":
        return re_split(f"{extension}$", orig_path, maxsplit=1, flags=I)[0]
    else:
        raise NotSupportedExtractionArchive("File format not supported for extraction")


async def create_recursive_symlink(source, destination):
    if await aiopath.isdir(source):
        await aiomakedirs(destination, exist_ok=True)
        for item in await listdir(source):
            item_source = ospath.join(source, item)
            item_dest = ospath.join(destination, item)
            await create_recursive_symlink(item_source, item_dest)
    elif await aiopath.isfile(source):
        try:
            await symlink(source, destination)
        except FileExistsError:
            LOGGER.error(f"Shortcut already exists: {destination}")
        except Exception as e:
            LOGGER.error(f"Error creating shortcut for {source}: {e}")


def get_mime_type(file_path):
    if ospath.islink(file_path):
        file_path = readlink(file_path)
    mime = Magic(mime=True)
    mime_type = mime.from_file(file_path)
    mime_type = mime_type or "text/plain"
    return mime_type


async def _remove_walked(fpath, should_remove):
    """Delete every file under *fpath* that *should_remove* accepts by name.

    The thumbnail directory yt-dlp writes is skipped: both callers mean the
    payload, and a filter written for videos would take the thumbs with it.
    """
    await sync_to_async(_remove_walked_sync, fpath, should_remove)


def _remove_walked_sync(fpath: str, should_remove: Callable[[str], bool]) -> None:
    """The blocking half: walk, test and unlink with no hop between them.

    *should_remove* is a plain string predicate, so it can run on the thread
    pool with the walk and the unlink that surround it -- which is the point.
    An ``await`` per file, which is what the callers here used to be, cost two
    thread hops each to reach an unlink that is a syscall.
    """
    for root, _, files in walk(fpath):
        if root.strip().endswith("/yt-dlp-thumb"):
            continue
        for f in files:
            if should_remove(f):
                os_remove(ospath.join(root, f))


async def remove_excluded_files(fpath, ee):
    # ``tuple(ee)`` once, not once per file: it used to be built inside the
    # predicate, where a tree of a thousand files rebuilt it a thousand times.
    exts = tuple(ee)
    await _remove_walked(fpath, lambda f: f.strip().lower().endswith(exts))


async def remove_non_included_files(fpath, ie):
    exts = tuple(ie)
    await _remove_walked(fpath, lambda f: not f.strip().lower().endswith(exts))


async def move_and_merge(source, destination, mid):
    if not await aiopath.exists(destination):
        await aiomakedirs(destination, exist_ok=True)
    for item in await listdir(source):
        item = item.strip()
        src_path = f"{source}/{item}"
        dest_path = f"{destination}/{item}"
        if await aiopath.isdir(src_path):
            if await aiopath.exists(dest_path):
                await move_and_merge(src_path, dest_path, mid)
            else:
                await move(src_path, dest_path)
        else:
            if item.endswith((".aria2", ".!qB")):
                continue
            if await aiopath.exists(dest_path):
                dest_path = f"{destination}/{mid}-{item}"
            await move(src_path, dest_path)


async def join_files(opath):
    files = await listdir(opath)
    results = []
    exists = False
    for file_ in files:
        if re_search(r"\.0+2$", file_) and await sync_to_async(
            get_mime_type, f"{opath}/{file_}"
        ) not in ["application/x-7z-compressed", "application/zip"]:
            exists = True
            final_name = file_.rsplit(".", 1)[0]
            fpath = f"{opath}/{final_name}"
            cmd = f'cat "{fpath}."* > "{fpath}"'
            _, stderr, code = await cmd_exec(cmd, True)
            if code != 0:
                LOGGER.error(f"Failed to join {final_name}, stderr: {stderr}")
                if await aiopath.isfile(fpath):
                    await remove(fpath)
            else:
                results.append(final_name)

    if not exists:
        LOGGER.warning("No files to join!")
    elif results:
        LOGGER.info("Join Completed!")
        for res in results:
            for file_ in files:
                if re_search(rf"{escape(res)}\.0[0-9]+$", file_):
                    await remove(f"{opath}/{file_}")


async def split_file(f_path, split_size, listener):
    out_path = f"{f_path}."
    # stdout is left alone: `split` says nothing on it, and there is no progress
    # to read back
    code, stderr = await run_subproc(
        listener,
        [
            "split",
            "--numeric-suffixes=1",
            "--suffix-length=3",
            f"--bytes={split_size}",
            f_path,
            out_path,
        ],
        stdout=None,
    )
    if code is None:
        return False
    if code != 0:
        LOGGER.error(f"{stderr}. Split Document: {f_path}")
    return True


class SevenZ(SubprocRunner):
    def __init__(self, listener):
        self._listener = listener
        # A fraction of the archive, so a float: both readers below get a
        # percentage off 7z and multiply the subtask's size by it.
        self._processed_bytes: float = 0
        self._percentage = "0%"

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def progress(self):
        return self._percentage

    async def _read_progress(self):
        pattern = (
            r"(\d+)\s+bytes|Total Physical Size\s*=\s*(\d+)|Physical Size\s*=\s*(\d+)"
        )
        # ``run_subproc`` puts the process on the listener before it calls this,
        # so it is there. Read once rather than through the listener on every
        # line: the same object for both loops, and a task that clears the
        # attribute while this is mid-read ends the loop instead of raising out
        # of it. Nothing to read from a command whose stdout was not a pipe.
        subproc = self._listener.subproc
        if subproc is None or subproc.stdout is None:
            return
        while not (
            subproc.returncode is not None
            or self._listener.is_cancelled
            or subproc.stdout.at_eof()
        ):
            try:
                line = await wait_for(subproc.stdout.readline(), 2)
            except OSError:
                break
            line = line.decode().strip()
            if "%" in line:
                perc = line.split("%", 1)[0]
                if perc.isdigit():
                    self._percentage = f"{perc}%"
                    self._processed_bytes = (int(perc) / 100) * self._listener.subsize
                else:
                    self._percentage = "0%"
                continue
            if match := re_search(pattern, line):
                self._listener.subsize = int(match[1] or match[2] or match[3])
        s = b""
        while not (
            self._listener.is_cancelled
            or subproc.returncode is not None
            or subproc.stdout.at_eof()
        ):
            try:
                char = await wait_for(subproc.stdout.read(1), 60)
            except (TimeoutError, Exception):
                break
            if not char:
                break
            s += char
            if char == b"%":
                try:
                    self._percentage = s.decode().rsplit(" ", 1)[-1].strip()
                    self._processed_bytes = (
                        int(self._percentage.strip("%")) / 100
                    ) * self._listener.subsize
                except (ValueError, ZeroDivisionError):
                    self._processed_bytes = 0
                    self._percentage = "0%"
                s = b""

        self._processed_bytes = 0
        self._percentage = "0%"

    async def extract(self, f_path, t_path, pswd):
        cmd = [
            "7z",
            "x",
            f"-p{pswd}",
            f_path,
            f"-o{t_path}",
            "-aot",
            "-xr!@PaxHeader",
            "-bsp1",
            "-bse1",
            "-bb3",
        ]
        if not pswd:
            del cmd[2]
        code, stderr = await self._run_cmd(cmd)
        if code is None:
            return False
        if code != 0:
            LOGGER.error(f"{stderr}. Unable to extract archive!. Path: {f_path}")
        return code

    async def zip(self, dl_path, up_path, pswd):
        size = await get_path_size(dl_path)
        # A byte count by the time a zip runs: ``-sp`` arrives as the text the
        # user typed ("2g") and ``_resolve_split_sizes`` has already reduced it,
        # which is the shape shift ``TaskConfigHost`` describes. Read once so
        # that assumption is stated here and not three times below.
        limit: int = self._listener.split_size  # pyrefly: ignore[bad-assignment]
        if self._listener.equal_splits:
            parts = -(-size // limit)
            split_size = (size // parts) + (size % parts)
        else:
            split_size = limit
        cmd = [
            "7z",
            f"-v{split_size}b",
            "a",
            "-mx=0",
            f"-p{pswd}",
            up_path,
            dl_path,
            "-bsp1",
            "-bse1",
            "-bb3",
        ]
        if int(size) > limit:
            if not pswd:
                del cmd[4]
            LOGGER.info(f"Zip: orig_path: {dl_path}, zip_path: {up_path}.0*")
        else:
            del cmd[1]
            if not pswd:
                del cmd[3]
            LOGGER.info(f"Zip: orig_path: {dl_path}, zip_path: {up_path}")
        code, stderr = await self._run_cmd(cmd)
        if code is None:
            return False
        if code == 0:
            await clean_target(dl_path)
            return up_path
        if await aiopath.exists(up_path):
            await remove(up_path)
        LOGGER.error(f"{stderr}. Unable to zip this path: {dl_path}")
        return dl_path
