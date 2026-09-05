"""Behavioural tests against a real PostgreSQL, gated by ``PG_TEST_URL``.

The hermetic suite pins the SQL each DbManager method emits; here the store is
actually there, so the properties that depend on it are proven: ``||`` jsonb
merge vs whole-document replace, one bot's rows never leaking into another's,
the global ``users`` table, blob revision-upserts, the notifier's read-then-
forget, and the per-user copy-record prune. Every test writes under its own bot
id and user id so none collides with another, and the whole module is skipped
unless ``PG_TEST_URL`` points at a reachable server:

    PG_TEST_URL=postgresql://mltb:mltb@localhost:55432/mltb_test \\
        .venv/bin/python -m pytest tests/test_pg_integration.py -q
"""

from __future__ import annotations

import os
from collections import Counter
from uuid import uuid4

import pytest

from bot.core.config_manager import Config
from bot.core.telegram_manager import TgClient
from bot.helper.ext_utils.copy_records import MAX_TASK_RECORDS
from bot.helper.ext_utils.db_handler import DbManager

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
    units = [
        {"mode": "single", "chat": -1001, "msg": 7, "media": [{"kind": "photo"}]}
    ]
    await dbm.save_copy_record(-1001, 7, 42, "a folder", units)
    await dbm.save_copy_record(-1001, 8, 42, "another", [{"mode": "single"}])

    found = await dbm.find_copy_records(7)

    assert len(found) == 1
    doc = found[0]
    assert doc["_id"] == "-1001:7"
    assert doc["user"] == 42
    assert doc["name"] == "a folder"
    assert doc["units"] == units


async def test_the_prune_is_per_user_and_spares_others(dbm):
    other_user = 43
    await dbm.save_copy_record(-1001, 0, other_user, "old but theirs", [])

    for mid in range(1, MAX_TASK_RECORDS + 5):
        await dbm.save_copy_record(-1001, mid, 42, f"bulk {mid}", [])

    # user 42 is trimmed to their newest MAX_TASK_RECORDS ...
    cursor = await dbm._conn.execute(
        "SELECT user_id FROM copy_records WHERE bot_id = %s", (dbm._bot,)
    )
    counts = Counter(row["user_id"] for row in await cursor.fetchall())
    assert counts[42] == MAX_TASK_RECORDS
    # ... while the flood never touched user 43's single record
    assert counts[other_user] == 1
    # their record -- the only one with mid 0 -- is still findable
    assert [d["user"] for d in await dbm.find_copy_records(0)] == [other_user]


async def test_disconnect_after_connect_returns_to_noop(dbm):
    await dbm.disconnect()
    assert not dbm.is_connected
    await dbm.save_blob("a", b"b", bot_id=dbm._bot)
    assert await dbm.get_blob("a", bot_id=dbm._bot) is None
