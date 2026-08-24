from aiofiles.os import path as aiopath
from base64 import b64encode
from secrets import token_urlsafe

from .. import LOGGER, bot_loop, multi_tags, DOWNLOAD_DIR
from ..helper.ext_utils.bot_utils import COMMAND_USAGE
from ..helper.ext_utils.links_utils import (
    is_url,
    is_magnet,
    is_telegram_link,
)
from ..helper.ext_utils.task_args import parse_leech_args, strip_link_tokens
from ..helper.listeners.command_task import CommandTask
from ..helper.mirror_leech_utils.download_utils.aria2_download import (
    add_aria2_download,
)
from ..helper.mirror_leech_utils.download_utils.direct_downloader import (
    add_direct_download,
)
from ..helper.mirror_leech_utils.download_utils.link_resolver import (
    resolve_torbox_torrent,
    resolve_alldebrid_torrent,
    resolve_torbox_web,
    resolve_alldebrid_web,
    resolve_direct_link,
    resolve_pornhub,
)
from ..helper.mirror_leech_utils.download_utils.mega_download import (
    add_mega_download,
)
from ..helper.mirror_leech_utils.download_utils.pornhub_download import (
    add_pornhub_download,
)
from ..helper.mirror_leech_utils.download_utils.qbit_download import add_qb_torrent
from ..helper.mirror_leech_utils.download_utils.telegram_download import (
    TelegramDownloadHelper,
)
from ..helper.mirror_leech_utils.download_utils.vidara_download import (
    add_vidara_download,
)
from ..helper.mirror_leech_utils.download_utils.yt_dlp_download import (
    add_ytdlp_download,
)
from ..helper.telegram_helper.message_utils import send_message, get_tg_link_message


