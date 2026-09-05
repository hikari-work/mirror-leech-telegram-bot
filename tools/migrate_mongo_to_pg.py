#!/usr/bin/env python3
"""One-shot migration of an mltb Mongo deployment into PostgreSQL.

Reads the Mongo layout that ``db_handler.py`` used before the PostgreSQL
rewrite (the git history before the swap commit holds it) and writes every
document into the matching jsonb table, so the bot restarted against the new
database keeps all settings, user records, RSS feeds, unfinished tasks, /copy
records and stored private files.

The tables must already exist: they are created by ``DbManager.connect()`` on
the bot's first boot against the target PG (the DDL lives in
``bot/helper/storage/db_handler.py`` as ``_SCHEMA``). Run the bot once against
an empty database first, or apply that DDL by hand.

``pymongo`` is needed only for this one-shot read and is deliberately not a
runtime dependency anymore:

    pip install pymongo
    python tools/migrate_mongo_to_pg.py \
        --mongo-uri "$MONGO_URL" --mongo-db mltb \
        --pg-url "$DATABASE_URL"            # optional: --pg-dbname mltb

Idempotent: every write is an upsert, so a re-run after a partial failure only
fills the gaps.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from psycopg import connect as pg_connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Keys whose value used to be a path to a stored file in the old users
# documents. The file itself is migrated separately; the scalar is not.
# Mirrors USER_DOC_KEYS in db_handler.py.
_USER_DOC_KEYS = ("THUMBNAIL",)

# Old settings collection -> new table. Keys are fixed literals, so splicing
# the table name into SQL below is injection-safe.
_SETTINGS_TABLES = {
    "settings.config": "settings_config",
    "settings.deployConfig": "settings_deploy",
    "settings.aria2c": "settings_aria2",
    "settings.qbittorrent": "settings_qbit",
}


def _connect_pg(url: str, dbname: str | None):
    """A sync autocommit connection with dict rows, as DbManager configures."""
    conn = pg_connect(url, dbname=dbname, autocommit=True) if dbname else pg_connect(
        url, autocommit=True
    )
    conn.row_factory = dict_row
    return conn


def _connect_mongo(uri: str, dbname: str):
    """Lazy pymongo import: it is a migration-time dependency only."""
    try:
        from pymongo import MongoClient
    except ImportError:
        raise SystemExit(
            "pymongo is not installed. It is needed only for this one-shot "
            "read: `pip install pymongo` first."
        ) from None
    return MongoClient(uri)[dbname]


def _without_id(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


def _discover_bots(mongo: Any) -> list[str]:
    """Every bot the database holds, from its per-bot and settings docs."""
    bots: set[str] = set()
    for name in mongo.list_collection_names():
        for prefix in ("rss.", "tasks.", "copies."):
            if name.startswith(prefix):
                bots.add(name[len(prefix) :])
    for doc in mongo["settings.config"].find({}, {"_id": 1}):
        bots.add(doc["_id"])
    return sorted(bots)


def _migrate_settings(pg: Any, mongo: Any) -> int:
    count = 0
    for source, table in _SETTINGS_TABLES.items():
        for doc in mongo[source].find():
            bot = doc.get("_id")
            if not bot:
                continue
            pg.execute(
                f"INSERT INTO {table} (bot_id, data) VALUES (%s, %s) "
                f"ON CONFLICT (bot_id) DO UPDATE "
                f"SET data = {table}.data || EXCLUDED.data",
                (bot, Jsonb(_without_id(doc))),
            )
            count += 1
    return count


def _migrate_users(pg: Any, mongo: Any) -> int:
    count = 0
    for doc in mongo["users"].find():
        uid = doc.get("_id")
        if uid is None:
            continue
        data = {
            k: v for k, v in doc.items() if k != "_id" and k not in _USER_DOC_KEYS
        }
        pg.execute(
            "INSERT INTO users (user_id, data) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
            (uid, Jsonb(data)),
        )
        count += 1
    return count


def _migrate_rss(pg: Any, mongo: Any, bot: str) -> int:
    count = 0
    for doc in mongo[f"rss.{bot}"].find():
        pg.execute(
            "INSERT INTO rss (bot_id, user_id, data) VALUES (%s, %s, %s) "
            "ON CONFLICT (bot_id, user_id) DO UPDATE SET data = EXCLUDED.data",
            (bot, doc["_id"], Jsonb(_without_id(doc))),
        )
        count += 1
    return count


def _migrate_tasks(pg: Any, mongo: Any, bot: str) -> int:
    count = 0
    for doc in mongo[f"tasks.{bot}"].find():
        pg.execute(
            "INSERT INTO incomplete_tasks (bot_id, link, cid, tag) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (bot_id, link) DO NOTHING",
            (bot, doc["_id"], doc.get("cid"), doc.get("tag")),
        )
        count += 1
    return count


def _migrate_copies(pg: Any, mongo: Any, bot: str) -> int:
    count = 0
    for doc in mongo[f"copies.{bot}"].find():
        cid, mid = doc.get("cid"), doc.get("mid")
        if cid is None or mid is None:
            continue
        # Each task is a parent row plus its unit/media children, rewritten as
        # the bot stores them today. One transaction keeps the trio atomic and
        # the delete-before-insert makes a re-run converge instead of duping.
        with pg.transaction():
            pg.execute(
                "INSERT INTO copy_tasks (bot_id, cid, mid, user_id, name, at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (bot_id, cid, mid) DO UPDATE SET "
                "user_id = EXCLUDED.user_id, name = EXCLUDED.name, "
                "at = EXCLUDED.at",
                (bot, cid, mid, doc.get("user"), doc.get("name"), doc.get("at")),
            )
            pg.execute(
                "DELETE FROM copy_units WHERE bot_id = %s AND cid = %s AND mid = %s",
                (bot, cid, mid),
            )
            for seq, unit in enumerate(doc.get("units") or []):
                pg.execute(
                    "INSERT INTO copy_units "
                    "(bot_id, cid, mid, seq, mode, src_chat, src_msg) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        bot,
                        cid,
                        mid,
                        seq,
                        unit.get("mode"),
                        unit.get("chat"),
                        unit.get("msg"),
                    ),
                )
                for idx, entry in enumerate(unit.get("media") or []):
                    pg.execute(
                        "INSERT INTO copy_unit_media "
                        "(bot_id, cid, mid, seq, idx, kind, file_id, caption) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            bot,
                            cid,
                            mid,
                            seq,
                            idx,
                            entry.get("kind"),
                            entry.get("file_id"),
                            entry.get("caption"),
                        ),
                    )
        count += 1
    return count


def _migrate_blobs(pg: Any, db: Any) -> int:
    """Copy stored private files; a name keeps its newest revision."""
    try:
        from gridfs import GridFSBucket
    except ImportError:
        raise SystemExit(
            "gridfs is not installed. `pip install pymongo` provides it."
        ) from None
    fs = GridFSBucket(db, bucket_name="files")
    count = 0
    for file_doc in fs.find():
        name = file_doc.filename
        with fs.open_download_stream_by_name(name) as gout:
            data = gout.read()
        pg.execute(
            "INSERT INTO blobs (name, data) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE "
            "SET data = EXCLUDED.data, updated_at = now()",
            (name, data),
        )
        count += 1
    return count


def _migrate_one_bot(pg: Any, mongo: Any, bot: str, totals: dict[str, int]) -> None:
    totals["rss"] += _migrate_rss(pg, mongo, bot)
    totals["tasks"] += _migrate_tasks(pg, mongo, bot)
    totals["copies"] += _migrate_copies(pg, mongo, bot)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-uri", required=True, help="old Mongo server URL")
    parser.add_argument("--mongo-db", default="mltb", help="old database name")
    parser.add_argument("--pg-url", required=True, help="PostgreSQL URL")
    parser.add_argument("--pg-dbname", default=None, help="optional PG dbname override")
    parser.add_argument(
        "--bot-id", default=None, help="restrict to one bot (default: all found)"
    )
    args = parser.parse_args(argv)

    mongo = _connect_mongo(args.mongo_uri, args.mongo_db)
    pg = _connect_pg(args.pg_url, args.pg_dbname)

    bots = [args.bot_id] if args.bot_id else _discover_bots(mongo)
    if not bots:
        print("No bots found in the Mongo database -- nothing to migrate.")
    totals = {"rss": 0, "tasks": 0, "copies": 0}
    for bot in bots:
        _migrate_one_bot(pg, mongo, bot, totals)
        print(f"bot {bot}: rss {totals['rss']}, tasks {totals['tasks']}, "
              f"copies {totals['copies']} (running totals)")

    settings = _migrate_settings(pg, mongo)
    users = _migrate_users(pg, mongo)
    blobs = _migrate_blobs(pg, mongo)

    print(
        f"Migrated: settings {settings}, users {users}, blobs {blobs}, "
        f"rss {totals['rss']}, tasks {totals['tasks']}, copies {totals['copies']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
