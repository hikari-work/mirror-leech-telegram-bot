from aiofiles.os import path as aiopath, makedirs
from aiofiles import open as aiopen
from asyncio import create_subprocess_shell
from os.path import dirname

from .. import (
    aria2_options,
    qbit_options,
    user_data,
    excluded_extensions,
    included_extensions,
    LOGGER,
    rss_dict,
    auth_chats,
    sudo_users,
)
from ..helper.storage.db_handler import database
from ..helper.util.shutil_helper import rmtree
from ..helper.util.user_sessions import load_user_sessions
from .config_manager import Config
from .telegram_manager import TgClient
from .torrent_manager import TorrentManager


async def update_qb_options():
    LOGGER.info("Get qBittorrent options from server")
    if not qbit_options:
        opt = await TorrentManager.qbittorrent.app.preferences()
        qbit_options.update(opt)
        del qbit_options["listen_port"]
        for k in list(qbit_options.keys()):
            if k.startswith("rss"):
                del qbit_options[k]
        qbit_options["web_ui_password"] = "mltbmltb"
        await TorrentManager.qbittorrent.app.set_preferences(
            {"web_ui_password": "mltbmltb"}
        )
    else:
        await TorrentManager.qbittorrent.app.set_preferences(qbit_options)


async def update_aria2_options():
    LOGGER.info("Get aria2 options from server")
    if not aria2_options:
        op = await TorrentManager.aria2.getGlobalOption()
        aria2_options.update(op)
    else:
        await TorrentManager.aria2.changeGlobalOption(aria2_options)


# Settings left behind by the JDownloader, Usenet, gallery-dl and cloud-upload
# code that this bot no longer carries. Config.load_dict already ignores keys it
# does not know, so this only keeps the stored documents from growing stale.
_LEGACY_CONFIG_KEYS = (
    "JD_EMAIL",
    "JD_PASS",
    "USENET_SERVERS",
    "GALLERY_DL_OPTIONS",
    "GDRIVE_ID",
    "INDEX_URL",
    "IS_TEAM_DRIVE",
    "STOP_DUPLICATE",
    "USE_SERVICE_ACCOUNTS",
    "DEFAULT_UPLOAD",
    "UPLOAD_PATHS",
    "RCLONE_PATH",
    "RCLONE_FLAGS",
    "RCLONE_SERVE_URL",
    "RCLONE_SERVE_PORT",
    "RCLONE_SERVE_USER",
    "RCLONE_SERVE_PASS",
    "BUZZHEAVIER_ACCOUNT_ID",
    "BUZZHEAVIER_FOLDER_ID",
    "GOFILE_API_KEY",
    "HYDRA_IP",
    "HYDRA_API_KEY",
)

_LEGACY_USER_KEYS = (
    "RCLONE_CONFIG",
    "RCLONE_PATH",
    "RCLONE_FLAGS",
    "TOKEN_PICKLE",
    "GDRIVE_ID",
    "INDEX_URL",
    "STOP_DUPLICATE",
    "USER_TOKENS",
    "DEFAULT_UPLOAD",
    "UPLOAD_PATHS",
    "GALLERY_DL_OPTIONS",
    "BUZZHEAVIER_ACCOUNT_ID",
    "BUZZHEAVIER_FOLDER_ID",
)

_LEGACY_BLOBS = (
    "sabnzbd/SABnzbd.ini",
    "cfg.zip",
    "accounts.zip",
    "rclone.conf",
    "token.pickle",
    "list_drives.txt",
)


async def migrate_legacy_keys(bot_id):
    """Drop settings and files owned by the removed subsystems.

    $unset on an absent field and a delete of an absent blob are both no-ops, so
    a restart repeats no work and logs nothing.
    """
    if not database.is_connected:
        return

    # The Mongo `$unset` has no SQL counterpart cleaner than "read, pop the
    # legacy keys, write back only when something changed". Both are no-ops on
    # a doc that holds none of them, so a restart repeats no work and logs
    # nothing -- the property `$unset` gave for free.
    removed = 0
    for reader, writer in (
        (database.read_config, database.replace_config),
        (database.read_deploy, database.replace_deploy),
    ):
        doc = await reader(bot_id)
        if doc is None:
            continue
        dropped = [key for key in _LEGACY_CONFIG_KEYS if key in doc]
        if not dropped:
            continue
        for key in dropped:
            doc.pop(key)
        await writer(doc, bot_id)
        removed += 1

    user_count = 0
    for user_id, data in await database.read_user_rows():
        dropped = [key for key in _LEGACY_USER_KEYS if key in data]
        if not dropped:
            continue
        for key in dropped:
            data.pop(key)
        await database.save_user_row(user_id, data)
        user_count += 1

    # Matched by name rather than by fetching each one, so a rotated
    # DB_ENCRYPTION_KEY cannot leave an undecryptable blob behind.
    stale = []
    for name in await database.list_blobs(bot_id=bot_id):
        if name in _LEGACY_BLOBS or name.rsplit("/", 1)[-1] in (
            "RCLONE_CONFIG",
            "TOKEN_PICKLE",
        ):
            stale.append(name)
    for name in stale:
        await database.delete_blob(name, bot_id=bot_id)

    if removed or user_count or stale:
        LOGGER.info(
            f"Removed legacy data from Database: {removed} config document(s), "
            f"{user_count} user record(s), {len(stale)} stored file(s)"
        )


