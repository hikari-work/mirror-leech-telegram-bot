"""PostgreSQL persistence for settings, users, RSS, tasks, copy records, blobs.

The bot's data model is a set of whole documents the code loads into memory at
boot (``user_data``, ``rss_dict``, the ``Config`` defaults, the aria2/qbit
option dicts) and rewrites wholesale on change. That "read whole dict, mutate,
write whole row" pattern maps one document to one ``jsonb`` row, so the schema
here is a handful of tables with a ``data jsonb`` column rather than a
field-per-column normalisation.

One database may host several bots, exactly as the Mongo layout it replaces
allowed: the per-bot collections ``rss.<bot>``/``tasks.<bot>``/``copies.<bot>``
and the ``_id``-keyed ``settings.*`` documents become a ``bot_id`` column, and
blobs keep their ``{bot_id}/{path}`` name. The one thing that stays global is
the ``users`` table -- it has no ``bot_id`` column, mirroring the shared Mongo
``users`` collection.

Blob data is stored already-encrypted (``blob_box``), so the database never
sees a plaintext private file. A name holds exactly one revision -- the
effective behaviour of the old GridFS prune-to-one -- so a save is an upsert.

Every public method opens with ``if self._return: return`` (or an equivalent
default value), mirroring the guard the Mongo code used: ``_return`` is True
until ``connect()`` succeeds and True again after ``disconnect()``, and with no
database configured it never flips, so the whole class is a no-op and the bot
runs from memory plus ``user_sessions.json``.
"""

from __future__ import annotations

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from importlib import import_module
from time import time
from typing import Any, cast
from collections.abc import Sequence

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ... import LOGGER, aria2_options, qbit_options, rss_dict, user_data
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient
from .blob_crypto import KEY_VAR, blob_box
from .copy_records import MAX_TASK_RECORDS

# Keys whose value is a file on disk rather than a scalar. They live in the
# blobs table under users/<uid>/<key>, so the users row only ever holds scalars.
USER_DOC_KEYS = ("THUMBNAIL",)

# Table name used by ``trunc_table`` -> the table that actually holds it.
_TRUNC_TABLES = {"rss": "rss", "tasks": "incomplete_tasks"}


