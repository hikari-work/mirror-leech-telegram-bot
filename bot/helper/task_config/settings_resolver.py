import ast
from collections import Counter
from copy import deepcopy
from re import findall

from pyrogram.enums import ChatAction

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
from ..telegram_helper.message_utils import get_tg_link_message


class SettingsResolverMixin:
    async def before_start(self) -> None:
        self.name_sub = (
            self.name_sub
            or self.user_dict.get("NAME_SUBSTITUTE", False)
            or (
                Config.NAME_SUBSTITUTE
                if "NAME_SUBSTITUTE" not in self.user_dict
                else ""
            )
        )
        if self.name_sub:
            self.name_sub = [x.split("/") for x in self.name_sub.split(" | ")]
        self.excluded_extensions = self.user_dict.get("EXCLUDED_EXTENSIONS") or (
            excluded_extensions
            if "EXCLUDED_EXTENSIONS" not in self.user_dict
            else ["aria2", "!qB"]
        )
        self.included_extensions = self.user_dict.get("INCLUDED_EXTENSIONS") or (
            included_extensions if "INCLUDED_EXTENSIONS" not in self.user_dict else []
        )
        self.user_transmission = (
            self.user_dict.get("USER_TRANSMISSION")
            or Config.USER_TRANSMISSION
            and "USER_TRANSMISSION" not in self.user_dict
        )

        if self.ffmpeg_cmds:
            if self.user_dict.get("FFMPEG_CMDS", None):
                ffmpeg_dict = deepcopy(self.user_dict["FFMPEG_CMDS"])
            elif (
                "FFMPEG_CMDS" not in self.user_dict or not self.user_dict["FFMPEG_CMDS"]
            ) and Config.FFMPEG_CMDS:
                ffmpeg_dict = deepcopy(Config.FFMPEG_CMDS)
            else:
                ffmpeg_dict = None
            cmds = []
            for key in list(self.ffmpeg_cmds):
                if isinstance(key, tuple):
                    cmds.extend(list(key))
                elif ffmpeg_dict is not None:
                    if key in ffmpeg_dict.keys():
                        for ind, vl in enumerate(ffmpeg_dict[key]):
                            if variables := set(findall(r"\{(.*?)\}", vl)):
                                ff_values = (
                                    self.user_dict.get("FFMPEG_VARIABLES", {})
                                    .get(key, {})
                                    .get(str(ind), {})
                                )
                                if Counter(list(variables)) == Counter(
                                    list(ff_values.keys())
                                ):
                                    cmds.append(vl.format(**ff_values))
                            else:
                                cmds.append(vl)
            self.ffmpeg_cmds = cmds

        self.up_dest = self.up_dest or Config.LEECH_DUMP_CHAT
        self.hybrid_leech = TgClient.IS_PREMIUM_USER and (
            self.user_dict.get("HYBRID_LEECH")
            or Config.HYBRID_LEECH
            and "HYBRID_LEECH" not in self.user_dict
        )
        if self.bot_trans:
            self.user_transmission = False
            self.hybrid_leech = False
        if self.user_trans:
            self.user_transmission = True
        if self.up_dest:
            if not isinstance(self.up_dest, int):
                if self.up_dest.startswith("b:"):
                    self.up_dest = self.up_dest.replace("b:", "", 1)
                    self.user_transmission = False
                    self.hybrid_leech = False
                elif self.up_dest.startswith("u:"):
                    self.up_dest = self.up_dest.replace("u:", "", 1)
                    self.user_transmission = True
                elif self.up_dest.startswith("h:"):
                    self.up_dest = self.up_dest.replace("h:", "", 1)
                    self.user_transmission = True
                    self.hybrid_leech = (
                        self.user_transmission and TgClient.IS_PREMIUM_USER
                    )
                if "|" in self.up_dest:
                    self.up_dest, self.chat_thread_id = list(
                        map(
                            lambda x: int(x) if x.lstrip("-").isdigit() else x,
                            self.up_dest.split("|", 1),
                        )
                    )
                elif self.up_dest.lstrip("-").isdigit():
                    self.up_dest = int(self.up_dest)
                elif self.up_dest.lower() == "pm":
                    self.up_dest = self.user_id

            if self.user_transmission:
                try:
                    chat = await TgClient.user.get_chat(self.up_dest)
                except Exception:
                    chat = None
                if chat is None:
                    LOGGER.warning(
                        "Account of user session can't find the the destination chat!"
                    )
                    self.user_transmission = False
                    self.hybrid_leech = False
                elif chat.type.name not in [
                    "SUPERGROUP",
                    "CHANNEL",
                    "GROUP",
                    "FORUM",
                ]:
                    self.user_transmission = False
                    self.hybrid_leech = False
                elif chat.is_admin:
                    member = await chat.get_member(TgClient.user.me.id)
                    if (
                        not member.privileges.can_manage_chat
                        or not member.privileges.can_delete_messages
                    ):
                        self.user_transmission = False
                        self.hybrid_leech = False
                        LOGGER.warning(
                            "Enable manage chat and delete messages to account of the user session from administration settings!"
                        )
                else:
                    LOGGER.warning(
                        "Promote the account of the user session to admin in the chat to get the benefit of user transmission!"
                    )
                    self.user_transmission = False
                    self.hybrid_leech = False

            if not self.user_transmission or self.hybrid_leech:
                try:
                    chat = await self.client.get_chat(self.up_dest)
                except Exception:
                    chat = None
                if chat is None:
                    if self.user_transmission:
                        self.hybrid_leech = False
                    else:
                        raise ValueError("Chat not found!")
                else:
                    if chat.type.name in [
                        "SUPERGROUP",
                        "CHANNEL",
                        "GROUP",
                        "FORUM",
                    ]:
                        if not chat.is_admin:
                            raise ValueError(
                                "Bot is not admin in the destination chat!"
                            )
                        else:
                            member = await chat.get_member(self.client.me.id)
                            if (
                                not member.privileges.can_manage_chat
                                or not member.privileges.can_delete_messages
                            ):
                                if not self.user_transmission:
                                    raise ValueError(
                                        "You don't have enough privileges in this chat! Enable manage chat and delete messages for this bot!"
                                    )
                                else:
                                    self.hybrid_leech = False
                    else:
                        try:
                            await self.client.send_chat_action(
                                self.up_dest, ChatAction.TYPING
                            )
                        except Exception:
                            raise ValueError("Start the bot and try again!")
        elif (
            self.user_transmission or self.hybrid_leech
        ) and not self.is_super_chat:
            self.user_transmission = False
            self.hybrid_leech = False
        if self.split_size:
            if self.split_size.isdigit():
                self.split_size = int(self.split_size)
            else:
                self.split_size = get_size_bytes(self.split_size)
        self.split_size = (
            self.split_size
            or self.user_dict.get("LEECH_SPLIT_SIZE")
            or Config.LEECH_SPLIT_SIZE
        )
        self.equal_splits = (
            self.user_dict.get("EQUAL_SPLITS")
            or Config.EQUAL_SPLITS
            and "EQUAL_SPLITS" not in self.user_dict
        )
        self.max_split_size = (
            TgClient.MAX_SPLIT_SIZE
            if self.user_transmission and TgClient.IS_PREMIUM_USER
            else 2097152000
        )
        self.split_size = min(self.split_size, self.max_split_size)

        if not self.as_doc:
            self.as_doc = (
                not self.as_med
                if self.as_med
                else (
                    self.user_dict.get("AS_DOCUMENT", False)
                    or Config.AS_DOCUMENT
                    and "AS_DOCUMENT" not in self.user_dict
                )
            )

        self.thumbnail_layout = (
            self.thumbnail_layout
            or self.user_dict.get("THUMBNAIL_LAYOUT", False)
            or (
                Config.THUMBNAIL_LAYOUT
                if "THUMBNAIL_LAYOUT" not in self.user_dict
                else ""
            )
        )

        self.clone_dump_chats = self.user_dict.get("CLONE_DUMP_CHATS", {}) or (
            Config.CLONE_DUMP_CHATS
            if "CLONE_DUMP_CHATS" not in self.user_dict and Config.CLONE_DUMP_CHATS
            else {}
        )
        if self.clone_dump_chats:
            if isinstance(self.clone_dump_chats, int):
                self.clone_dump_chats = [self.clone_dump_chats]
            elif isinstance(self.clone_dump_chats, str):
                if self.clone_dump_chats.startswith(
                    "["
                ) and self.clone_dump_chats.endswith("]"):
                    self.clone_dump_chats = ast.literal_eval(self.clone_dump_chats)
                else:
                    self.clone_dump_chats = [self.clone_dump_chats]
            temp_dict = {}
            for ch in self.clone_dump_chats:
                if isinstance(ch, str) and "|" in ch:
                    ci, ti = map(
                        lambda x: int(x) if x.lstrip("-").isdigit() else x,
                        ch.split("|", 1),
                    )
                    temp_dict[ci] = {"thread_id": ti, "last_sent_msg": None}
                elif isinstance(ch, str):
                    if ch.lower() == "pm":
                        ci = self.user_id
                    else:
                        ci = int(ch) if ch.lstrip("-").isdigit() else ch
                    temp_dict[ci] = {"thread_id": None, "last_sent_msg": None}
                else:
                    temp_dict[ch] = {"thread_id": None, "last_sent_msg": None}
            self.clone_dump_chats = temp_dict
        if self.thumb != "none" and is_telegram_link(self.thumb):
            msg = (await get_tg_link_message(self.thumb, self.user_id))[0]
            self.thumb = (
                await create_thumb(msg) if msg.photo or msg.document else ""
            )