async def load_settings():
    if not Config.DATABASE_URL:
        await load_user_sessions()
        return

    if await aiopath.exists("thumbnails"):
        await rmtree("thumbnails", ignore_errors=True)

    await database.connect()
    if not database.is_connected:
        await load_user_sessions()
        return

    BOT_ID = Config.BOT_TOKEN.split(":", 1)[0]

    # Before anything reads the stored config or writes blobs back to disk, so
    # a dead file never lands on the filesystem again.
    await migrate_legacy_keys(BOT_ID)

    config_dict = await database.read_config(BOT_ID)
    if config_dict:
        Config.load_dict(config_dict)

    all_defaults = Config.get_all()
    missing = {
        k: v for k, v in all_defaults.items() if k not in (config_dict or {})
    }
    if missing:
        LOGGER.info(
            f"Adding {len(missing)} new config key(s) to Database: {', '.join(missing)}"
        )
        await database.update_config(missing, BOT_ID)

    await restore_private_files(BOT_ID)

    if a2c_options := await database.read_aria2(BOT_ID):
        aria2_options.update(a2c_options)

    if qbit_opt := await database.read_qbit(BOT_ID):
        qbit_options.update(qbit_opt)

    await restore_users(BOT_ID)

    if rss_rows := await database.read_rss_rows(BOT_ID):
        for user_id, feeds in rss_rows:
            rss_dict[user_id] = feeds
        LOGGER.info("Rss data has been imported from Database.")


async def restore_private_files(bot_id):
    """Write every stored private file back to disk.

    Blobs are keyed by their real relative path, so a name doubles as the
    destination and nested paths need no mapping.
    """
    for name in await database.list_blobs(bot_id=bot_id):
        if name.startswith("users/"):
            continue
        if not (blob := await database.get_blob(name, bot_id=bot_id)):
            continue
        if folder := dirname(name):
            await makedirs(folder, exist_ok=True)
        async with aiopen(name, "wb+") as f:
            await f.write(blob)


async def restore_users(bot_id):
    """Rebuild user_data from the scalar records plus their stored files.

    A user may own files without holding any scalar setting, since uploading a
    thumbnail only calls update_user_doc. Those ids exist solely as blob names,
    so both sources are merged here.
    """
    rows = {user_id: data for user_id, data in await database.read_user_rows()}
    for name in await database.list_blobs("users/", bot_id=bot_id):
        # users/<uid>/<KEY>
        parts = name.split("/")
        if len(parts) == 3 and parts[1].lstrip("-").isdigit():
            rows.setdefault(int(parts[1]), {})
    if not rows:
        return

    # Copy presets are their own rows now (global per user), so they are read
    # separately and overlaid on the jsonb doc -- rows are authoritative for a
    # user who has them, while a pre-normalisation doc key still serves the
    # users who do not until the one-shot move runs.
    presets_by_user = await database.read_copy_presets_all()

    if not await aiopath.exists("thumbnails"):
        await makedirs("thumbnails")
    for uid, row in rows.items():
        path_ = f"thumbnails/{uid}.jpg"
        if blob := await database.get_blob(f"users/{uid}/THUMBNAIL", bot_id=bot_id):
            async with aiopen(path_, "wb+") as f:
                await f.write(blob)
            row["THUMBNAIL"] = path_
        if uid in presets_by_user:
            row["COPY_PRESETS"] = presets_by_user[uid]
        user_data[uid] = row
    LOGGER.info("Users data has been imported from Database")


async def save_settings():
    if not database.is_connected:
        return
    config_dict = await database.read_config()
    all_current = Config.get_all()
    missing = {
        k: v for k, v in all_current.items() if k not in (config_dict or {})
    }
    if missing:
        await database.update_config(missing)
    if await database.read_aria2() is None:
        await database.save_aria2_settings()
    if await database.read_qbit() is None:
        await database.save_qbit_settings()


async def update_variables():
    if (
        Config.LEECH_SPLIT_SIZE > TgClient.MAX_SPLIT_SIZE
        or Config.LEECH_SPLIT_SIZE == 2097152000
        or not Config.LEECH_SPLIT_SIZE
    ):
        Config.LEECH_SPLIT_SIZE = TgClient.MAX_SPLIT_SIZE

    Config.HYBRID_LEECH = bool(Config.HYBRID_LEECH and TgClient.IS_PREMIUM_USER)

    if Config.AUTHORIZED_CHATS:
        aid = Config.AUTHORIZED_CHATS.split()
        for id_ in aid:
            chat_id, *thread_ids = id_.split("|")
            chat_id = int(chat_id.strip())
            if thread_ids:
                thread_ids = list(map(lambda x: int(x.strip()), thread_ids))
                auth_chats[chat_id] = thread_ids
            else:
                auth_chats[chat_id] = []

    if Config.SUDO_USERS:
        aid = Config.SUDO_USERS.split()
        for id_ in aid:
            sudo_users.append(int(id_.strip()))

    if Config.EXCLUDED_EXTENSIONS:
        fx = Config.EXCLUDED_EXTENSIONS.split()
        for x in fx:
            x = x.lstrip(".")
            excluded_extensions.append(x.strip().lower())

    if Config.INCLUDED_EXTENSIONS:
        fx = Config.INCLUDED_EXTENSIONS.split()
        for x in fx:
            x = x.lstrip(".")
            included_extensions.append(x.strip().lower())


async def load_configurations():

    if not await aiopath.exists(".netrc"):
        async with aiopen(".netrc", "w"):
            pass

    await (
        await create_subprocess_shell(
            "chmod 600 .netrc && cp .netrc /root/.netrc && chmod +x aria-nox.sh && ./aria-nox.sh"
        )
    ).wait()

    if Config.BASE_URL:
        await create_subprocess_shell(
            f"gunicorn -k uvicorn.workers.UvicornWorker -w 1 web.wserver:app --bind 0.0.0.0:{Config.BASE_URL_PORT}"
        )
