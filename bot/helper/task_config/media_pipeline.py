from asyncio import gather
from os import path as ospath
from os import walk
from re import I, sub
from shlex import split

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, remove
from aiofiles.os import path as aiopath
from natsort import natsorted

from ... import (
    LOGGER,
    cores,
    cpu_eater_lock,
    task_dict,
    task_dict_lock,
)
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.files_utils import (
    SevenZ,
    get_base_name,
    get_path_size,
    is_archive,
    is_archive_split,
    is_first_archive_split,
    split_file,
    walk_files,
)
from ..ext_utils.links_utils import is_telegram_link
from ..ext_utils.media_utils import (
    FFMpeg,
    ffconcat_escape,
    get_document_type,
    take_ss,
)
from ..ext_utils.shutil_helper import move, rmtree
from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus
from ..mirror_leech_utils.status_utils.sevenz_status import SevenZStatus
from ..telegram_helper.message_utils import get_tg_link_message, temp_download
from ._host import TaskConfigHost


def is_extractable(file_):
    """What ``proceed_extract`` will hand to 7z.

    A whole archive or the first part of a split one, but a plain ``.rar`` is
    left alone -- only its ``.partN.rar`` form, which the split check catches, is
    picked up. Both loops in ``proceed_extract`` decide with this, and they used
    to spell the same three clauses out separately.
    """
    return (
        is_first_archive_split(file_)
        or is_archive(file_)
        and not file_.strip().lower().endswith(".rar")
    )


