from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from gridfs.asynchronous import AsyncGridFSBucket
from importlib import import_module
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi
from pymongo.errors import PyMongoError

from ... import LOGGER, user_data, rss_dict, qbit_options
from ...core.telegram_manager import TgClient
from ...core.config_manager import Config
from .blob_crypto import KEY_VAR, blob_box

# Keys whose value is a file on disk rather than a scalar. They live in GridFS
# under users/<uid>/<key>, so the users collection only ever holds scalars.
USER_DOC_KEYS = ("THUMBNAIL", "RCLONE_CONFIG", "TOKEN_PICKLE")


class DbManager:
    def __init__(self):
        self._return = True
        self._conn = None
        self.db = None
        self._bucket = None

    async def connect(self):
        try:
            if self._conn is not None:
                await self._conn.close()
            self._conn = AsyncMongoClient(
                Config.DATABASE_URL,
                server_api=ServerApi("1"),
                connectTimeoutMS=60000,
                serverSelectionTimeoutMS=60000,
            )
            self.db = self._conn[Config.DATABASE_NAME]
            self._bucket = AsyncGridFSBucket(self.db, bucket_name="files")
            self._return = False
        except PyMongoError as e:
            LOGGER.error(f"Error in DB connection: {e}")
            self.db = None
            self._bucket = None
            self._return = True
            self._conn = None

    async def disconnect(self):
        self._return = True
        if self._conn is not None:
            await self._conn.close()
        self._conn = None
        self._bucket = None

    # ---------------------------------------------------------------- GridFS

    @staticmethod
    def _blob_name(path, bot_id=None):
        """Namespace a blob by bot id so one database can host several bots."""
        return f"{bot_id or TgClient.ID}/{path}"

    async def save_blob(self, path, data: bytes, bot_id=None):
        """Store data under path, replacing whatever revision was there.

        The upload happens before the prune so that a crash in between leaves
        the new revision in place rather than nothing at all.
        """
        if self._return:
            return
        name = self._blob_name(path, bot_id)
        await self._bucket.upload_from_stream(name, blob_box.encrypt(data))
        await self._prune_blob(name)

    async def _prune_blob(self, name, keep=1):
        cursor = self._bucket.find({"filename": name}).sort("uploadDate", -1)
        for idx, doc in enumerate(await cursor.to_list(None)):
            if idx >= keep:
                await self._bucket.delete(doc._id)

    async def get_blob(self, path, bot_id=None) -> bytes | None:
        if self._return:
            return None
        name = self._blob_name(path, bot_id)
        try:
            stream = await self._bucket.open_download_stream_by_name(name)
        except Exception:
            return None
        try:
            return blob_box.decrypt(await stream.read()) or None
        finally:
            await stream.close()

    async def list_blobs(self, prefix="", bot_id=None) -> list[str]:
        """Return stored paths under prefix, with the bot namespace stripped."""
        if self._return:
            return []
        root = self._blob_name(prefix, bot_id)
        names = set()
        # Range scan rather than a regex so that dots and slashes in the path
        # need no escaping. ￿ sorts above any character a name may hold.
        cursor = self._bucket.find(
            {"filename": {"$gte": root, "$lt": root + "￿"}}
        )
        for doc in await cursor.to_list(None):
            names.add(doc.filename.split("/", 1)[1])
        return sorted(names)

    async def delete_blob(self, path, bot_id=None):
        if self._return:
            return
        await self._prune_blob(self._blob_name(path, bot_id), keep=0)

    async def update_deploy_config(self):
        if self._return:
            return
        try:
            settings = import_module("config")
            config_file = {
                key: value.strip() if isinstance(value, str) else value
                for key, value in vars(settings).items()
                if not key.startswith("__") and key != KEY_VAR
            }
        except ModuleNotFoundError:
            return
        await self.db.settings.deployConfig.replace_one(
            {"_id": TgClient.ID}, config_file, upsert=True
        )

    async def update_config(self, dict_):
        if self._return:
            return
        await self.db.settings.config.update_one(
            {"_id": TgClient.ID}, {"$set": dict_}, upsert=True
        )

    async def update_aria2(self, key, value):
        if self._return:
            return
        await self.db.settings.aria2c.update_one(
            {"_id": TgClient.ID}, {"$set": {key: value}}, upsert=True
        )

    async def update_qbittorrent(self, key, value):
        if self._return:
            return
        await self.db.settings.qbittorrent.update_one(
            {"_id": TgClient.ID}, {"$set": {key: value}}, upsert=True
        )

    async def save_qbit_settings(self):
        if self._return:
            return
        await self.db.settings.qbittorrent.update_one(
            {"_id": TgClient.ID}, {"$set": qbit_options}, upsert=True
        )

    async def update_private_file(self, path):
        if self._return:
            return
        if await aiopath.exists(path):
            async with aiopen(path, "rb") as pf:
                pf_bin = await pf.read()
            await self.save_blob(path, pf_bin)
            if path == "config.py":
                await self.update_deploy_config()
        else:
            await self.delete_blob(path)

    async def update_nzb_config(self):
        if self._return:
            return
        async with aiopen("sabnzbd/SABnzbd.ini", "rb") as pf:
            nzb_conf = await pf.read()
        await self.save_blob("sabnzbd/SABnzbd.ini", nzb_conf)

    async def update_user_data(self, user_id):
        if self._return:
            return
        data = user_data.get(user_id, {}).copy()
        # Files are kept in GridFS, so the record is plain scalars and can be
        # replaced wholesale.
        for key in USER_DOC_KEYS:
            data.pop(key, None)
        await self.db.users.replace_one({"_id": user_id}, data, upsert=True)

    async def update_user_doc(self, user_id, key, path=""):
        if self._return:
            return
        if path:
            async with aiopen(path, "rb") as doc:
                doc_bin = await doc.read()
            await self.save_blob(f"users/{user_id}/{key}", doc_bin)
        else:
            await self.delete_blob(f"users/{user_id}/{key}")

    async def rss_update_all(self):
        if self._return:
            return
        for user_id in list(rss_dict.keys()):
            await self.db.rss[TgClient.ID].replace_one(
                {"_id": user_id}, rss_dict[user_id], upsert=True
            )

    async def rss_update(self, user_id):
        if self._return:
            return
        await self.db.rss[TgClient.ID].replace_one(
            {"_id": user_id}, rss_dict[user_id], upsert=True
        )

    async def rss_delete(self, user_id):
        if self._return:
            return
        await self.db.rss[TgClient.ID].delete_one({"_id": user_id})

    async def add_incomplete_task(self, cid, link, tag):
        if self._return:
            return
        await self.db.tasks[TgClient.ID].insert_one(
            {"_id": link, "cid": cid, "tag": tag}
        )

    async def rm_complete_task(self, link):
        if self._return:
            return
        await self.db.tasks[TgClient.ID].delete_one({"_id": link})

    async def get_incomplete_tasks(self):
        notifier_dict = {}
        if self._return:
            return notifier_dict
        if await self.db.tasks[TgClient.ID].find_one():
            rows = self.db.tasks[TgClient.ID].find({})
            async for row in rows:
                if row["cid"] in list(notifier_dict.keys()):
                    if row["tag"] in list(notifier_dict[row["cid"]]):
                        notifier_dict[row["cid"]][row["tag"]].append(row["_id"])
                    else:
                        notifier_dict[row["cid"]][row["tag"]] = [row["_id"]]
                else:
                    notifier_dict[row["cid"]] = {row["tag"]: [row["_id"]]}
        await self.db.tasks[TgClient.ID].drop()
        return notifier_dict

    async def trunc_table(self, name):
        if self._return:
            return
        await self.db[name][TgClient.ID].drop()


database = DbManager()
