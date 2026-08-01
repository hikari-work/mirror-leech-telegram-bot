from pyrogram import Client, enums
from pyrogram.types import LinkPreviewOptions
from asyncio import Lock

from .. import LOGGER, user_data, user_clients
from .config_manager import Config


class TgClient:
    _lock = Lock()
    bot = None
    user = None
    NAME = ""
    ID = 0
    IS_PREMIUM_USER = False
    MAX_SPLIT_SIZE = 2097152000

    @classmethod
    async def start_bot(cls):
        LOGGER.info("Creating client from BOT_TOKEN")
        cls.ID = Config.BOT_TOKEN.split(":", 1)[0]
        cls.bot = Client(
            cls.ID,
            Config.TELEGRAM_API,
            Config.TELEGRAM_HASH,
            proxy=Config.TG_PROXY,
            bot_token=Config.BOT_TOKEN,
            workdir="/app",
            parse_mode=enums.ParseMode.HTML,
            max_concurrent_transmissions=10,
            max_message_cache_size=15000,
            max_topic_cache_size=15000,
            sleep_threshold=0,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await cls.bot.start()
        cls.NAME = cls.bot.me.username

    @classmethod
    async def start_user(cls):
        if Config.USER_SESSION_STRING:
            LOGGER.info("Creating client from USER_SESSION_STRING")
            try:
                cls.user = Client(
                    "user",
                    Config.TELEGRAM_API,
                    Config.TELEGRAM_HASH,
                    proxy=Config.TG_PROXY,
                    session_string=Config.USER_SESSION_STRING,
                    workdir="/app",
                    parse_mode=enums.ParseMode.HTML,
                    sleep_threshold=60,
                    max_concurrent_transmissions=10,
                    max_message_cache_size=15000,
                    max_topic_cache_size=15000,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                await cls.user.start()
                cls.IS_PREMIUM_USER = cls.user.me.is_premium
                if cls.IS_PREMIUM_USER:
                    cls.MAX_SPLIT_SIZE = 4194304000
            except Exception as e:
                LOGGER.error(f"Failed to start client from USER_SESSION_STRING. {e}")
                cls.IS_PREMIUM_USER = False
                cls.user = None

    @classmethod
    async def stop(cls):
        async with cls._lock:
            await stop_user_clients()
            if cls.bot:
                await cls.bot.stop()
            if cls.user:
                await cls.user.stop()
            LOGGER.info("Client(s) stopped")

    @classmethod
    async def reload(cls):
        async with cls._lock:
            await cls.bot.restart()
            if cls.user:
                await cls.user.restart()
            LOGGER.info("Client(s) restarted")


_user_client_locks = {}
_locks_guard = Lock()


async def _user_client_lock(user_id):
    async with _locks_guard:
        return _user_client_locks.setdefault(user_id, Lock())


async def get_user_client(user_id):
    """Return the personal client of user_id, starting it on demand.

    Returns None if the user never logged in or his session can't be started.
    """
    if not user_id:
        return None
    if client := user_clients.get(user_id):
        return client
    session_string = user_data.get(user_id, {}).get("USER_SESSION_STRING")
    if not session_string:
        return None
    async with await _user_client_lock(user_id):
        if client := user_clients.get(user_id):
            return client
        LOGGER.info(f"Creating client from USER_SESSION_STRING of {user_id}")
        client = Client(
            f"user_{user_id}",
            Config.TELEGRAM_API,
            Config.TELEGRAM_HASH,
            proxy=Config.TG_PROXY,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
            parse_mode=enums.ParseMode.HTML,
            sleep_threshold=60,
            max_concurrent_transmissions=5,
            max_message_cache_size=1000,
            max_topic_cache_size=1000,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        try:
            await client.start()
        except Exception as e:
            LOGGER.error(f"Failed to start client of {user_id}. {e}")
            try:
                await client.stop()
            except Exception:
                pass
            return None
        user_clients[user_id] = client
        return client


async def stop_user_client(user_id):
    if client := user_clients.pop(user_id, None):
        try:
            await client.stop()
        except Exception as e:
            LOGGER.error(f"Failed to stop client of {user_id}. {e}")


async def stop_user_clients():
    for user_id in list(user_clients.keys()):
        await stop_user_client(user_id)