class MediaPipelineMixin(TaskConfigHost):
    """The steps ``TaskListener`` runs over a finished download.

    Every step takes the path it should work on and answers with the path the
    next one should work on -- which is how ``_run_stage`` threads them together.
    A step that gives up mid-way answers ``False`` instead, and sets
    ``is_cancelled`` on the way out; ``_run_stage`` checks that flag right after
    the call and drops the answer, so the sentinel never reaches a path
    operation. It is in the signatures because it is real, not because a caller
    is expected to do something with it.
    """

    async def _ffmpeg_status(self, gid: str, status: str):
        """An ffmpeg driver, registered as what this task is doing now."""
        ffmpeg = FFMpeg(self)
        async with task_dict_lock:
            task_dict[self.mid] = FFmpegStatus(self, ffmpeg, gid, status)
        return ffmpeg

    async def _sevenz_status(self, gid: str, status: str):
        sevenz = SevenZ(self)
        async with task_dict_lock:
            task_dict[self.mid] = SevenZStatus(self, sevenz, gid, status)
        return sevenz

    async def _claim_cpu(self, ffmpeg, gid: str) -> None:
        """Show the ffmpeg status line and wait for the CPU to be free.

        ``progress`` goes off while the task queues for the lock: it has nothing
        to report until it is actually running, and the status list says
        "queued" rather than 0% forever.
        """
        async with task_dict_lock:
            task_dict[self.mid] = FFmpegStatus(self, ffmpeg, gid, "FFmpeg")
        self.progress = False
        await cpu_eater_lock.acquire()
        self.progress = True

    async def proceed_extract(self, dl_path: str, gid: str) -> str | bool:
        pswd = self.extract if isinstance(self.extract, str) else ""
        self.files_to_proceed = []
        if self.is_file and is_archive(dl_path):
            self.files_to_proceed.append(dl_path)
        else:
            for f_path in await walk_files(dl_path):
                if is_extractable(ospath.basename(f_path)):
                    self.files_to_proceed.append(f_path)

        if not self.files_to_proceed:
            return dl_path
        t_path = dl_path
        # `code` feeds the `return` at the end of the method; it is reset per
        # directory inside the loop, which never runs if the tree is empty.
        code = 0
        LOGGER.info(f"Extracting: {self.name}")
        sevenz = await self._sevenz_status(gid, "Extract")
        walk_data = await sync_to_async(
            lambda: list(walk(self.up_dir or self.dir, topdown=False))
        )
        for dirpath, _, files in walk_data:
            code = 0
            for file_ in files:
                if self.is_cancelled:
                    return False
                if is_extractable(file_):
                    self.proceed_count += 1
                    f_path = ospath.join(dirpath, file_)
                    t_path = get_base_name(f_path) if self.is_file else dirpath
                    if not self.is_file:
                        self.subname = file_
                    code = await sevenz.extract(f_path, t_path, pswd)
            if self.is_cancelled:
                return False
            if code == 0:
                for file_ in files:
                    if is_archive_split(file_) or is_archive(file_):
                        del_path = ospath.join(dirpath, file_)
                        try:
                            await remove(del_path)
                        except OSError:
                            self.is_cancelled = True
        if self.proceed_count == 0:
            LOGGER.info("No files able to extract!")
        return t_path if self.is_file and code == 0 else dl_path

    async def proceed_ffmpeg(self, dl_path: str, gid: str) -> str | bool:
        checked = False
        inputs = {}
        cmds = [
            [part.strip() for part in split(item) if part.strip()]
            for item in self.ffmpeg_cmds or []
        ]
        try:
            ffmpeg = FFMpeg(self)
            for ffmpeg_cmd in cmds:
                self.proceed_count = 0
                cmd = [
                    "taskset",
                    "-c",
                    f"{cores}",
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-progress",
                    "pipe:1",
                ] + ffmpeg_cmd
                if "-del" in cmd:
                    cmd.remove("-del")
                    delete_files = True
                else:
                    delete_files = False
                input_indexes = [
                    index for index, value in enumerate(cmd) if value == "-i"
                ]
                input_file = next(
                    (
                        cmd[index + 1]
                        for index in input_indexes
                        if cmd[index + 1].startswith("mltb")
                    ),
                    "",
                )
                if not input_file:
                    LOGGER.error("Wrong FFmpeg cmd!")
                    return dl_path
                if input_file.strip().endswith(".video") or input_file.strip().endswith(
                    ".txt"
                ):
                    ext = "video"
                elif input_file.strip().endswith(".audio"):
                    ext = "audio"
                elif "." not in input_file:
                    ext = "all"
                else:
                    ext = ospath.splitext(input_file)[-1].lower()
                if await aiopath.isfile(dl_path):
                    is_video, is_audio, _ = await get_document_type(dl_path)
                    if not is_video and not is_audio:
                        break
                    elif is_video and ext == "audio":
                        break
                    elif is_audio and not is_video and ext == "video":
                        break
                    elif ext not in [
                        "all",
                        "audio",
                        "video",
                    ] and not dl_path.strip().lower().endswith(ext):
                        break
                    new_folder = ospath.splitext(dl_path)[0]
                    name = ospath.basename(dl_path)
                    await makedirs(new_folder, exist_ok=True)
                    file_path = f"{new_folder}/{name}"
                    await move(dl_path, file_path)
                    if not checked:
                        checked = True
                        await self._claim_cpu(ffmpeg, gid)
                    LOGGER.info(f"Running ffmpeg cmd for: {file_path}")
                    var_cmd = cmd.copy()
                    for index in input_indexes:
                        if cmd[index + 1].startswith("mltb"):
                            var_cmd[index + 1] = file_path
                        elif is_telegram_link(cmd[index + 1]):
                            msg = (
                                await get_tg_link_message(cmd[index + 1], self.user_id)
                            )[0]
                            file_dir = await temp_download(msg)
                            inputs[index + 1] = file_dir
                            var_cmd[index + 1] = file_dir
                    self.subsize = self.size
                    res = await ffmpeg.ffmpeg_cmds(var_cmd, file_path)
                    if res:
                        if delete_files:
                            await remove(file_path)
                            if len(await listdir(new_folder)) == 1:
                                folder = new_folder.rsplit("/", 1)[0]
                                self.name = ospath.basename(res[0])
                                if self.name.startswith("ffmpeg"):
                                    self.name = self.name.split(".", 1)[-1]
                                dl_path = ospath.join(folder, self.name)
                                await move(res[0], dl_path)
                                await rmtree(new_folder)
                            else:
                                dl_path = new_folder
                                self.name = new_folder.rsplit("/", 1)[-1]
                        else:
                            dl_path = new_folder
                            self.name = new_folder.rsplit("/", 1)[-1]
                    else:
                        await move(file_path, dl_path)
                        await rmtree(new_folder)
                else:
                    walk_data = await sync_to_async(
                        lambda: list(walk(dl_path, topdown=False))
                    )
                    for dirpath, _, files in natsorted(walk_data):
                        f_path = []
                        for file_ in natsorted(files):
                            if (
                                ospath.join(dirpath, file_) in f_path
                                or file_ == "mltb.txt"
                            ):
                                continue
                            var_cmd = cmd.copy()
                            if self.is_cancelled:
                                return False
                            if "concat" not in var_cmd:
                                f_path = ospath.join(dirpath, file_)
                                is_video, is_audio, _ = await get_document_type(f_path)
                                if not is_video and not is_audio:
                                    continue
                                elif is_video and ext == "audio":
                                    continue
                                elif is_audio and not is_video and ext == "video":
                                    continue
                                elif ext not in [
                                    "all",
                                    "audio",
                                    "video",
                                ] and not f_path.strip().lower().endswith(ext):
                                    continue
                            self.proceed_count += 1
                            for index in input_indexes:
                                if cmd[index + 1].startswith("mltb"):
                                    if cmd[index + 1].endswith("txt"):
                                        txt = ""
                                        for mf in natsorted(files):
                                            df = ospath.join(dirpath, mf)
                                            if (await get_document_type(df))[0]:
                                                f_path.append(df)
                                                txt += f"file '{ffconcat_escape(df)}'\n"
                                        async with aiopen(
                                            f"{dirpath}/mltb.txt", "w"
                                        ) as f:
                                            await f.write(txt)
                                        var_cmd[index + 1] = f"{dirpath}/mltb.txt"
                                    else:
                                        # The one file this iteration is for.
                                        # ``f_path`` holds two things by design
                                        # -- that path, or the list of files a
                                        # concat folded together, which the
                                        # cleanup below tells apart with an
                                        # ``isinstance``. Only the path form
                                        # belongs in a command line, and a
                                        # concat names its input as mltb.txt,
                                        # which is the branch above.
                                        var_cmd[index + 1] = f_path  # pyrefly: ignore[unsupported-operation]
                                elif is_telegram_link(cmd[index + 1]):
                                    msg = (
                                        await get_tg_link_message(
                                            cmd[index + 1], self.user_id
                                        )
                                    )[0]
                                    file_dir = await temp_download(msg)
                                    inputs[index + 1] = file_dir
                                    var_cmd[index + 1] = file_dir
                            if not checked:
                                checked = True
                                await self._claim_cpu(ffmpeg, gid)
                            LOGGER.info(f"Running ffmpeg cmd for: {f_path}")
                            if isinstance(f_path, list):
                                self.subsize = 0
                                for mf in f_path:
                                    self.subsize += await get_path_size(mf)
                            else:
                                self.subsize = await get_path_size(f_path)
                            self.subname = file_
                            res = await ffmpeg.ffmpeg_cmds(var_cmd, f_path)
                            if res and delete_files:
                                if isinstance(f_path, list):
                                    for mf in f_path:
                                        await remove(mf)
                                else:
                                    await remove(f_path)
                                if len(res) == 1:
                                    file_name = ospath.basename(res[0])
                                    if file_name.startswith("ffmpeg"):
                                        newname = file_name.split(".", 1)[-1]
                                        newres = ospath.join(dirpath, newname)
                                        await move(res[0], newres)
                            if await aiopath.exists(f"{dirpath}/mltb.txt"):
                                await remove(f"{dirpath}/mltb.txt")
                for inp in inputs.values():
                    if "/temp/" in inp and await aiopath.exists(inp):
                        await remove(inp)
        finally:
            if checked:
                cpu_eater_lock.release()
        return dl_path

    async def substitute(self, dl_path: str) -> str:
        def perform_substitution(name, substitutions):
            for substitution in substitutions:
                sen = False
                pattern = substitution[0]
                if pattern.startswith('"') and pattern.endswith('"'):
                    pattern = pattern.strip('"')
                if len(substitution) > 1:
                    if len(substitution) > 2:
                        sen = substitution[2] == "s"
                        res = substitution[1]
                    elif len(substitution[1]) == 0:
                        res = " "
                    else:
                        res = substitution[1]
                else:
                    res = ""
                try:
                    name = sub(pattern, res, name, flags=I if sen else 0)
                except Exception as e:
                    LOGGER.error(
                        f"Substitute Error: pattern: {pattern} res: {res}. Error: {e}"
                    )
                    return False
                if len(name.encode()) > 255:
                    LOGGER.error(f"Substitute: {name} is too long")
                    return False
            return name

        if self.is_file:
            up_dir, name = dl_path.rsplit("/", 1)
            new_name = perform_substitution(name, self.name_sub)
            if not new_name:
                return dl_path
            new_path = ospath.join(up_dir, new_name)
            await move(dl_path, new_path)
            return new_path
        else:
            for f_path in await walk_files(dl_path):
                new_name = perform_substitution(ospath.basename(f_path), self.name_sub)
                if not new_name:
                    continue
                await move(f_path, ospath.join(ospath.dirname(f_path), new_name))
            return dl_path

    async def generate_screenshots(self, dl_path: str) -> str:
        """Where the file ended up: a new folder beside the screenshots, or
        ``dl_path`` when it was left where it was."""
        ss_nb = int(self.screen_shots) if isinstance(self.screen_shots, str) else 10
        if self.is_file:
            if (await get_document_type(dl_path))[0]:
                LOGGER.info(f"Creating Screenshot for: {dl_path}")
                res = await take_ss(dl_path, ss_nb)
                if res:
                    new_folder = ospath.splitext(dl_path)[0]
                    name = ospath.basename(dl_path)
                    await makedirs(new_folder, exist_ok=True)
                    await gather(
                        move(dl_path, f"{new_folder}/{name}"),
                        move(res, new_folder),
                    )
                    return new_folder
        else:
            LOGGER.info(f"Creating Screenshot for: {dl_path}")
            for f_path in await walk_files(dl_path):
                if (await get_document_type(f_path))[0]:
                    await take_ss(f_path, ss_nb)
        return dl_path

    async def convert_media(self, dl_path: str, gid: str) -> str | bool:
        fvext = []
        if isinstance(self.convert_video, str) and self.convert_video:
            vdata = self.convert_video.split()
            vext = vdata[0].lower()
            if len(vdata) > 2:
                if "+" in vdata[1].split():
                    vstatus = "+"
                elif "-" in vdata[1].split():
                    vstatus = "-"
                else:
                    vstatus = ""
                fvext.extend(f".{ext.lower()}" for ext in vdata[2:])
            else:
                vstatus = ""
        else:
            vext = ""
            vstatus = ""

        faext = []
        if isinstance(self.convert_audio, str) and self.convert_audio:
            adata = self.convert_audio.split()
            aext = adata[0].lower()
            if len(adata) > 2:
                if "+" in adata[1].split():
                    astatus = "+"
                elif "-" in adata[1].split():
                    astatus = "-"
                else:
                    astatus = ""
                faext.extend(f".{ext.lower()}" for ext in adata[2:])
            else:
                astatus = ""
        else:
            aext = ""
            astatus = ""

        self.files_to_proceed = {}
        all_files = []
        if self.is_file:
            all_files.append(dl_path)
        else:
            all_files = await walk_files(dl_path)

        for f_path in all_files:
            is_video, is_audio, _ = await get_document_type(f_path)
            if (
                is_video
                and vext
                and not f_path.strip().lower().endswith(f".{vext}")
                and (
                    vstatus == "+"
                    and f_path.strip().lower().endswith(tuple(fvext))
                    or vstatus == "-"
                    and not f_path.strip().lower().endswith(tuple(fvext))
                    or not vstatus
                )
            ):
                self.files_to_proceed[f_path] = "video"
            elif (
                is_audio
                and aext
                and not is_video
                and not f_path.strip().lower().endswith(f".{aext}")
                and (
                    astatus == "+"
                    and f_path.strip().lower().endswith(tuple(faext))
                    or astatus == "-"
                    and not f_path.strip().lower().endswith(tuple(faext))
                    or not astatus
                )
            ):
                self.files_to_proceed[f_path] = "audio"
        del all_files

        if self.files_to_proceed:
            ffmpeg = await self._ffmpeg_status(gid, "Convert")
            self.progress = False
            async with cpu_eater_lock:
                self.progress = True
                for f_path, f_type in self.files_to_proceed.items():
                    self.proceed_count += 1
                    LOGGER.info(f"Converting: {f_path}")
                    if self.is_file:
                        self.subsize = self.size
                    else:
                        self.subsize = await get_path_size(f_path)
                        self.subname = ospath.basename(f_path)
                    if f_type == "video":
                        res = await ffmpeg.convert_video(f_path, vext)
                    else:
                        res = await ffmpeg.convert_audio(f_path, aext)
                    if res:
                        try:
                            await remove(f_path)
                        except OSError:
                            self.is_cancelled = True
                            return False
                        if self.is_file:
                            return res
        return dl_path

    async def generate_sample_video(self, dl_path: str, gid: str) -> str:
        data = (
            self.sample_video.split(":") if isinstance(self.sample_video, str) else ""
        )
        if data:
            sample_duration = int(data[0]) if data[0] else 60
            part_duration = int(data[1]) if len(data) > 1 else 4
        else:
            sample_duration = 60
            part_duration = 4

        self.files_to_proceed = {}
        if self.is_file and (await get_document_type(dl_path))[0]:
            file_ = ospath.basename(dl_path)
            self.files_to_proceed[dl_path] = file_
        else:
            for f_path in await walk_files(dl_path):
                if (await get_document_type(f_path))[0]:
                    self.files_to_proceed[f_path] = ospath.basename(f_path)
        if self.files_to_proceed:
            ffmpeg = await self._ffmpeg_status(gid, "Sample Video")
            self.progress = False
            async with cpu_eater_lock:
                self.progress = True
                LOGGER.info(f"Creating Sample video: {self.name}")
                for f_path, file_ in self.files_to_proceed.items():
                    self.proceed_count += 1
                    if self.is_file:
                        self.subsize = self.size
                    else:
                        self.subsize = await get_path_size(f_path)
                        self.subname = file_
                    res = await ffmpeg.sample_video(
                        f_path, sample_duration, part_duration
                    )
                    if res and self.is_file:
                        new_folder = ospath.splitext(f_path)[0]
                        await makedirs(new_folder, exist_ok=True)
                        await gather(
                            move(f_path, f"{new_folder}/{file_}"),
                            move(res, f"{new_folder}/SAMPLE.{file_}"),
                        )
                        return new_folder
        return dl_path

    async def proceed_compress(self, dl_path: str, gid: str) -> str | bool:
        pswd = self.compress if isinstance(self.compress, str) else ""
        if self.is_file:
            new_folder = ospath.splitext(dl_path)[0]
            name = ospath.basename(dl_path)
            await makedirs(new_folder, exist_ok=True)
            new_dl_path = f"{new_folder}/{name}"
            await move(dl_path, new_dl_path)
            dl_path = new_dl_path
            up_path = f"{new_dl_path}.zip"
            self.is_file = False
        else:
            up_path = f"{dl_path}.zip"
        sevenz = await self._sevenz_status(gid, "Zip")
        return await sevenz.zip(dl_path, up_path, pswd)

    async def proceed_split(self, dl_path: str, gid: str) -> None:
        """Split whatever is over the size limit, in place.

        The odd one out: it replaces files under ``dl_path`` instead of moving
        the task to a new path, so it answers with nothing and the caller keeps
        the path it already had.
        """
        # ``_resolve_split_sizes`` reduced the typed "-sp 2g" to a byte count
        # before the download started, so only the number reaches here.
        limit: int = self.split_size  # pyrefly: ignore[bad-assignment]
        self.files_to_proceed = {}
        if self.is_file:
            f_size = await get_path_size(dl_path)
            if f_size > limit:
                self.files_to_proceed[dl_path] = [f_size, ospath.basename(dl_path)]
        else:
            for f_path in await walk_files(dl_path):
                f_size = await get_path_size(f_path)
                if f_size > limit:
                    self.files_to_proceed[f_path] = [f_size, ospath.basename(f_path)]
        if self.files_to_proceed:
            ffmpeg = await self._ffmpeg_status(gid, "Split")
            LOGGER.info(f"Splitting: {self.name}")
            for f_path, (f_size, file_) in self.files_to_proceed.items():
                self.proceed_count += 1
                if self.is_file:
                    self.subsize = self.size
                else:
                    self.subsize = f_size
                    self.subname = file_
                parts = -(-f_size // limit)
                if self.equal_splits:
                    split_size = (f_size // parts) + (f_size % parts)
                else:
                    split_size = limit
                if not self.as_doc and (await get_document_type(f_path))[0]:
                    self.progress = True
                    res = await ffmpeg.split(f_path, file_, parts, split_size)
                else:
                    self.progress = False
                    res = await split_file(f_path, split_size, self)
                if self.is_cancelled:
                    return
                if res or f_size >= self.max_split_size:
                    try:
                        await remove(f_path)
                    except OSError:
                        self.is_cancelled = True
