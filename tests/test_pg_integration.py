"""Behavioural tests against a real PostgreSQL, gated by ``PG_TEST_URL``.

The hermetic suite pins the SQL each DbManager method emits; here the store is
actually there, so the properties that depend on it are proven: ``||`` jsonb
merge vs whole-document replace, one bot's rows never leaking into another's,
the global ``users`` table, blob revision-upserts, the notifier's read-then-
forget, the per-user copy-record prune, and every subscriber's feeds going in
as one write. Every test writes under its own bot
id and user id so none collides with another, and the whole module is skipped
unless ``PG_TEST_URL`` points at a reachable server:

    PG_TEST_URL=postgresql://mltb:mltb@localhost:55432/mltb_test \\
        .venv/bin/python -m pytest tests/test_pg_integration.py -q
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from uuid import uuid4

import pytest

from bot import rss_dict, user_data
from bot.core import startup
from bot.core.config_manager import Config
from bot.core.telegram_manager import TgClient
from bot.helper.storage.copy_records import MAX_TASK_RECORDS
from bot.helper.storage.db_handler import DbManager

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL is not set"
    ),
]


@pytest.fixture
async def dbm(monkeypatch):
    """A connected DbManager under a throwaway bot id, torn down per test."""
    url = os.getenv("PG_TEST_URL", "")
    monkeypatch.setattr(Config, "DATABASE_URL", url)
    monkeypatch.setattr(Config, "DATABASE_NAME", "")
    bot = f"it-{uuid4().hex[:10]}"
    monkeypatch.setattr(TgClient, "ID", bot)

    manager = DbManager()
    await manager.connect()
    assert manager.is_connected, f"could not connect to {url}"
    manager._bot = bot
    yield manager
    await manager.disconnect()


# ── settings: merge vs replace, and per-bot isolation ─────────────────


async def test_update_config_merges_but_replace_config_replaces(dbm):
    await dbm.update_config({"A": 1}, bot_id=dbm._bot)
    await dbm.update_config({"B": 2}, bot_id=dbm._bot)
    assert await dbm.read_config(dbm._bot) == {"A": 1, "B": 2}

    await dbm.replace_config({"C": 3}, bot_id=dbm._bot)
    # the whole document is gone, not just overlaid
    assert await dbm.read_config(dbm._bot) == {"C": 3}


async def test_one_bots_settings_never_leak_into_another(dbm):
    other = f"it-{uuid4().hex[:10]}"

    await dbm.update_config({"A": 1}, bot_id=dbm._bot)

    assert await dbm.read_config(dbm._bot) == {"A": 1}
    assert await dbm.read_config(other) is None


# ── users: global across bots, replaced wholesale ─────────────────────


async def test_users_table_is_shared_and_replace_wholesale(dbm):
    uid = -int(uuid4().hex[:8], 16)  # a realistic negative telegram id

    await dbm.save_user_row(uid, {"THUMBNAIL": "a", "AS_DOCUMENT": True})
    await dbm.save_user_row(uid, {"AS_DOCUMENT": False})

    rows = dict(await dbm.read_user_rows())
    # the first record's fields did not survive the second write
    assert rows[uid] == {"AS_DOCUMENT": False}
    # ... and the write happened with no bot scope at all
    await dbm.update_config({"A": 1}, bot_id=dbm._bot)
    assert dict(await dbm.read_user_rows())[uid] == {"AS_DOCUMENT": False}


async def test_copy_presets_live_in_rows_not_the_users_doc(dbm):
    uid = -int(uuid4().hex[:8], 16)
    user_data[uid] = {
        "COPY_PRESETS": {"anime": ["pm", "@updates", "-1001501001|2"], "empty": []},
        "AS_DOCUMENT": True,
    }

    await dbm.update_user_data(uid)

    try:
        # the jsonb document no longer carries the preset key ...
        assert dict(await dbm.read_user_rows())[uid] == {"AS_DOCUMENT": True}
        # ... and the preset rows round-trip with the tokens the user typed
        presets = await dbm.read_copy_presets_all()
        assert presets.get(uid) == {
            "anime": ["pm", "@updates", "-1001501001|2"],
            "empty": [],
        }

        # A second save replaces the set rather than adding to it: "anime" loses
        # a destination and "empty" is dropped. This is the ordering the two
        # inserts have to get right -- parents before children -- against a
        # server that enforces the foreign key, and the delete has to clear the
        # old destination rows before their replacements arrive.
        user_data[uid]["COPY_PRESETS"] = {"anime": ["pm"]}
        await dbm.update_user_data(uid)

        assert (await dbm.read_copy_presets_all()).get(uid) == {"anime": ["pm"]}
    finally:
        del user_data[uid]


# ── user-document blobs ───────────────────────────────────────────────


async def test_user_doc_blob_round_trip(dbm, tmp_path):
    uid = -int(uuid4().hex[:8], 16)
    doc = tmp_path / "5.jpg"
    doc.write_bytes(b"pixels")

    await dbm.update_user_doc(uid, "THUMBNAIL", path=str(doc))
    # the name is namespaced by the (real) bot id, but keyed under the user
    assert await dbm.get_blob(f"users/{uid}/THUMBNAIL") == b"pixels"

    await dbm.update_user_doc(uid, "THUMBNAIL")
    assert await dbm.get_blob(f"users/{uid}/THUMBNAIL") is None


# ── blobs: one revision per name, prefix listing ──────────────────────


async def test_blob_save_is_one_revision_and_delete_removes_it(dbm):
    await dbm.save_blob("a.bin", b"v1", bot_id=dbm._bot)
    await dbm.save_blob("a.bin", b"v2", bot_id=dbm._bot)

    assert await dbm.get_blob("a.bin", bot_id=dbm._bot) == b"v2"

    await dbm.delete_blob("a.bin", bot_id=dbm._bot)
    assert await dbm.get_blob("a.bin", bot_id=dbm._bot) is None


async def test_blob_list_is_scoped_to_prefix_and_namespace(dbm):
    await dbm.save_blob("thumb/1.jpg", b"a", bot_id=dbm._bot)
    await dbm.save_blob("thumb/2.jpg", b"b", bot_id=dbm._bot)
    await dbm.save_blob("other/1.jpg", b"c", bot_id=dbm._bot)

    # the returned names have the bot namespace stripped
    assert await dbm.list_blobs(bot_id=dbm._bot) == [
        "other/1.jpg",
        "thumb/1.jpg",
        "thumb/2.jpg",
    ]
    assert await dbm.list_blobs("thumb/", bot_id=dbm._bot) == ["1.jpg", "2.jpg"]


async def test_a_blob_of_another_bot_is_invisible(dbm):
    other = f"it-{uuid4().hex[:10]}"
    await dbm.save_blob("a.bin", b"mine", bot_id=dbm._bot)

    assert await dbm.get_blob("a.bin", bot_id=other) is None
    assert await dbm.list_blobs(bot_id=other) == []


# ── incomplete tasks: grouped once, then forgotten ────────────────────


async def test_incomplete_tasks_group_by_chat_then_are_forgotten(dbm):
    await dbm.add_incomplete_task(-100, "link-a", "tag")
    await dbm.add_incomplete_task(-100, "link-b", "tag")
    await dbm.add_incomplete_task(-100, "link-a", "tag")  # duplicate, no-op
    await dbm.add_incomplete_task(-200, "link-c", "other")

    assert await dbm.get_incomplete_tasks() == {
        -100: {"tag": ["link-a", "link-b"]},
        -200: {"other": ["link-c"]},
    }
    # the notifier runs once per restart: a second read is empty
    assert await dbm.get_incomplete_tasks() == {}


# ── copy records: shaping and the per-user prune ──────────────────────


async def test_copy_records_round_trip_is_shaped_like_the_old_documents(dbm):
    album = [
        {"mode": "group", "chat": -1001, "msg": 70, "media": [
            {"kind": "photo", "file_id": "a1", "caption": "one"},
            {"kind": "document", "file_id": "a2", "caption": ""},
        ]},
        {"mode": "single", "chat": -1001, "msg": 71, "media": [
            {"kind": "video", "file_id": "a3", "caption": "tail"},
        ]},
    ]
    await dbm.save_copy_record(-1001, 7, 42, "a folder", album)
    # a unit with neither coordinates nor media is legitimate on the edge
    await dbm.save_copy_record(-1001, 8, 42, "sparse", [{"mode": "single"}])

    # the album replays with its units and media in the recorded seq/idx order
    (doc,) = await dbm.find_copy_records(7)
    assert doc["_id"] == "-1001:7"
    assert (doc["cid"], doc["mid"], doc["user"], doc["name"]) == (-1001, 7, 42,
                                                                  "a folder")
    assert isinstance(doc["at"], int)
    assert doc["units"] == album
    # a stored unit with no media rows is normalised to an empty media list
    (sparse,) = await dbm.find_copy_records(8)
    assert sparse["units"] == [{"mode": "single", "media": []}]

    # and the flattening really happened: rows, not a jsonb blob
    assert await _count(dbm, "copy_units", dbm._bot) == 3  # 2 album + 1 sparse
    assert await _count(dbm, "copy_unit_media", dbm._bot) == 3  # all on the album


async def _count(dbm: DbManager, table: str, bot: str) -> int:
    """Rows of one bot in a table -- the shape of a table-level assertion."""
    rows = await dbm._fetchall(
        f"SELECT count(*) AS n FROM {table} WHERE bot_id = %s", (bot,)
    )
    return rows[0]["n"]


async def test_the_prune_is_per_user_and_spares_others(dbm):
    other_user = 43
    await dbm.save_copy_record(-1001, 0, other_user, "old but theirs", [])

    for mid in range(1, MAX_TASK_RECORDS + 5):
        await dbm.save_copy_record(-1001, mid, 42, f"bulk {mid}", [])

    # user 42 is trimmed to their newest MAX_TASK_RECORDS ...
    rows = await dbm._fetchall(
        "SELECT user_id FROM copy_tasks WHERE bot_id = %s", (dbm._bot,)
    )
    counts = Counter(row["user_id"] for row in rows)
    assert counts[42] == MAX_TASK_RECORDS
    # ... while the flood never touched user 43's single record
    assert counts[other_user] == 1
    # their record -- the only one with mid 0 -- is still findable
    assert [d["user"] for d in await dbm.find_copy_records(0)] == [other_user]


async def test_a_pruned_album_takes_its_units_and_media_with_it(dbm):
    """The one-statement delete leans on the cascade, so the cascade is pinned.

    Nothing names the child rows: they go because their parent went. Under the
    per-row delete this held too, but it held per deleted row -- counted here,
    because a delete that matched the parent but missed a child table's foreign
    key would leave orphans that ``/copy`` would go on replaying.
    """
    for mid in range(MAX_TASK_RECORDS + 1):
        await dbm.save_copy_record(
            -1001,
            mid,
            42,
            f"album {mid}",
            [
                {"mode": "group", "chat": -1001, "msg": 100 + mid, "media": [
                    {"kind": "photo", "file_id": f"p{mid}", "caption": ""},
                ]},
                {"mode": "single", "chat": -1001, "msg": 200 + mid, "media": [
                    {"kind": "video", "file_id": f"v{mid}", "caption": ""},
                ]},
            ],
        )

    # mid 0 was the oldest, so the save of the last mid pruned it
    assert await dbm.find_copy_records(0) == []
    # 2 units and 2 media rows per album survived, and not one row more: the
    # pruned album's four child rows went with it
    assert await _count(dbm, "copy_units", dbm._bot) == MAX_TASK_RECORDS * 2
    assert await _count(dbm, "copy_unit_media", dbm._bot) == MAX_TASK_RECORDS * 2


# ── rss: every subscriber's feeds in one write ────────────────────────


async def test_rss_update_all_writes_and_then_updates_every_user(dbm):
    """The whole map in one statement, and a second call replaces rather than adds.

    The upsert is the load-bearing part: writing every user in one ``VALUES``
    only holds if ``ON CONFLICT`` still fires for the ones already stored, and
    the unit tests pin that clause as text, not as behaviour.
    """
    rss_dict.update({
        1: {"https://a/rss": {"title": "one"}},
        2: {"https://b/rss": {"title": "two"}},
    })
    await dbm.rss_update_all(bot_id=dbm._bot)

    assert dict(await dbm.read_rss_rows(dbm._bot)) == {
        1: {"https://a/rss": {"title": "one"}},
        2: {"https://b/rss": {"title": "two"}},
    }

    rss_dict[2] = {"https://b/rss": {"title": "renamed"}}
    await dbm.rss_update_all(bot_id=dbm._bot)

    rows = dict(await dbm.read_rss_rows(dbm._bot))
    assert rows[2] == {"https://b/rss": {"title": "renamed"}}
    # Replaced in place: a second row for user 2 would have been collapsed by
    # the dict above, and the primary key would not have caught it here -- the
    # bot id is part of that key, and every run of this test brings a new one.
    assert await _count_where(
        dbm, "rss", "bot_id = %s AND user_id = %s", (dbm._bot, 2)
    ) == 1
    rss_dict.clear()  # shared with the rest of the session, like user_data


async def _count_where(dbm: DbManager, table: str, where: str, params) -> int:
    """Rows matching *where* in a table -- ``_count`` for a narrower question."""
    rows = await dbm._fetchall(
        f"SELECT count(*) AS n FROM {table} WHERE {where}", params
    )
    return rows[0]["n"]


async def test_rss_update_all_with_no_subscribers_is_accepted(dbm):
    """The guard, against a server that would reject the statement without it.

    ``VALUES`` with no row after it does not run as a no-op -- PostgreSQL fails
    to parse it at the ``ON`` -- so this is the case that separates "writes
    nothing" from "raises". A bot whose users have no feeds reaches this on
    every ``save_everyone``.
    """
    rss_dict.clear()

    await dbm.rss_update_all(bot_id=dbm._bot)

    assert await dbm.read_rss_rows(dbm._bot) == []


async def test_a_user_whose_only_record_is_a_thumbnail_comes_back(
    dbm, monkeypatch, tmp_path
):
    """The boot restore, through the real ``list_blobs`` rather than a stub.

    A user who only ever uploaded a thumbnail owns a blob and no row: the
    upload calls ``update_user_doc``, never ``update_user_data``. ``load_settings``
    wipes ``thumbnails/`` at every start, so if this merge misses them their
    thumbnail is gone from disk and from ``user_data`` until they upload it
    again -- which is what the prefix handed to ``list_blobs`` used to cause.
    That prefix's effect on the names is exactly what a stub cannot show, so
    this asserts against the store that defines it.
    """
    uid = -int(uuid4().hex[:8], 16)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(startup, "user_data", {})
    monkeypatch.setattr(startup, "database", dbm)
    await dbm.save_blob(f"users/{uid}/THUMBNAIL", b"pixels", bot_id=dbm._bot)

    await startup.restore_users(dbm._bot)

    assert startup.user_data[uid]["THUMBNAIL"] == f"thumbnails/{uid}.jpg"
    assert (tmp_path / "thumbnails" / f"{uid}.jpg").read_bytes() == b"pixels"


async def test_restoring_users_fetches_only_the_thumbnails_that_exist(
    dbm, monkeypatch, tmp_path
):
    """One row per user, one blob for two of them: two fetches, not four.

    The listing is the same query the merge already needs, so the users holding
    nothing cost nothing. Pinned against the store because the property is
    about what the database answers, not about the SQL this module wrote.
    """
    with_thumb = -int(uuid4().hex[:8], 16)
    without = -int(uuid4().hex[:8], 16)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(startup, "user_data", {})
    monkeypatch.setattr(startup, "database", dbm)
    await dbm.save_user_row(with_thumb, {"AS_DOCUMENT": True})
    await dbm.save_user_row(without, {"AS_DOCUMENT": False})
    await dbm.save_blob(f"users/{with_thumb}/THUMBNAIL", b"one", bot_id=dbm._bot)

    fetches = []
    real_get_blob = dbm.get_blob

    async def counting_get_blob(path, bot_id=None):
        fetches.append(path)
        return await real_get_blob(path, bot_id=bot_id)

    monkeypatch.setattr(dbm, "get_blob", counting_get_blob)

    await startup.restore_users(dbm._bot)

    assert fetches == [f"users/{with_thumb}/THUMBNAIL"]
    assert startup.user_data[with_thumb]["THUMBNAIL"] == (
        f"thumbnails/{with_thumb}.jpg"
    )
    assert startup.user_data[without] == {"AS_DOCUMENT": False}


async def test_connect_lets_database_name_pick_the_database(monkeypatch):
    """``DATABASE_NAME`` overrides whatever database the URL arrives at.

    This is the branch the bot normally takes: the setting defaults to ``mltb``,
    so it is nearly always set. It also has to survive the move to a pool, which
    takes it through ``make_conninfo`` rather than by handing ``dbname`` to
    ``connect()``. The URL below deliberately points somewhere else.

    The assertion has to be the database's own name rather than "the tables are
    there": ``connect()`` creates the schema wherever it lands, so a connection
    to the wrong database would still leave every table in place.
    """
    url = os.getenv("PG_TEST_URL", "")
    monkeypatch.setattr(Config, "DATABASE_URL", url.replace("/mltb_test", "/postgres"))
    monkeypatch.setattr(Config, "DATABASE_NAME", "mltb_test")
    monkeypatch.setattr(TgClient, "ID", f"it-{uuid4().hex[:10]}")

    manager = DbManager()
    await manager.connect()
    try:
        assert manager.is_connected
        (row,) = await manager._fetchall("SELECT current_database() AS db")
        assert row["db"] == "mltb_test"
    finally:
        await manager.disconnect()


async def test_the_copy_lookup_index_is_really_installed(dbm):
    """``/copy`` filters on ``(bot_id, mid)``, which the primary key cannot serve.

    ``copy_tasks`` is keyed ``(bot_id, cid, mid)``, so ``mid`` is the third
    column and a lookup by it scans every bot's history. The index is declared
    in ``_SCHEMA``; this reads the columns back out of the catalog, so it fails
    if the declaration is dropped *or* if ``connect()`` stops applying it.

    It deliberately does not assert the planner uses it -- on a table this small
    a sequential scan is the correct plan, and asserting otherwise would be
    testing Postgres rather than this module.
    """
    (index,) = await dbm._fetchall(
        """
        SELECT i.indexrelid::regclass::text AS name
        FROM pg_index i
        WHERE i.indexrelid = to_regclass('copy_tasks_bot_mid_idx')
        """,
    )
    assert index["name"] == "copy_tasks_bot_mid_idx"

    # one row per key column, in the order the index declares them --
    # ``pg_get_indexdef`` rather than the ``indkey`` vector, whose ``= ANY``
    # silently matches only the first entry.
    columns = await dbm._fetchall(
        """
        SELECT pg_get_indexdef(i.indexrelid, k, true) AS column
        FROM pg_index i, generate_series(1, i.indnkeyatts) AS k
        WHERE i.indexrelid = to_regclass('copy_tasks_bot_mid_idx')
        ORDER BY k
        """,
    )
    assert [row["column"] for row in columns] == ["bot_id", "mid"]


async def test_disconnect_after_connect_returns_to_noop(dbm):
    await dbm.disconnect()
    assert not dbm.is_connected
    await dbm.save_blob("a", b"b", bot_id=dbm._bot)
    assert await dbm.get_blob("a", bot_id=dbm._bot) is None


# ── the two tables a restart reads ────────────────────────────────────


async def test_an_active_task_survives_the_reconnection_it_exists_for(dbm) -> None:
    """Written before a restart, read after one -- which is a reconnection.

    ``data`` carries the parsed arguments, and one of them is a set of tuples:
    neither type exists in json, so the serializer tags both. What this checks is
    that the tag survives the jsonb column rather than being flattened into a
    list on the way in, because a reader handed a list would give
    ``_resolve_ffmpeg_commands`` something the task never had.
    """
    from bot.helper.util.task_args import LeechArgs, dump_args, load_args

    args = LeechArgs(
        extract="pw",
        ffmpeg_cmds={("-vf", "scale=1280:-2"), ("-c:a", "aac")},
        headers=["cookie: x"],
    )
    await dbm.add_active_task(
        42, -100, 10, 7, "@user",
        {"schema": 1, "engine": "aria2", "engine_dir": "/d/42", "args": dump_args(args)},
    )

    await dbm.disconnect()
    await dbm.connect()

    (row,) = await dbm.get_active_tasks()
    assert row["mid"] == 42
    assert row["state"] == "dl"
    assert row["data"]["engine_dir"] == "/d/42"
    rebuilt = load_args(row["data"]["args"])
    assert isinstance(rebuilt.ffmpeg_cmds, set)
    assert rebuilt.ffmpeg_cmds == args.ffmpeg_cmds
    assert rebuilt.extract == "pw"
    assert rebuilt.headers == ["cookie: x"]


async def test_a_dispatched_task_is_replaced_rather_than_duplicated(dbm) -> None:
    """One row per task, describing the run that is happening now.

    A task can reach the dispatch twice -- the option keyboard applied again, a
    re-queue -- and two rows for one mid would have the recovery pass rebuild it
    twice, each against the same engine job.
    """
    await dbm.add_active_task(42, -100, 10, 7, "@u", {"schema": 1})
    await dbm.add_active_task(42, -100, 11, 7, "@u", {"schema": 1, "engine": "qbit"})

    rows = await dbm.get_active_tasks()

    assert len(rows) == 1
    assert rows[0]["cmd_msg_id"] == 11
    assert rows[0]["data"]["engine"] == "qbit"


async def test_one_bots_task_row_is_not_another_bots(dbm, monkeypatch) -> None:
    """Two bots can share a message id, and the pass reads only its own."""
    from bot.core.telegram_manager import TgClient

    await dbm.add_active_task(42, -100, 10, 7, "@u", {"schema": 1})
    monkeypatch.setattr(TgClient, "ID", "another-bot")

    assert await dbm.get_active_tasks() == []


async def test_uploaded_files_come_back_in_the_order_they_were_sent(dbm) -> None:
    """Order is what the completion report is rebuilt from."""
    for seq, name in enumerate(["album/b.mkv", "album/a.mkv"], start=1):
        await dbm.add_uploaded_file(
            42, name, seq, -100, 100 + seq, f"https://t/{seq}", name, None
        )

    rows = await dbm.get_uploaded_files(42)

    # by send order, not by name: b went out first
    assert [row["relpath"] for row in rows] == ["album/b.mkv", "album/a.mkv"]
    assert [row["seq"] for row in rows] == [1, 2]


async def test_the_files_an_album_absorbed_are_re_pointed_at_the_album(dbm) -> None:
    """The messages the album carried are deleted, so their links die with them.

    A resumed task rebuilds its report from these rows, and a link to a message
    telegram no longer has is worse than no link at all.
    """
    await dbm.add_uploaded_file(42, "a.mkv", 1, -100, 10, "https://t/10", "a", None)
    await dbm.add_uploaded_file(42, "b.mkv", 2, -100, 11, "https://t/11", "b", None)

    await dbm.rewrite_uploaded_album(
        42,
        # a list, not a set: the two conditions go into the statement in this
        # order, and a set would hand them over in an arbitrary one
        [(-100, 10), (-100, 11)],
        -100,
        99,
        "https://t/99",
        {"m": 1},
    )

    rows = await dbm.get_uploaded_files(42)
    assert [(r["msg_id"], r["link"]) for r in rows] == [(99, "https://t/99")] * 2


async def test_rerecording_a_file_updates_it_in_place(dbm) -> None:
    """A file sent twice must be counted once, or the report says it twice."""
    await dbm.add_uploaded_file(42, "a.mkv", 1, -100, 10, "https://t/10", "a", None)
    await dbm.add_uploaded_file(42, "a.mkv", 5, -100, 99, "", "a", None)

    rows = await dbm.get_uploaded_files(42)

    assert len(rows) == 1
    assert rows[0]["seq"] == 5
    assert rows[0]["msg_id"] == 99


async def test_clearing_one_task_leaves_the_others_alone(dbm) -> None:
    """Two tasks can be uploading at once, and only one of them is finishing."""
    await dbm.add_active_task(42, -100, 10, 7, "@u", {"schema": 1})
    await dbm.add_active_task(43, -100, 11, 7, "@u", {"schema": 1})
    await dbm.add_uploaded_file(42, "a.mkv", 1, -100, 10, "", "a", None)
    await dbm.add_uploaded_file(43, "b.mkv", 1, -100, 11, "", "b", None)

    await dbm.rm_active_task(42)
    await dbm.rm_uploaded_files(42)

    assert [row["mid"] for row in await dbm.get_active_tasks()] == [43]
    assert await dbm.get_uploaded_files(42) == []
    assert [row["relpath"] for row in await dbm.get_uploaded_files(43)] == ["b.mkv"]


# ── two tasks writing at the same time ────────────────────────────────

_INSERT_TASK = """
    INSERT INTO copy_tasks (bot_id, cid, mid, user_id, name, at)
    VALUES (%s, %s, %s, %s, %s, %s)
