import ast
from collections import Counter
from copy import deepcopy
from re import findall

from ... import (
    LOGGER,
    excluded_extensions,
    included_extensions,
)
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient
from ..ext_utils.bot_utils import get_size_bytes
from ..ext_utils.links_utils import is_telegram_link
from ..ext_utils.media_utils import create_thumb
from ..telegram_helper.dest_chat import (
    ChatLookupError,
    can_reach_dest,
    get_dest_chat,
    get_dest_member,
)
from ..telegram_helper.message_utils import get_tg_link_message

# A destination the bot can post many files into and clean up after; anything
# else (a PM, most of all) is handled as a plain chat.
GROUP_CHAT_TYPES = ("SUPERGROUP", "CHANNEL", "GROUP", "FORUM")

# Telegram's per-file ceiling for a bot token; a premium user session raises it.
BOT_MAX_SPLIT_SIZE = 2097152000

TRANSMISSION_PREFIXES = ("b:", "u:", "h:")


def _setting_for(user_dict, key, global_value, when_set=""):
    """The value of ``key`` for this task, preferring the user's own setting.

    A key *present* in ``user_dict`` means the user has an opinion about it --
    even an empty one -- so the bot-wide default no longer applies and
    ``when_set`` is used instead.
    """
    return user_dict.get(key) or (global_value if key not in user_dict else when_set)


def _is_enabled(user_dict, key, global_value) -> bool:
    """Whether a boolean setting is on, preferring the user's own choice."""
    return bool(user_dict.get(key) or (global_value and key not in user_dict))


def _as_chat_id(value):
    """A chat or thread id as an int when it looks numeric, else untouched."""
    return int(value) if value.lstrip("-").isdigit() else value


def _can_manage_and_delete(member) -> bool:
    """Whether an account can both manage the chat and delete messages in it."""
    return (
        member.privileges.can_manage_chat and member.privileges.can_delete_messages
    )


def _as_dump_entries(chats):
    """The configured clone dump chats as a list, whatever shape they came in."""
    if isinstance(chats, int):
        return [chats]
    if isinstance(chats, str):
        if chats.startswith("[") and chats.endswith("]"):
            return ast.literal_eval(chats)
        return [chats]
    return chats


