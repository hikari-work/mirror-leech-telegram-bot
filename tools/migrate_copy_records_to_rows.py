#!/usr/bin/env python3
"""One-shot move of a legacy ``copy_records`` jsonb table into relational rows.

Between the jsonb rewrite and the row normalisation, one finished /copy task
was a single ``copy_records`` row whose ``units`` held every file id and
message coordinate as a nested jsonb list. The runtime now writes
``copy_tasks``/``copy_units``/``copy_unit_media`` instead, so a database that
was filled by the older code still holds old records the bot can no longer
read. This tool flattens those rows the same way ``save_copy_record`` does --
one parent row, one unit row per entry, one media row per file -- then drops
the legacy table.

The row tables must already exist: ``DbManager.connect()`` creates them from
its ``_SCHEMA`` on first boot against the target PG. Run the bot once first,
or apply that DDL by hand.

Idempotent: each task is an upsert plus a child delete-and-reinsert inside one
transaction, so a re-run after a partial failure only fills the gaps and never
duplicates. The table is dropped only after every row has been written.

    python tools/migrate_copy_records_to_rows.py \
        --pg-url "$DATABASE_URL"            # optional: --pg-dbname mltb
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from psycopg import connect as pg_connect
from psycopg.rows import dict_row


def _connect_pg(url: str, dbname: str | None):
    """A sync autocommit connection with dict rows, as DbManager configures."""
    conn = pg_connect(url, dbname=dbname, autocommit=True) if dbname else pg_connect(
        url, autocommit=True
    )
    conn.row_factory = dict_row
    return conn


def _write_rows(pg: Any, row: dict[str, Any]) -> tuple[int, int]:
    """Flatten one legacy copy_records row, returning (units, media) written."""
    bot, cid, mid = row["bot_id"], row["cid"], row["mid"]
    units = row["units"] or []
    with pg.transaction():
        pg.execute(
            "INSERT INTO copy_tasks (bot_id, cid, mid, user_id, name, at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (bot_id, cid, mid) DO UPDATE SET "
            "user_id = EXCLUDED.user_id, name = EXCLUDED.name, "
            "at = EXCLUDED.at",
            (bot, cid, mid, row["user_id"], row["name"], row["at"]),
        )
        # Delete children first so a re-run converges instead of duplicating;
        # the delete cascades from copy_units into copy_unit_media.
        pg.execute(
            "DELETE FROM copy_units WHERE bot_id = %s AND cid = %s AND mid = %s",
            (bot, cid, mid),
        )
        n_units = n_media = 0
        for seq, unit in enumerate(units):
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
            n_units += 1
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
                n_media += 1
    return n_units, n_media


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-url", required=True, help="PostgreSQL URL")
    parser.add_argument("--pg-dbname", default=None, help="optional PG dbname override")
    args = parser.parse_args(argv)

    pg = _connect_pg(args.pg_url, args.pg_dbname)
    present = pg.execute(
        "SELECT to_regclass('copy_records') AS t"
    ).fetchone()["t"]
    if present is None:
        print("No copy_records table -- nothing to do.")
        return 0

    rows = pg.execute(
        "SELECT bot_id, cid, mid, user_id, name, at, units "
        "FROM copy_records ORDER BY bot_id, cid, mid"
    ).fetchall()
    tasks = units = media = 0
    for row in rows:
        n_units, n_media = _write_rows(pg, row)
        tasks += 1
        units += n_units
        media += n_media

    # Only after every row is written: the legacy table is now fully read.
    pg.execute("DROP TABLE IF EXISTS copy_records")
    print(
        f"Migrated {tasks} copy records into {units} unit rows and "
        f"{media} media rows; dropped copy_records."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
