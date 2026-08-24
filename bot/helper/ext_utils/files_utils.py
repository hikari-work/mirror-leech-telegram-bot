from aioshutil import rmtree as aiormtree, move
from asyncio import create_subprocess_exec, wait_for
from magic import Magic
from os import walk, path as ospath, readlink
from re import split as re_split, I, search as re_search, escape
from aiofiles.os import (
    remove,
    path as aiopath,
    listdir,
    rmdir,
    readlink as aioreadlink,
    symlink,
    makedirs as aiomakedirs,
)

from ... import LOGGER, DOWNLOAD_DIR
from ...core.torrent_manager import TorrentManager
from .bot_utils import sync_to_async, cmd_exec
from .exceptions import NotSupportedExtractionArchive
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


async def clean_unwanted(opath):
    LOGGER.info(f"Cleaning unwanted files/folders: {opath}")
    walk_data = await sync_to_async(lambda: list(walk(opath, topdown=False)))
    for dirpath, _, files in walk_data:
        for filee in files:
            f_path = ospath.join(dirpath, filee)
            if filee.strip().endswith(".parts") and filee.startswith("."):
                await remove(f_path)
        if dirpath.strip().endswith(".unwanted"):
            await aiormtree(dirpath, ignore_errors=True)
    walk_data = await sync_to_async(lambda: list(walk(opath, topdown=False)))
    for dirpath, _, files in walk_data:
        if not await listdir(dirpath):
            await rmdir(dirpath)


async def walk_files(opath, topdown=False):
    """Every file under *opath*, deepest directory first by default.

    ``os.walk`` blocks, so the whole tree is drained into a list off the event
    loop before any of the paths are touched -- which is also what makes the
    deepest-first order safe to move or delete through.
    """
    walk_data = await sync_to_async(lambda: list(walk(opath, topdown=topdown)))
    return [
        ospath.join(dirpath, file_)
        for dirpath, _, files in walk_data
        for file_ in files
    ]


async def get_path_size(opath):
    total_size = 0
    if await aiopath.isfile(opath):
        if await aiopath.islink(opath):
            opath = await aioreadlink(opath)
        return await aiopath.getsize(opath)
    for abs_path in await walk_files(opath):
        if await aiopath.islink(abs_path):
            abs_path = await aioreadlink(abs_path)
        total_size += await aiopath.getsize(abs_path)
    return total_size


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
    walk_data = await sync_to_async(lambda: list(walk(fpath)))
    for root, _, files in walk_data:
        if root.strip().endswith("/yt-dlp-thumb"):
            continue
        for f in files:
            if should_remove(f):
                await remove(ospath.join(root, f))


async def remove_excluded_files(fpath, ee):
    await _remove_walked(fpath, lambda f: f.strip().lower().endswith(tuple(ee)))


async def remove_non_included_files(fpath, ie):
    await _remove_walked(fpath, lambda f: not f.strip().lower().endswith(tuple(ie)))


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
