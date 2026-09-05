#!/usr/bin/env python3
"""One-shot move of copy presets out of the users jsonb document into rows.

Until the preset rows landed, a user's copy presets were the ``COPY_PRESETS``
map inside the ``users`` document. The runtime now keeps them in
``copy_presets``/``copy_preset_dests`` and, once this tool has run, reads them
only from there -- so the jsonb key is dead weight and a second source of
truth. This tool merges a stale key into the row tables and strips the key
from the document.

The row tables must already exist: ``DbManager.connect()`` creates them from
its ``_SCHEMA``. Run the bot once (so the C3 runtime has booted and any preset
edits since then are already rows), or apply that DDL by hand.

Merge, never overwrite: a user who already has preset rows is authoritative --
the C3 runtime wrote them after the jsonb key -- so their jsonb key is dropped
without touching the rows. Only a user with no rows yet is backfilled from the
stale key. Idempotent: after the first run no users have the key, so a re-run
has nothing to do.

    python tools/migrate_copy_presets_to_rows.py \
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


def _strip_key(pg: Any, uid: int) -> None:
    """Delete the preset key from one user's jsonb document, whatever it holds."""
    pg.execute(
        "UPDATE users SET data = data - 'COPY_PRESETS' WHERE user_id = %s", (uid,)
    )


def _migrate_user(pg: Any, uid: int, presets: dict[str, list[str]]) -> int:
    """Backfill one user's preset rows from their jsonb key; returns rows moved.

    Called only when the user has no preset rows yet, so the inserts cannot
    collide. A preset with no destinations keeps its parent row alone.
    """
    moved = 0
    with pg.transaction():
        for name, dests in presets.items():
            pg.execute(
                "INSERT INTO copy_presets (user_id, name) VALUES (%s, %s)",
                (uid, name),
            )
            for seq, dest in enumerate(dests or []):
                pg.execute(
                    "INSERT INTO copy_preset_dests "
                    "(user_id, name, dst_seq, dest) VALUES (%s, %s, %s, %s)",
                    (uid, name, seq, dest),
                )
            moved += 1
        _strip_key(pg, uid)
    return moved


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-url", required=True, help="PostgreSQL URL")
    parser.add_argument("--pg-dbname", default=None, help="optional PG dbname override")
    args = parser.parse_args(argv)

    pg = _connect_pg(args.pg_url, args.pg_dbname)
    if pg.execute("SELECT to_regclass('copy_presets') AS t").fetchone()["t"] is None:
        raise SystemExit(
            "copy_presets does not exist. Boot the bot once against this "
            "database so its schema creates the row tables, then re-run."
        )

    rows = pg.execute(
        "SELECT user_id, data->'COPY_PRESETS' AS presets "
        "FROM users WHERE data ? 'COPY_PRESETS'"
    ).fetchall()
    moved = 0
    for row in rows:
        uid, presets = row["user_id"], row["presets"]
        if not isinstance(presets, dict):
            # A leftover "" or [] opinion: nothing to backfill, just remove it.
            _strip_key(pg, uid)
            continue
        # Rows already present mean the runtime is authoritative -- drop the
        # stale key without backfilling over the newer rows.
        has_rows = pg.execute(
            "SELECT 1 FROM copy_presets WHERE user_id = %s LIMIT 1", (uid,)
        ).fetchone()
        if has_rows is None:
            n = _migrate_user(pg, uid, presets)
            moved += n
            print(f"user {uid}: moved {n} preset(s) from the jsonb document")
        else:
            _strip_key(pg, uid)
            print(f"user {uid}: preset rows already present; dropped the stale key")

    print(
        f"Moved {moved} preset(s) into rows and dropped the stale jsonb key "
        f"from {len(rows)} user document(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
