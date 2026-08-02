from aiofiles.os import path as aiopath, remove, makedirs
from aiofiles import open as aiopen
from aioshutil import rmtree
from asyncio import create_subprocess_exec, create_subprocess_shell, sleep
from importlib import import_module
from os.path import dirname

from .. import (
    aria2_options,
    qbit_options,
    nzb_options,
    drives_ids,
    drives_names,
    index_urls,
    user_data,
    excluded_extensions,
    included_extensions,
    LOGGER,
    rss_dict,
    sabnzbd_client,
    auth_chats,
    sudo_users,
)
from ..helper.ext_utils.blob_crypto import KEY_VAR
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.user_sessions import load_user_sessions
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


async def update_nzb_options():
    LOGGER.info("Get SABnzbd options from server")
    while True:
        try:
            no = (await sabnzbd_client.get_config())["config"]["misc"]
            nzb_options.update(no)
        except:
            await sleep(0.5)
            continue
        break


async def load_settings():
    if not Config.DATABASE_URL:
        await load_user_sessions()
        return

    for p in ["thumbnails", "tokens", "rclone"]:
        if await aiopath.exists(p):
            await rmtree(p, ignore_errors=True)

    await database.connect()
    if database.db is None:
        await load_user_sessions()
        return

    BOT_ID = Config.BOT_TOKEN.split(":", 1)[0]

    try:
        settings = import_module("config")
        config_file = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in vars(settings).items()
            if not key.startswith("__") and key != KEY_VAR
        }
    except ModuleNotFoundError:
        config_file = {}

    old_config = await database.db.settings.deployConfig.find_one(
        {"_id": BOT_ID}, {"_id": 0}
    )
    if old_config is None and config_file:
        await database.db.settings.deployConfig.replace_one(
            {"_id": BOT_ID}, config_file, upsert=True
        )
    elif old_config and config_file and old_config != config_file:
        LOGGER.info("Replacing existing deploy config in Database")
        await database.db.settings.deployConfig.replace_one(
            {"_id": BOT_ID}, config_file, upsert=True
        )
    else:
        config_dict = await database.db.settings.config.find_one(
            {"_id": BOT_ID}, {"_id": 0}
        )
        if config_dict:
            Config.load_dict(config_dict)

    await restore_private_files(BOT_ID)

    if a2c_options := await database.db.settings.aria2c.find_one(
        {"_id": BOT_ID}, {"_id": 0}
    ):
        aria2_options.update(a2c_options)

    if qbit_opt := await database.db.settings.qbittorrent.find_one(
        {"_id": BOT_ID}, {"_id": 0}
    ):
        qbit_options.update(qbit_opt)

    await restore_users(BOT_ID)

    if await database.db.rss[BOT_ID].find_one():
        rows = database.db.rss[BOT_ID].find({})
        async for row in rows:
            user_id = row["_id"]
            del row["_id"]
            rss_dict[user_id] = row
        LOGGER.info("Rss data has been imported from Database.")


async def restore_private_files(bot_id):
    """Write every stored private file back to disk.

    Blobs are keyed by their real relative path, so a name doubles as the
    destination and nested paths such as sabnzbd/SABnzbd.ini need no mapping.
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
        if name == "sabnzbd/SABnzbd.ini" and await aiopath.exists(
            "sabnzbd/SABnzbd.ini.bak"
        ):
            await remove("sabnzbd/SABnzbd.ini.bak")


async def restore_users(bot_id):
    """Rebuild user_data from the scalar records plus their stored files.

    A user may own files without holding any scalar setting, since uploading a
    thumbnail only calls update_user_doc. Those ids exist solely as blob names,
    so both sources are merged here.
    """
    rows = {}
    async for row in database.db.users.find({}):
        rows[row.pop("_id")] = row
    for name in await database.list_blobs("users/", bot_id=bot_id):
        # users/<uid>/<KEY>
        parts = name.split("/")
        if len(parts) == 3 and parts[1].lstrip("-").isdigit():
            rows.setdefault(int(parts[1]), {})
    if not rows:
        return

    for p in ["thumbnails", "tokens", "rclone"]:
        if not await aiopath.exists(p):
            await makedirs(p)
    for uid, row in rows.items():
        for key, path_ in (
            ("THUMBNAIL", f"thumbnails/{uid}.jpg"),
            ("RCLONE_CONFIG", f"rclone/{uid}.conf"),
            ("TOKEN_PICKLE", f"tokens/{uid}.pickle"),
        ):
            if blob := await database.get_blob(f"users/{uid}/{key}", bot_id=bot_id):
                async with aiopen(path_, "wb+") as f:
                    await f.write(blob)
                row[key] = path_
        user_data[uid] = row
    LOGGER.info("Users data has been imported from Database")


async def save_settings():
    if database.db is None:
        return
    config_dict = Config.get_all()
    await database.db.settings.config.replace_one(
        {"_id": TgClient.ID}, config_dict, upsert=True
    )
    if await database.db.settings.aria2c.find_one({"_id": TgClient.ID}) is None:
        await database.db.settings.aria2c.update_one(
            {"_id": TgClient.ID}, {"$set": aria2_options}, upsert=True
        )
    if await database.db.settings.qbittorrent.find_one({"_id": TgClient.ID}) is None:
        await database.save_qbit_settings()
    if await database.get_blob("sabnzbd/SABnzbd.ini") is None:
        await database.update_nzb_config()


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

    if Config.GDRIVE_ID:
        drives_names.append("Main")
        drives_ids.append(Config.GDRIVE_ID)
        index_urls.append(Config.INDEX_URL)

    if await aiopath.exists("list_drives.txt"):
        async with aiopen("list_drives.txt", "r+") as f:
            lines = await f.readlines()
            for line in lines:
                temp = line.split()
                drives_ids.append(temp[1])
                drives_names.append(temp[0].replace("_", " "))
                if len(temp) > 2:
                    index_urls.append(temp[2])
                else:
                    index_urls.append("")


async def load_configurations():

    if not await aiopath.exists(".netrc"):
        async with aiopen(".netrc", "w"):
            pass

    await (
        await create_subprocess_shell(
            "chmod 600 .netrc && cp .netrc /root/.netrc && chmod +x aria-nox-nzb.sh && ./aria-nox-nzb.sh"
        )
    ).wait()

    if Config.BASE_URL:
        await create_subprocess_shell(
            f"gunicorn -k uvicorn.workers.UvicornWorker -w 1 web.wserver:app --bind 0.0.0.0:{Config.BASE_URL_PORT}"
        )

    if await aiopath.exists("cfg.zip"):
        if await aiopath.exists("/JDownloader/cfg"):
            await rmtree("/JDownloader/cfg", ignore_errors=True)
        await (
            await create_subprocess_exec("7z", "x", "cfg.zip", "-o/JDownloader")
        ).wait()

    if await aiopath.exists("accounts.zip"):
        if await aiopath.exists("accounts"):
            await rmtree("accounts")
        await (
            await create_subprocess_exec(
                "7z", "x", "-o.", "-aoa", "accounts.zip", "accounts/*.json"
            )
        ).wait()
        await (await create_subprocess_exec("chmod", "-R", "777", "accounts")).wait()
        await remove("accounts.zip")

    if not await aiopath.exists("accounts"):
        Config.USE_SERVICE_ACCOUNTS = False
