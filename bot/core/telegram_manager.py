from pyrogram import Client, enums
from pyrogram.types import LinkPreviewOptions, User
from asyncio import Lock

from .. import LOGGER, user_data, user_clients
from .config_manager import Config


def own_account(client: Client) -> User:
    """The account ``client`` is signed in as.

    ``Client.me`` is filled by ``start()`` and stays set for the life of the
    client, so every caller in the bot has one. Stating that here keeps the
    single inaccuracy in one place instead of a None check at each call site.
    """
    return client.me  # pyrefly: ignore[bad-return]


def user_session() -> Client:
    """The USER_SESSION_STRING client, for a path that only runs when it exists.

    ``TgClient.user`` is honestly optional, but the uploader and the telegram
    downloader only reach for it once ``user_transmission`` is set, and the
    settings resolver clears that flag when there is no user session -- so the
    None cannot be observed from there. Code where it really can (a link a user
    session may or may not be able to read) uses ``TgClient.user`` directly and
    checks it.
    """
    return TgClient.user  # pyrefly: ignore[bad-return]


class TgClient:
    _lock = Lock()
    # Both clients are built during startup, before any handler can run.
    #
    # ``bot`` is annotated non-optional even though it really is None between
    # import and ``start_bot()``: ``stop()`` leans on that to be callable on a
    # bot that never started. The alternative -- ``Client | None`` -- would make
    # every one of the ~90 reads after startup ask about a state none of them can
    # observe, so the inaccuracy is kept to the one assignment below instead.
    bot: Client = None  # pyrefly: ignore[bad-assignment]
    # ``user`` is a different case and is typed honestly: ``start_user()`` skips
    # it entirely without a USER_SESSION_STRING and sets it back to None if the
    # session fails to start, so reads really can find nothing here.
    user: Client | None = None
    NAME = ""
    # The bot half of BOT_TOKEN, i.e. a str -- this was `= 0`, which made every
    # use of it look like an int. Nothing does arithmetic on it: it is the
    # pyrogram session name, a mongo `_id`, and a collection name (which
    # `AsyncCollection.__getitem__` requires be a str).
    ID: str = ""
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
        # A bot token always has a username; the `or ""` is for the annotation.
        cls.NAME = own_account(cls.bot).username or ""

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
                cls.IS_PREMIUM_USER = bool(own_account(cls.user).is_premium)
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
