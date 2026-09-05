"""PostgreSQL persistence for settings, users, RSS, tasks, copy records, blobs.

The bot's data model is a set of whole documents the code loads into memory at
boot (``user_data``, ``rss_dict``, the ``Config`` defaults, the aria2/qbit
option dicts) and rewrites wholesale on change. That "read whole dict, mutate,
write whole row" pattern maps one document to one ``jsonb`` row, so the schema
here is a handful of tables with a ``data jsonb`` column rather than a
field-per-column normalisation.

Two exceptions are rows, because they are the fixed-shape nested lists the bot
keeps: the ``units`` of a finished copy task (``copy_tasks`` plus one
``copy_units`` row per unit and one ``copy_unit_media`` row per file in it) and
the per-user copy presets (``copy_presets`` plus one ``copy_preset_dests`` row
per destination). Everything else -- maps of scalars, or documents whose keys
are decided at runtime -- stays a single ``jsonb`` column.

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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib import import_module
from time import time
from typing import Any, cast

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
    # Copy presets are global per user too: they used to live in the users
    # document, and adding a bot_id would change multi-bot behaviour. A preset
    # is one row here and its destinations child rows; a preset delete cascades
    # them away. Destinations stay the tokens the user typed (``pm``,
    # ``@username``, a chat id, ``chat|thread``) -- resolving one needs the
    # reader's own id, so they are never split into chat/thread columns.
    """
    CREATE TABLE IF NOT EXISTS copy_presets (
        user_id bigint NOT NULL,
        name    text   NOT NULL,
        PRIMARY KEY (user_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_preset_dests (
        user_id bigint NOT NULL,
        name    text   NOT NULL,
        dst_seq integer NOT NULL,
        dest    text   NOT NULL,
        PRIMARY KEY (user_id, name, dst_seq),
        FOREIGN KEY (user_id, name) REFERENCES copy_presets
            ON DELETE CASCADE
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
    # One finished task is one row here; its units and their media are child
    # rows (below) that a task delete cascades away. ``cid``/``mid`` are the
    # coordinates of the task's result message, unique per chat.
    """
    CREATE TABLE IF NOT EXISTS copy_tasks (
        bot_id  text   NOT NULL,
        cid     bigint NOT NULL,
        mid     bigint NOT NULL,
        user_id bigint NOT NULL,
        name    text   NOT NULL,
        at      bigint NOT NULL,
        PRIMARY KEY (bot_id, cid, mid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_units (
        bot_id   text   NOT NULL,
        cid      bigint NOT NULL,
        mid      bigint NOT NULL,
        seq      integer NOT NULL,
        mode     text   NOT NULL,
        src_chat bigint,
        src_msg  bigint,
        PRIMARY KEY (bot_id, cid, mid, seq),
        FOREIGN KEY (bot_id, cid, mid) REFERENCES copy_tasks
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_unit_media (
        bot_id  text   NOT NULL,
        cid     bigint NOT NULL,
        mid     bigint NOT NULL,
        seq     integer NOT NULL,
        idx     integer NOT NULL,
        kind    text,
        file_id text,
        caption text,
        PRIMARY KEY (bot_id, cid, mid, seq, idx),
        FOREIGN KEY (bot_id, cid, mid, seq) REFERENCES copy_units
            ON DELETE CASCADE
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

    @asynccontextmanager
    async def _txn(self) -> AsyncIterator[None]:
        """Yield inside one transaction -- or straight through when unconnected.

        The connection autocommits each statement, so the several writes of one
        save (a parent upsert plus child deletes and inserts) are wrapped in an
        explicit transaction to land atomically. The hermetic tests replace the
        executors and leave ``_conn`` None; for them there is no connection to
        wrap, so this yields directly and every statement is just recorded.
        """
        conn = self._conn
        if conn is None:
            yield
            return
        async with conn.transaction():
            yield

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
        """Write one user's whole in-memory settings back, presets included.

        Copy presets no longer live in the users jsonb document: the key is
        pulled out and stored as its own rows in the same transaction, so the
        two never disagree. When the key is absent from memory nothing is
        written for presets -- a user without them keeps a single users write.
        """
        if self._return:
            return
        data = user_data.get(user_id, {}).copy()
        # Files live in the blobs table, so the record is plain scalars and can
        # be replaced wholesale.
        for key in USER_DOC_KEYS:
            data.pop(key, None)
        # "Remove" leaves a "" behind in the mapping; only a dict is a real set.
        presets = data.pop("COPY_PRESETS", None)
        async with self._txn():
            await self.save_user_row(user_id, data)
            if presets is not None:
                await self._replace_copy_presets(
                    user_id, presets if isinstance(presets, dict) else {}
                )

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

    async def _replace_copy_presets(
        self, user_id: int, presets: dict[str, list[str]]
    ) -> None:
        """Store one user's whole preset set, replacing whatever was there.

        Called from ``update_user_data`` inside its transaction. A preset with
        no destinations keeps its parent row alone; the cascade clears the
        destination rows of a preset (or user) that is being deleted.
        """
        await self._execute(
            "DELETE FROM copy_presets WHERE user_id = %s", (user_id,)
        )
        for name, dests in presets.items():
            await self._execute(
                "INSERT INTO copy_presets (user_id, name) VALUES (%s, %s)",
                (user_id, name),
            )
            for seq, dest in enumerate(dests or []):
                await self._execute(
                    """
                    INSERT INTO copy_preset_dests (user_id, name, dst_seq, dest)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_id, name, seq, dest),
                )

    async def read_copy_presets_all(self) -> dict[int, dict[str, list[str]]]:
        """Every user's presets keyed by user id, for the boot restore.

        A preset whose destination rows are gone reads back as an empty list,
        so the mapping is exact rather than best-effort.
        """
        if self._return:
            return {}
        rows = await self._fetchall(
            """
            SELECT p.user_id, p.name, d.dst_seq, d.dest
            FROM copy_presets p
            LEFT JOIN copy_preset_dests d
              ON d.user_id = p.user_id AND d.name = p.name
            ORDER BY p.user_id, p.name, d.dst_seq
            """
        )
        presets: dict[int, dict[str, list[str]]] = {}
        for row in rows:
            presets.setdefault(row["user_id"], {}).setdefault(row["name"], [])
            if row["dest"] is not None:
                presets[row["user_id"]][row["name"]].append(row["dest"])
        return presets

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
        async with self._txn():
            await self._execute(
                """
                INSERT INTO copy_tasks (bot_id, cid, mid, user_id, name, at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (bot_id, cid, mid) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    name = EXCLUDED.name,
                    at = EXCLUDED.at
                """,
                (TgClient.ID, cid, mid, user_id, name, int(time())),
            )
            # Children are deleted and reinserted so a re-save of the same task
            # can never leave a unit the new record no longer has. Deleting the
            # units cascades to their media rows.
            await self._execute(
                """
                DELETE FROM copy_units
                WHERE bot_id = %s AND cid = %s AND mid = %s
                """,
                (TgClient.ID, cid, mid),
            )
            for seq, unit in enumerate(units):
                # chat/msg are absent on a sparse unit, so both stay nullable.
                await self._execute(
                    """
                    INSERT INTO copy_units
                        (bot_id, cid, mid, seq, mode, src_chat, src_msg)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        TgClient.ID,
                        cid,
                        mid,
                        seq,
                        unit["mode"],
                        unit.get("chat"),
                        unit.get("msg"),
                    ),
                )
                for idx, entry in enumerate(unit.get("media") or []):
                    await self._execute(
                        """
                        INSERT INTO copy_unit_media
                            (bot_id, cid, mid, seq, idx, kind, file_id, caption)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            TgClient.ID,
                            cid,
                            mid,
                            seq,
                            idx,
                            entry.get("kind"),
                            entry.get("file_id"),
                            entry.get("caption"),
                        ),
                    )
            await self._prune_copy_records(user_id)

    async def find_copy_records(self, mid: int) -> list[dict]:
        """Every saved record with this task id -- one chat's mid can repeat.

        The task, unit and media rows are joined into documents shaped like the
        Mongo rows callers read: ``_id`` is ``"cid:mid"`` and the owner field is
        named ``user``. A unit rebuilds its ``chat``/``msg`` only when the source
        coordinates were stored, a media entry only its present keys, and every
        unit carries a ``media`` list -- empty when no media rows were stored.
        """
        if self._return:
            return []
        rows = await self._fetchall(
            """
            SELECT t.bot_id, t.cid, t.mid, t.user_id, t.name, t.at,
                   u.seq, u.mode, u.src_chat, u.src_msg,
                   m.idx, m.kind, m.file_id, m.caption
            FROM copy_tasks t
            LEFT JOIN copy_units u
              ON u.bot_id = t.bot_id AND u.cid = t.cid AND u.mid = t.mid
            LEFT JOIN copy_unit_media m
              ON m.bot_id = u.bot_id AND m.cid = u.cid AND m.mid = u.mid
             AND m.seq = u.seq
            WHERE t.bot_id = %s AND t.mid = %s
            ORDER BY t.cid, t.mid, u.seq, m.idx
            """,
            (TgClient.ID, mid),
        )
        docs: list[dict] = []
        cur_seq: int | None = None
        for row in rows:
            if not docs or (docs[-1]["cid"], docs[-1]["mid"]) != (
                row["cid"],
                row["mid"],
            ):
                docs.append(
                    {
                        "_id": f"{row['cid']}:{row['mid']}",
                        "cid": row["cid"],
                        "mid": row["mid"],
                        "user": row["user_id"],
                        "name": row["name"],
                        "at": row["at"],
                        "units": [],
                    }
                )
                cur_seq = None
            doc = docs[-1]
            if row["seq"] is None:
                continue  # a parent row whose unit rows are gone -> units: []
            if row["seq"] != cur_seq:
                doc["units"].append(_rebuild_unit(row))
                cur_seq = row["seq"]
            if row["idx"] is not None:
                doc["units"][-1]["media"].append(_rebuild_media(row))
        return docs

    async def _prune_copy_records(self, user_id: int) -> None:
        """Drop a user's records past the newest MAX_TASK_RECORDS of them.

        No index is built on user/at: the repo has never made one, and with a
        couple hundred rows per user a table scan per save is cheaper than an
        index whose only reader is this prune. ``at`` can tie between two
        saves in the same second, so ``mid`` breaks the order deterministically.
        """
        if self._return:
            return
        rows = await self._fetchall(
            """
            SELECT cid, mid FROM copy_tasks
            WHERE bot_id = %s AND user_id = %s
            ORDER BY at DESC, mid DESC
            OFFSET %s
            """,
            (TgClient.ID, user_id, MAX_TASK_RECORDS),
        )
        for stale in rows:
            # One delete per stale task; the FK cascade clears its units/media.
            await self._execute(
                """
                DELETE FROM copy_tasks
                WHERE bot_id = %s AND cid = %s AND mid = %s
                """,
                (TgClient.ID, stale["cid"], stale["mid"]),
            )


def _rebuild_unit(row: dict) -> dict:
    """A unit dict from its row, omitting source keys that were never stored."""
    unit: dict[str, Any] = {"mode": row["mode"], "media": []}
    if row["src_chat"] is not None:
        unit["chat"] = row["src_chat"]
    if row["src_msg"] is not None:
        unit["msg"] = row["src_msg"]
    return unit


def _rebuild_media(row: dict) -> dict:
    """One media entry from its row, keeping only the keys that were stored."""
    entry: dict[str, Any] = {}
    for key in ("kind", "file_id", "caption"):
        if row[key] is not None:
            entry[key] = row[key]
    return entry


database = DbManager()