class Leech(CommandTask):
    async def new_event(self):
        text = self.cmd_text.split("\n")
        input_list = text[0].split(" ")

        args = parse_leech_args(input_list[1:])

        self._apply_args(args)

        headers = args.headers

        if args.is_bulk:
            await self.init_bulk(input_list, args.bulk_start, args.bulk_end, Leech)
            return

        await self.register_same_dir()

        if len(self.bulk) != 0:
            del self.bulk[0]

        await self.run_multi(input_list, Leech)
        await self.get_tag(text)

        path = f"{DOWNLOAD_DIR}{self.mid}{self.folder_name}"

        result = await self._resolve_reply(input_list)
        if result is None:
            # _resolve_reply handled everything (bulk dispatch or error)
            return
        reply_to, file_, session = result

        if not await self._validate_link(file_):
            return

        if len(self.link) > 0:
            LOGGER.info(self.link)

        try:
            await self.before_start()
        except Exception as e:
            await self.fail_task(e)
            return

        if not await self._resolve_links(headers, file_):
            return

        await self._dispatch_download(path, args, headers, file_, reply_to, session)

    # ── arg application ─────────────────────────────────────────────

    def _apply_args(self, args):
        """The shared options, plus the ones only a leech takes."""
        super()._apply_args(args)
        self.seed = args.seed
        self.extract = args.extract
        self.join = args.join
        self.is_alldebrid = args.is_alldebrid
        self.is_torbox = args.is_torbox
        self.stream_upload = args.stream_upload

    # ── reply / link resolution ─────────────────────────────────────

    async def _resolve_reply(self, input_list):
        """Resolve the reply message, handling telegram links and bulk.

        Returns ``(reply_to, file_, session)`` on success, or ``None``
        if the method handled everything (bulk dispatch or error).
        """
        reply_to = None
        file_ = None
        session = ""

        if not self.link and (reply_to := self.message.reply_to_message):
            if reply_to.text:
                self.link = reply_to.text.split("\n", 1)[0].strip()

        if is_telegram_link(self.link):
            try:
                reply_to, session = await get_tg_link_message(
                    self.link, user_id=self.user_id
                )
            except Exception as e:
                await self.fail_task(f"ERROR: {e}")
                return None

        if isinstance(reply_to, list):
            await self._handle_bulk(reply_to, input_list)
            return None

        if reply_to:
            file_ = (
                reply_to.document
                or reply_to.photo
                or reply_to.video
                or reply_to.audio
                or reply_to.voice
                or reply_to.video_note
                or reply_to.sticker
                or reply_to.animation
                or None
            )

            if file_ is None:
                if reply_text := reply_to.text:
                    self.link = reply_text.split("\n", 1)[0].strip()
                else:
                    reply_to = None
            elif (document := reply_to.document) and (
                # ``document`` and not ``file_``: they are the same object here
                # -- a document is first in the chain above, so a reply carrying
                # one is what ``file_`` came from -- but only the document is
                # known to have these two fields, and neither is guaranteed to
                # be filled. A document sent without a name used to reach
                # ``.endswith`` on None.
                document.mime_type == "application/x-bittorrent"
                or (document.file_name or "").endswith(".torrent")
            ):
                self.link = await reply_to.download()
                file_ = None

        return reply_to, file_, session

    async def _handle_bulk(self, reply_to, input_list):
        """Handle list-type reply_to (a telegram link range expanded to links)."""
        # the range link itself must go: every child gets its own link in front
        # of these options
        self.options = " ".join(strip_link_tokens(input_list[1:]))

        if not self.multi_tag:
            self.multi_tag = token_urlsafe(3)
            multi_tags.add(self.multi_tag)

        if "-m" not in self.options:
            self.options += f" -m bulk-{self.multi_tag}"

        try:
            await self.dispatch_bulk(input_list[0], reply_to, Leech)
        except Exception as e:
            multi_tags.discard(self.multi_tag)
            LOGGER.error(f"Can't start bulk from telegram links: {e}")
            await send_message(self.message, f"Can't start bulk: {e}")

    async def _validate_link(self, file_):
        """Return *False* (and show usage / fail) if there is no valid input."""
        if (
            not self.link
            and file_ is None
            or is_telegram_link(self.link)
            and file_ is None
            or file_ is None
            and not is_url(self.link)
            and not is_magnet(self.link)
            and not await aiopath.exists(self.link)
        ):
            if not self._batch():
                # one bad link out of a hundred must not paste the whole usage
                # text into the chat; the batch summary names it instead
                await send_message(
                    self.message, COMMAND_USAGE["leech"][0], COMMAND_USAGE["leech"][1]
                )
            await self.fail_task("Invalid or missing link", notify=False)
            return False
        return True

    # ── debrid / direct link resolution ─────────────────────────────

    async def _resolve_links(self, headers, file_):
        """Run torbox / alldebrid / direct-link resolution.

        Returns *False* if the task was failed (caller should return).
        """
        if self.is_torbox:
            if not await resolve_torbox_torrent(self):
                return False

        if self.is_alldebrid:
            if not await resolve_alldebrid_torrent(self):
                return False

        # Web link resolution (not magnet, not torrent, not qbit, no file)
        if (
            self.link
            and isinstance(self.link, str)
            and not self.is_qbit
            and not is_magnet(self.link)
            and not self.link.endswith(".torrent")
            and file_ is None
        ):
            if self.is_torbox:
                if not await resolve_torbox_web(self):
                    return False

            if self.is_alldebrid:
                if not await resolve_alldebrid_web(self):
                    return False

            if isinstance(self.link, str):
                if not await resolve_pornhub(self):
                    return False

            if isinstance(self.link, str):
                result = await resolve_direct_link(self, headers)
                if result is None:
                    return False
                headers.clear()
                headers.extend(result)

        return True

    # ── download dispatch ───────────────────────────────────────────

    async def _dispatch_download(self, path, args, headers, file_, reply_to, session):
        """Start the appropriate downloader."""
        if file_ is not None:
            await TelegramDownloadHelper(self).add_download(
                reply_to, f"{path}/", session
            )
        elif isinstance(self.link, dict):
            if self.link.get("ytdlp"):
                await add_ytdlp_download(self, path)
            elif self.link.get("mega"):
                await add_mega_download(self, path)
            elif self.link.get("pornhub"):
                await add_pornhub_download(self, path)
            elif self.link.get("vidara"):
                await add_vidara_download(self, path)
            else:
                await add_direct_download(self, path)
        elif self.is_qbit:
            await add_qb_torrent(self, path, args.ratio, args.seed_time)
        else:
            if args.ussr or args.pssw:
                auth = f"{args.ussr}:{args.pssw}"
                headers.extend(
                    [f"authorization: Basic {b64encode(auth.encode()).decode('ascii')}"]
                )
            await add_aria2_download(self, path, headers, args.ratio, args.seed_time)


async def leech(client, message):
    bot_loop.create_task(Leech(client, message).new_event())


async def qb_leech(client, message):
    bot_loop.create_task(Leech(client, message, is_qbit=True).new_event())
