from pyrogram.filters import create

from ... import user_data, auth_chats, sudo_users
from ...core.config_manager import Config


class CustomFilters:
    async def owner_filter(self, _, update):
        user = update.from_user or update.sender_chat
        return user.id == Config.OWNER_ID

    owner = create(owner_filter)

    async def authorized_user(self, _, update):
        user = update.from_user or update.sender_chat
        uid = user.id
        chat_id = update.chat.id
        thread_id = update.message_thread_id if update.topic_message else None
        return bool(
            uid == Config.OWNER_ID
            or (
                uid in user_data
                and (
                    user_data[uid].get("AUTH", False)
                    or user_data[uid].get("SUDO", False)
                )
            )
            or (
                chat_id in user_data
                and user_data[chat_id].get("AUTH", False)
                and (
                    thread_id is None
                    or thread_id in user_data[chat_id].get("thread_ids", [])
                )
            )
            or uid in sudo_users
            or uid in auth_chats
            or chat_id in auth_chats
            and (
                auth_chats[chat_id]
                and thread_id
                and thread_id in auth_chats[chat_id]
                or not auth_chats[chat_id]
            )
        )

    authorized = create(authorized_user)

    async def sudo_user(self, _, update):
        user = update.from_user or update.sender_chat
        uid = user.id
        return bool(
            uid == Config.OWNER_ID
            or uid in user_data
            and user_data[uid].get("SUDO")
            or uid in sudo_users
        )

    sudo = create(sudo_user)

    @staticmethod
    async def is_sudo(update) -> bool:
        """Whether *update* came from a sudo user or the owner.

        The filters above are pyrogram Filter objects, so calling one takes the
        client as its first argument -- and every one of them ignores it, which
        is why that parameter is named ``_``. Eight places ask this question
        outside a handler, with no client in reach, and each of them used to
        pass an empty string for it.
        """
        # pyrefly: ignore[bad-argument-type]
        return await CustomFilters.sudo("", update)