"""


async def test_two_transactions_at_once_stay_separate(dbm):
    """A rollback must not take another task's committed work with it.

    On one shared connection this cannot hold. psycopg decides whether to issue
    ``BEGIN`` or ``SAVEPOINT`` by asking the connection whether a transaction is
    already open, and a second ``_txn`` arriving while the first is still
    running finds one -- so its writes join the first task's transaction, and
    when that one rolls back they go with it. Each task now checks a connection
    out of the pool for the length of its own transaction, so this is two
    transactions rather than one nested inside the other.
    """
    first_has_written = asyncio.Event()
    second_has_committed = asyncio.Event()

    async def doomed():
        try:
            async with dbm._txn():
                await dbm._execute(
                    _INSERT_TASK, (dbm._bot, -1001, 900, 42, "doomed", 1)
                )
                first_has_written.set()
                # held open on purpose: the second transaction runs entirely
                # inside this one's lifetime
                await second_has_committed.wait()
                raise RuntimeError("this task failed halfway")
        except RuntimeError:
            pass

    async def finishes_cleanly():
        await first_has_written.wait()
        async with dbm._txn():
            await dbm._execute(
                _INSERT_TASK, (dbm._bot, -1001, 901, 42, "committed", 1)
            )
        second_has_committed.set()

    await asyncio.gather(doomed(), finishes_cleanly())

    stored = {
        row["mid"]
        for row in await dbm._fetchall(
            "SELECT mid FROM copy_tasks WHERE bot_id = %s", (dbm._bot,)
        )
    }
    assert 901 in stored  # the task that finished has its row
    assert 900 not in stored  # the one that failed does not