# Idempotent DDL: applied on every connect, so a fresh database comes up ready
# and a reconnect after a restart changes nothing.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS settings_config (
        bot_id text PRIMARY KEY,
        data   jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_deploy (
        bot_id text PRIMARY KEY,
        data   jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_aria2 (
        bot_id text PRIMARY KEY,
        data   jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings_qbit (
        bot_id text PRIMARY KEY,
        data   jsonb NOT NULL
    )
    """,
    # Shared across bots, like the Mongo users collection it replaces.
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id bigint PRIMARY KEY,
        data    jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rss (
        bot_id  text NOT NULL,
        user_id bigint NOT NULL,
        data    jsonb NOT NULL,
        PRIMARY KEY (bot_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incomplete_tasks (
        bot_id text   NOT NULL,
        link   text   NOT NULL,
        cid    bigint NOT NULL,
        tag    text   NOT NULL,
        PRIMARY KEY (bot_id, link)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_records (
        bot_id  text   NOT NULL,
        id      text   NOT NULL,
        cid     bigint NOT NULL,
        mid     bigint NOT NULL,
        user_id bigint NOT NULL,
        name    text   NOT NULL,
        at      bigint NOT NULL,
        units   jsonb  NOT NULL,
        PRIMARY KEY (bot_id, id)
    )
    """,
    # A name holds the newest revision only; save_blob upserts in place.
    """
    CREATE TABLE IF NOT EXISTS blobs (
        name       text      PRIMARY KEY,
        data       bytea     NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)


def _jsonb(value: Any) -> Jsonb:
    """Wrap a dict/list so psycopg can adapt it into a jsonb column."""
    return Jsonb(value)


class DbManager:
    def __init__(self):
        # ``_return`` guards the whole class: every method opens with
        # ``if self._return: ...`` and it is only False between a successful
        # ``connect()`` and the next ``disconnect()``/failure. The dereferences
        # below therefore never see the None ``_conn`` starts as. Note it is
        # *not* interchangeable with ``self._conn is None``: ``disconnect()``
        # sets ``_return`` True before ``_conn`` is cleared.
        self._return = True
        self._conn: AsyncConnection | None = None

    @property
    def is_connected(self) -> bool:
        return not self._return and self._conn is not None

    @staticmethod
    def _conninfo() -> tuple[str, str | None]:
        return Config.DATABASE_URL, Config.DATABASE_NAME or None

    async def connect(self):
        if not Config.DATABASE_URL:
            self._return = True
            return
        try:
            if self._conn is not None:
                await self._conn.close()
            url, dbname = self._conninfo()
            if dbname:
                self._conn = await AsyncConnection.connect(
                    url, dbname=dbname, autocommit=True
                )
            else:
                self._conn = await AsyncConnection.connect(url, autocommit=True)
            self._conn.row_factory = cast(Any, dict_row)
            for statement in _SCHEMA:
                await self._conn.execute(cast(Any, statement))
            self._return = False
        except Exception as e:
            LOGGER.error(f"Error in DB connection: {e}")
            await self._close_conn()
            self._return = True

    async def disconnect(self):
        self._return = True
        await self._close_conn()

    async def _close_conn(self):
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
        self._conn = None

    # ----------------------------------------------------- SQL executors
    # The three methods every query goes through, so a test can replace them
    # with a fake that records the SQL and answers with fixture rows -- the
    # hermetic seam of the whole module.

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        conn = self._conn
        if conn is None:
            # Only reachable through a bug: every public method guards on
            # ``_return``, which is False only while ``_conn`` is set.
            raise RuntimeError("DbManager is not connected")
        # The statement is always one of this module's own literal SQL strings,
        # so the cast is only for psycopg's LiteralString-typed ``execute``.
        await conn.execute(cast(Any, sql), params)

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        conn = self._conn
        if conn is None:
            raise RuntimeError("DbManager is not connected")
        cursor = await conn.execute(cast(Any, sql), params)
        # ``row_factory`` is ``dict_row``, so a row is a dict -- the cast keeps
        # psycopg's generic Row type from leaking into the module's API.
        return cast("dict | None", await cursor.fetchone())

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        conn = self._conn
        if conn is None:
            raise RuntimeError("DbManager is not connected")
        cursor = await conn.execute(cast(Any, sql), params)
        return cast("list[dict]", await cursor.fetchall())

    # ---------------------------------------------------------------- blobs

    @staticmethod
    def _blob_name(path, bot_id=None):
        """Namespace a blob by bot id so one database can host several bots."""
        return f"{bot_id or TgClient.ID}/{path}"

    async def save_blob(self, path, data: bytes, bot_id=None):
        if self._return:
            return
        name = self._blob_name(path, bot_id)
        await self._execute(
            """
            INSERT INTO blobs (name, data, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (name) DO UPDATE
                SET data = EXCLUDED.data, updated_at = now()
            """,
            (name, blob_box.encrypt(data)),
        )

    async def get_blob(self, path, bot_id=None) -> bytes | None:
        if self._return:
            return None
        name = self._blob_name(path, bot_id)
        row = await self._fetchone(
            "SELECT data FROM blobs WHERE name = %s", (name,)
        )
        if row is None:
            return None
        return blob_box.decrypt(row["data"]) or None

    async def list_blobs(self, prefix="", bot_id=None) -> list[str]:
        """Return stored paths under prefix, with the bot namespace stripped."""
        if self._return:
            return []
        root = self._blob_name(prefix, bot_id)
        # A prefix test with substr, not LIKE: no wildcard to escape, and dots
        # and slashes in a path need no care -- the same reason the Mongo code
        # used a range scan rather than a regex.
        rows = await self._fetchall(
            "SELECT name FROM blobs WHERE substr(name, 1, length(%s)) = %s",
            (root, root),
        )
        names = {row["name"][len(root) :] for row in rows}
        return sorted(names)

    async def delete_blob(self, path, bot_id=None):
        if self._return:
            return
        name = self._blob_name(path, bot_id)
        await self._execute("DELETE FROM blobs WHERE name = %s", (name,))

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

    # ------------------------------------------------------------ settings

    async def read_config(self, bot_id=None) -> dict[str, Any] | None:
        if self._return:
            return None
        row = await self._fetchone(
            "SELECT data FROM settings_config WHERE bot_id = %s",
            (bot_id or TgClient.ID,),
        )
        return row["data"] if row else None

    async def read_deploy(self, bot_id=None) -> dict[str, Any] | None:
        if self._return:
            return None
        row = await self._fetchone(
            "SELECT data FROM settings_deploy WHERE bot_id = %s",
            (bot_id or TgClient.ID,),
        )
        return row["data"] if row else None

    async def replace_config(self, data: dict[str, Any], bot_id=None):
        """Replace a bot's whole config document -- a `$set` of every key."""
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_config (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb(data)),
        )

    async def replace_deploy(self, data: dict[str, Any], bot_id=None):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_deploy (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb(data)),
        )

    async def update_deploy_config(self, bot_id=None):
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
        await self.replace_deploy(config_file, bot_id)

    async def update_config(self, dict_, bot_id=None):
        """Merge keys into the config document -- a Mongo `$set`, not a replace."""
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_config (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE
                SET data = settings_config.data || EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb(dict_)),
        )

    async def read_aria2(self, bot_id=None) -> dict[str, Any] | None:
        if self._return:
            return None
        row = await self._fetchone(
            "SELECT data FROM settings_aria2 WHERE bot_id = %s",
            (bot_id or TgClient.ID,),
        )
        return row["data"] if row else None

    async def read_qbit(self, bot_id=None) -> dict[str, Any] | None:
        if self._return:
            return None
        row = await self._fetchone(
            "SELECT data FROM settings_qbit WHERE bot_id = %s",
            (bot_id or TgClient.ID,),
        )
        return row["data"] if row else None

    async def update_aria2(self, key, value, bot_id=None):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_aria2 (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE
                SET data = settings_aria2.data || EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb({key: value})),
        )

    async def update_qbittorrent(self, key, value, bot_id=None):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_qbit (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE
                SET data = settings_qbit.data || EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb({key: value})),
        )

    async def save_qbit_settings(self, bot_id=None):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_qbit (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE
                SET data = settings_qbit.data || EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb(qbit_options)),
        )

    async def save_aria2_settings(self, bot_id=None):
        """Seed a bot's whole aria2 options when it has none stored yet."""
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO settings_aria2 (bot_id, data)
            VALUES (%s, %s)
            ON CONFLICT (bot_id) DO UPDATE
                SET data = settings_aria2.data || EXCLUDED.data
            """,
            (bot_id or TgClient.ID, _jsonb(aria2_options)),
        )

    # --------------------------------------------------------------- users
    # The users table is global (no bot_id), exactly like the Mongo collection.

    async def update_user_data(self, user_id):
        if self._return:
            return
        data = user_data.get(user_id, {}).copy()
        # Files live in the blobs table, so the record is plain scalars and can
        # be replaced wholesale.
        for key in USER_DOC_KEYS:
            data.pop(key, None)
        await self.save_user_row(user_id, data)

    async def save_user_row(self, user_id: int, data: dict[str, Any]):
        """Replace one user's whole settings document."""
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO users (user_id, data)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (user_id, _jsonb(data)),
        )

    async def read_user_rows(self) -> list[tuple[int, dict[str, Any]]]:
        """Every user document as ``(user_id, data)``, for the boot restore."""
        if self._return:
            return []
        rows = await self._fetchall(
            "SELECT user_id, data FROM users ORDER BY user_id"
        )
        return [(row["user_id"], row["data"]) for row in rows]

    async def update_user_doc(self, user_id, key, path=""):
        if self._return:
            return
        if path:
            async with aiopen(path, "rb") as doc:
                doc_bin = await doc.read()
            await self.save_blob(f"users/{user_id}/{key}", doc_bin)
        else:
            await self.delete_blob(f"users/{user_id}/{key}")

    # ---------------------------------------------------------------- rss

    async def read_rss_rows(self, bot_id=None) -> list[tuple[int, dict[str, Any]]]:
        """Every stored feed set of one bot as ``(user_id, feeds)``."""
        if self._return:
            return []
        rows = await self._fetchall(
            "SELECT user_id, data FROM rss WHERE bot_id = %s ORDER BY user_id",
            (bot_id or TgClient.ID,),
        )
        return [(row["user_id"], row["data"]) for row in rows]

    async def rss_update_all(self, bot_id=None):
        if self._return:
            return
        bot = bot_id or TgClient.ID
        for user_id in list(rss_dict.keys()):
            await self._execute(
                """
                INSERT INTO rss (bot_id, user_id, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (bot_id, user_id) DO UPDATE SET data = EXCLUDED.data
                """,
                (bot, user_id, _jsonb(rss_dict[user_id])),
            )

    async def rss_update(self, user_id, bot_id=None):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO rss (bot_id, user_id, data)
            VALUES (%s, %s, %s)
            ON CONFLICT (bot_id, user_id) DO UPDATE SET data = EXCLUDED.data
            """,
            (bot_id or TgClient.ID, user_id, _jsonb(rss_dict[user_id])),
        )

    async def rss_delete(self, user_id, bot_id=None):
        if self._return:
            return
        await self._execute(
            "DELETE FROM rss WHERE bot_id = %s AND user_id = %s",
            (bot_id or TgClient.ID, user_id),
        )

    # --------------------------------------------------------------- tasks

    async def add_incomplete_task(self, cid, link, tag):
        if self._return:
            return
        await self._execute(
            """
            INSERT INTO incomplete_tasks (bot_id, link, cid, tag)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (bot_id, link) DO NOTHING
            """,
            (TgClient.ID, link, cid, tag),
        )

    async def rm_complete_task(self, link):
        if self._return:
            return
        await self._execute(
            "DELETE FROM incomplete_tasks WHERE bot_id = %s AND link = %s",
            (TgClient.ID, link),
        )

    async def get_incomplete_tasks(self) -> dict[int, dict[str, list[str]]]:
        """Read a bot's unfinished tasks, then forget them -- as before, the
        notifier runs once per restart."""
        if self._return:
            return {}
        bot = TgClient.ID
        rows = await self._fetchall(
            "SELECT cid, tag, link FROM incomplete_tasks WHERE bot_id = %s",
            (bot,),
        )
        notifier_dict: dict[int, dict[str, list[str]]] = {}
        for row in rows:
            cid = row["cid"]
            tag = row["tag"]
            if cid in notifier_dict:
                if tag in notifier_dict[cid]:
                    notifier_dict[cid][tag].append(row["link"])
                else:
                    notifier_dict[cid][tag] = [row["link"]]
            else:
                notifier_dict[cid] = {tag: [row["link"]]}
        await self._execute(
            "DELETE FROM incomplete_tasks WHERE bot_id = %s", (bot,)
        )
        return notifier_dict

    async def trunc_table(self, name):
        if self._return:
            return
        table = _TRUNC_TABLES.get(name)
        if table is None:
            return
        await self._execute(f"DELETE FROM {table} WHERE bot_id = %s", (TgClient.ID,))

    # -------------------------------------------------------- copy records

    async def save_copy_record(
        self, cid: int, mid: int, user_id: int, name: str, units: list
    ) -> None:
        """Store what one task uploaded, replacing any record of the same id."""
        if self._return:
            return
        key = f"{cid}:{mid}"
        await self._execute(
            """
            INSERT INTO copy_records
                (bot_id, id, cid, mid, user_id, name, at, units)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (bot_id, id) DO UPDATE SET
                cid = EXCLUDED.cid,
                mid = EXCLUDED.mid,
                user_id = EXCLUDED.user_id,
                name = EXCLUDED.name,
                at = EXCLUDED.at,
                units = EXCLUDED.units
            """,
            (TgClient.ID, key, cid, mid, user_id, name, int(time()), _jsonb(units)),
        )
        await self._prune_copy_records(user_id)

    async def find_copy_records(self, mid: int) -> list[dict]:
        """Every saved record with this task id -- one chat's mid can repeat.

        Rows are shaped like the Mongo documents callers read: ``_id`` is
        ``"cid:mid"`` and the owner field is named ``user``.
        """
        if self._return:
            return []
        rows = await self._fetchall(
            """
            SELECT id, cid, mid, user_id, name, at, units
            FROM copy_records WHERE bot_id = %s AND mid = %s
            ORDER BY id
            """,
            (TgClient.ID, mid),
        )
        return [
            {
                "_id": row["id"],
                "cid": row["cid"],
                "mid": row["mid"],
                "user": row["user_id"],
                "name": row["name"],
                "at": row["at"],
                "units": row["units"],
            }
            for row in rows
        ]

    async def _prune_copy_records(self, user_id: int) -> None:
        """Drop a user's records past the newest MAX_TASK_RECORDS of them.

        No index is built on user/at: the repo has never made one, and with a
        couple hundred rows per user a table scan per save is cheaper than an
        index whose only reader is this prune. ``at`` can tie between two
        saves in the same second, so ``id`` breaks the order deterministically.
        """
        if self._return:
            return
        rows = await self._fetchall(
            """
            SELECT id FROM copy_records
            WHERE bot_id = %s AND user_id = %s
            ORDER BY at DESC, id
            OFFSET %s
            """,
            (TgClient.ID, user_id, MAX_TASK_RECORDS),
        )
        stale = [row["id"] for row in rows]
        if not stale:
            return
        await self._execute(
            """
            DELETE FROM copy_records
            WHERE bot_id = %s AND id = ANY(%s)
            """,
            (TgClient.ID, stale),
        )


database = DbManager()