class SettingsResolverMixin:
    def _dest_unverified(self, what, error) -> None:
        """Handle a destination check that could not be completed.

        Telegram was never answered successfully, so nothing new is known about
        the chat -- "Chat not found!" or "not admin" would be a guess, and during
        a bulk it is the wrong guess for every link in the batch. Hybrid leech
        can simply stay off, the upload still goes through the user session; a
        bot-only upload has to stop and say that the *check* failed.
        """
        if self.user_transmission:
            LOGGER.warning(
                f"Hybrid leech off for this task, can't check {what}: {error}"
            )
            self.hybrid_leech = False
            return
        raise ValueError(
            f"Can't check {what} right now: {error}. Try again in a moment."
        )

    async def before_start(self) -> None:
        """Settle every task setting that was left to a default.

        Order matters: the destination has the last word on which session
        uploads, and that in turn decides how large a split may be.
        """
        self._resolve_name_substitutions()
        self._resolve_extension_filters()
        self._resolve_ffmpeg_commands()
        await self._resolve_upload_destination()
        self._resolve_split_sizes()
        self._resolve_upload_format()
        self._resolve_thumbnail_layout()
        self._resolve_clone_dump_chats()
        await self._resolve_thumbnail()

    # ── plain settings ──────────────────────────────────────────────────

    def _resolve_name_substitutions(self) -> None:
        """Parse the rename rules from ``old/new | old/new`` into pairs."""
        self.name_sub = self.name_sub or _setting_for(
            self.user_dict, "NAME_SUBSTITUTE", Config.NAME_SUBSTITUTE
        )
        if self.name_sub:
            self.name_sub = [x.split("/") for x in self.name_sub.split(" | ")]

    def _resolve_extension_filters(self) -> None:
        """Decide which file extensions this task keeps and which it drops."""
        self.excluded_extensions = _setting_for(
            self.user_dict,
            "EXCLUDED_EXTENSIONS",
            excluded_extensions,
            ["aria2", "!qB"],
        )
        self.included_extensions = _setting_for(
            self.user_dict, "INCLUDED_EXTENSIONS", included_extensions, []
        )

    def _resolve_upload_format(self) -> None:
        """Decide whether files go up as documents or as playable media."""
        if self.as_doc:
            return
        self.as_doc = (
            False
            if self.as_med
            else _is_enabled(self.user_dict, "AS_DOCUMENT", Config.AS_DOCUMENT)
        )

    def _resolve_thumbnail_layout(self) -> None:
        """Settle how many thumbnails per row a grid preview gets."""
        self.thumbnail_layout = self.thumbnail_layout or _setting_for(
            self.user_dict, "THUMBNAIL_LAYOUT", Config.THUMBNAIL_LAYOUT
        )

    async def _resolve_thumbnail(self) -> None:
        """Turn a telegram link thumbnail into a file on disk."""
        if self.thumb == "none" or not is_telegram_link(self.thumb):
            return
        msg = (await get_tg_link_message(self.thumb, self.user_id))[0]
        self.thumb = await create_thumb(msg) if msg.photo or msg.document else ""

    # ── ffmpeg presets ──────────────────────────────────────────────────

    def _resolve_ffmpeg_commands(self) -> None:
        """Turn the requested preset names into runnable command lines."""
        if not self.ffmpeg_cmds:
            return
        presets = self._ffmpeg_presets()
        cmds = []
        for key in list(self.ffmpeg_cmds):
            if isinstance(key, tuple):
                cmds.extend(key)
            elif presets and key in presets:
                cmds.extend(self._fill_preset(key, presets[key]))
        self.ffmpeg_cmds = cmds

    def _ffmpeg_presets(self):
        """The named command sets this task may draw on, user's before bot's."""
        if self.user_dict.get("FFMPEG_CMDS"):
            return deepcopy(self.user_dict["FFMPEG_CMDS"])
        if Config.FFMPEG_CMDS:
            return deepcopy(Config.FFMPEG_CMDS)
        return None

    def _fill_preset(self, key, templates):
        """The usable commands of one preset, dropping any left with holes.

        A template keeps its ``{placeholders}`` only when the user supplied
        exactly that set of variables; filling in a subset would hand ffmpeg a
        command line with braces still in it.
        """
        for index, template in enumerate(templates):
            variables = set(findall(r"\{(.*?)\}", template))
            if not variables:
                yield template
                continue
            values = (
                self.user_dict.get("FFMPEG_VARIABLES", {})
                .get(key, {})
                .get(str(index), {})
            )
            if Counter(list(variables)) == Counter(list(values.keys())):
                yield template.format(**values)

    # ── upload destination and the session that uploads ─────────────────

    async def _resolve_upload_destination(self) -> None:
        """Settle where the files go and which session is able to send them."""
        self._apply_transmission_defaults()
        if self.up_dest:
            self._normalize_up_dest()
            if self.user_transmission:
                await self._verify_dest_for_user_session()
            if not self.user_transmission or self.hybrid_leech:
                await self._verify_dest_for_bot()
        elif (self.user_transmission or self.hybrid_leech) and not self.is_super_chat:
            # uploading back into a PM: neither trick applies
            self._downgrade_to_bot_session()

    def _downgrade_to_bot_session(self) -> None:
        """Give up on the user session for this task."""
        self.user_transmission = False
        self.hybrid_leech = False

    def _apply_transmission_defaults(self) -> None:
        """Pick the uploading session before the destination gets a say."""
        self.user_transmission = _is_enabled(
            self.user_dict, "USER_TRANSMISSION", Config.USER_TRANSMISSION
        )
        self.up_dest = self.up_dest or Config.LEECH_DUMP_CHAT
        self.hybrid_leech = bool(
            TgClient.IS_PREMIUM_USER
            and _is_enabled(self.user_dict, "HYBRID_LEECH", Config.HYBRID_LEECH)
        )
        if self.bot_trans:
            self._downgrade_to_bot_session()
        if self.user_trans:
            self.user_transmission = True

    def _normalize_up_dest(self) -> None:
        """Reduce the destination the user typed to a chat id and thread id."""
        if isinstance(self.up_dest, int):
            return
        self._apply_transmission_prefix()
        if "|" in self.up_dest:
            chat, thread = self.up_dest.split("|", 1)
            self.up_dest = _as_chat_id(chat)
            self.chat_thread_id = _as_chat_id(thread)
        elif self.up_dest.lstrip("-").isdigit():
            self.up_dest = int(self.up_dest)
        elif self.up_dest.lower() == "pm":
            self.up_dest = self.user_id

    def _apply_transmission_prefix(self) -> None:
        """Let a ``b:``/``u:``/``h:`` prefix override the uploading session."""
        prefix = self.up_dest[:2]
        if prefix not in TRANSMISSION_PREFIXES:
            return
        self.up_dest = self.up_dest[2:]
        if prefix == "b:":
            self._downgrade_to_bot_session()
        elif prefix == "u:":
            self.user_transmission = True
        else:
            self.user_transmission = True
            self.hybrid_leech = bool(TgClient.IS_PREMIUM_USER)

    async def _verify_dest_for_user_session(self) -> None:
        """Downgrade to the bot unless the user session can manage the chat.

        The user session is an optimisation, never a requirement, so every
        failure here costs user transmission and hybrid leech and nothing more.
        """
        try:
            chat = await get_dest_chat(TgClient.user, self.up_dest)
        except ChatLookupError as e:
            # a rate limit is not an answer about the chat, but the user
            # session has nothing to fall back on here, so the task goes
            # on through the bot rather than failing
            LOGGER.warning(
                f"Can't check the destination chat for the user session: {e}"
            )
            chat = None
        if chat is None:
            LOGGER.warning(
                "Account of user session can't find the the destination chat!"
            )
            self._downgrade_to_bot_session()
        elif chat.type.name not in GROUP_CHAT_TYPES:
            self._downgrade_to_bot_session()
        elif not chat.is_admin:
            LOGGER.warning(
                "Promote the account of the user session to admin in the chat"
                " to get the benefit of user transmission!"
            )
            self._downgrade_to_bot_session()
        else:
            await self._verify_user_session_privileges(chat)

    async def _verify_user_session_privileges(self, chat) -> None:
        """Check the user session may manage the chat and delete its messages."""
        try:
            member = await get_dest_member(
                TgClient.user, chat.id, TgClient.user.me.id
            )
        except ChatLookupError as e:
            LOGGER.warning(
                "Can't check the privileges of the user session in "
                f"the destination chat: {e}"
            )
            self._downgrade_to_bot_session()
            return
        if not _can_manage_and_delete(member):
            self._downgrade_to_bot_session()
            LOGGER.warning(
                "Enable manage chat and delete messages to account of the user"
                " session from administration settings!"
            )

    async def _verify_dest_for_bot(self) -> None:
        """Make sure the bot itself can post to the destination, or stop."""
        chat = None
        try:
            chat = await get_dest_chat(self.client, self.up_dest)
        except ChatLookupError as e:
            # "could not ask" must not come out as "not there": that is
            # what turned one FloodWait during a bulk into a batch of
            # links reported dead while every one of them was fine
            self._dest_unverified("the destination chat", e)
        if chat is None:
            if not self.user_transmission:
                raise ValueError("Chat not found!")
            self.hybrid_leech = False
        elif chat.type.name in GROUP_CHAT_TYPES:
            await self._verify_bot_privileges(chat)
        else:
            await self._verify_bot_can_reach_dest()

    async def _verify_bot_privileges(self, chat) -> None:
        """Check the bot may manage the destination and delete its messages."""
        if not chat.is_admin:
            raise ValueError("Bot is not admin in the destination chat!")
        member = None
        try:
            member = await get_dest_member(self.client, chat.id, self.client.me.id)
        except ChatLookupError as e:
            self._dest_unverified("the bot's privileges", e)
        if member is not None and not _can_manage_and_delete(member):
            if not self.user_transmission:
                raise ValueError(
                    "You don't have enough privileges in this chat! Enable"
                    " manage chat and delete messages for this bot!"
                )
            self.hybrid_leech = False

    async def _verify_bot_can_reach_dest(self) -> None:
        """Check a non-group destination has actually started the bot."""
        # unverified means unverified: an unanswered probe leaves this True so
        # the task is not told to start a bot it has already started
        reachable = True
        try:
            reachable = await can_reach_dest(self.client, self.up_dest)
        except ChatLookupError as e:
            self._dest_unverified("the destination chat", e)
        if not reachable:
            raise ValueError("Start the bot and try again!")

    # ── splitting ───────────────────────────────────────────────────────

    def _resolve_split_sizes(self) -> None:
        """Fix the split size and the ceiling telegram will accept for it."""
        if self.split_size:
            self.split_size = (
                int(self.split_size)
                if self.split_size.isdigit()
                else get_size_bytes(self.split_size)
            )
        self.split_size = (
            self.split_size
            or self.user_dict.get("LEECH_SPLIT_SIZE")
            or Config.LEECH_SPLIT_SIZE
        )
        self.equal_splits = _is_enabled(
            self.user_dict, "EQUAL_SPLITS", Config.EQUAL_SPLITS
        )
        self.max_split_size = (
            TgClient.MAX_SPLIT_SIZE
            if self.user_transmission and TgClient.IS_PREMIUM_USER
            else BOT_MAX_SPLIT_SIZE
        )
        self.split_size = min(self.split_size, self.max_split_size)

    # ── clone dump chats ────────────────────────────────────────────────

    def _resolve_clone_dump_chats(self) -> None:
        """Index every extra dump chat by id, ready to record what was sent."""
        self.clone_dump_chats = (
            _setting_for(
                self.user_dict, "CLONE_DUMP_CHATS", Config.CLONE_DUMP_CHATS, {}
            )
            or {}
        )
        if not self.clone_dump_chats:
            return
        self.clone_dump_chats = {
            chat_id: {"thread_id": thread_id, "last_sent_msg": None}
            for chat_id, thread_id in map(
                self._as_dump_target, _as_dump_entries(self.clone_dump_chats)
            )
        }

    def _as_dump_target(self, entry):
        """One configured dump chat as a ``(chat_id, thread_id)`` pair."""
        if not isinstance(entry, str):
            return entry, None
        if "|" in entry:
            chat, thread = entry.split("|", 1)
            return _as_chat_id(chat), _as_chat_id(thread)
        if entry.lower() == "pm":
            return self.user_id, None
        return _as_chat_id(entry), None
