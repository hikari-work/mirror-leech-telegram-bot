"""Hermetic tests for every DbManager method, pinned at the SQL boundary.

Each test stands in for the connection with a recorder (monkeypatching the
``_execute``/``_fetchone``/``_fetchall`` seam) and asserts two things: the SQL
a method chose -- full-column replace vs ``||`` jsonb merge, which table, which
guard -- and the parameters it sent. Row fixtures are returned exactly as
``dict_row`` shapes them. Behavioural round trips that need a real store live
in ``test_pg_integration.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Jsonb

from bot import aria2_options, qbit_options, rss_dict, user_data
from bot.core.telegram_manager import TgClient
from bot.helper.ext_utils.db_handler import DbManager, blob_box


class _Recorder:
    """The hermetic seam: records every query, answers from scripted rows."""

    def __init__(self, *results):
        self._results = list(results)
        self.writes = []  # (sql, params) of every _execute
        self.reads = []  # (sql, params) of every _fetchone/_fetchall

    async def execute(self, sql, params=()):
        # the real connection adapts a Jsonb param into the column value; the
        # recorder unwraps it so tests assert on the stored python value
        self.writes.append((sql, tuple(_plain(p) for p in params)))

    async def fetchone(self, sql, params=()):
        self.reads.append((sql, params))
        return self._results.pop(0) if self._results else None

    async def fetchall(self, sql, params=()):
        self.reads.append((sql, params))
        return self._results.pop(0) if self._results else []


def _plain(value):
    """A param as the store keeps it: a Jsonb wrapper reduced to its payload."""
    return value.obj if isinstance(value, Jsonb) else value


@pytest.fixture
def dbm(monkeypatch):
    """A DbManager wired to a fresh recorder, bot id pinned and deterministic.

    ``TgClient.ID`` is read live at call time by methods that take no
    ``bot_id`` (tasks, copy records), so it is monkeypatched to a constant.
    """
    monkeypatch.setattr(TgClient, "ID", "999")
    manager = DbManager()
    manager._return = False
    recorder = _Recorder()
    manager._execute = recorder.execute
    manager._fetchone = recorder.fetchone
    manager._fetchall = recorder.fetchall
    manager._recorder = recorder
    return manager


@pytest.fixture(autouse=True)
def _clean_globals():
    """Isolate each test from the shared in-memory option/user/feed dicts."""
    user_data.clear()
    rss_dict.clear()
    aria2_options.clear()
    qbit_options.clear()


# ── blobs ──────────────────────────────────────────────────────────────


async def test_save_blob_upserts_one_revision_per_name(dbm):
    await dbm.save_blob("thumbnails/1.jpg", b"jpeg", bot_id="123")

    sql, (name, data) = dbm._recorder.writes[0]
    assert "INSERT INTO blobs" in sql
    assert "ON CONFLICT (name) DO UPDATE" in sql
    assert name == "123/thumbnails/1.jpg"
    assert blob_box.decrypt(data) == b"jpeg"


async def test_get_blob_reads_by_namespaced_name(dbm):
    dbm._recorder._results = [{"data": blob_box.encrypt(b"secret")}]

    got = await dbm.get_blob("cfg.zip", bot_id="123")

    assert got == b"secret"
    sql, (name,) = dbm._recorder.reads[0]
    assert "SELECT data FROM blobs WHERE name = %s" in sql
    assert name == "123/cfg.zip"


async def test_get_blob_missing_returns_none(dbm):
    assert await dbm.get_blob("nope", bot_id="123") is None


async def test_list_blobs_strips_the_bot_namespace_and_sorts(dbm):
    dbm._recorder._results = [
        [{"name": "123/thumbnails/b.jpg"}, {"name": "123/thumbnails/a.jpg"}],
    ]

    found = await dbm.list_blobs("thumbnails/", bot_id="123")

    assert found == ["a.jpg", "b.jpg"]
    sql, params = dbm._recorder.reads[0]
    assert "substr(name, 1, length(%s)) = %s" in sql
    assert params == ("123/thumbnails/", "123/thumbnails/")


async def test_delete_blob_targets_one_name(dbm):
    await dbm.delete_blob("users/5/THUMBNAIL", bot_id="123")

    sql, (name,) = dbm._recorder.writes[0]
    assert "DELETE FROM blobs WHERE name = %s" in sql
    assert name == "123/users/5/THUMBNAIL"


async def test_update_private_file_deletes_when_file_is_gone(dbm, monkeypatch):
    import bot.helper.ext_utils.db_handler as handler

    monkeypatch.setattr(
        handler, "aiopath", SimpleNamespace(exists=AsyncMock(return_value=False))
    )

    await dbm.update_private_file("cookies.txt")

    sql, (name,) = dbm._recorder.writes[0]
    assert "DELETE FROM blobs" in sql
    assert name.endswith("cookies.txt")


# ── settings: config / deploy / aria2 / qbit ──────────────────────────


@pytest.mark.parametrize(
    ("reader", "table"),
    [
        (DbManager.read_config, "settings_config"),
        (DbManager.read_deploy, "settings_deploy"),
        (DbManager.read_aria2, "settings_aria2"),
        (DbManager.read_qbit, "settings_qbit"),
    ],
)
async def test_read_setting_returns_row_data_or_none(dbm, reader, table):
    dbm._recorder._results = [{"data": {"LEECH_SPLIT_SIZE": 1}}]

    assert await reader(dbm, bot_id="123") == {"LEECH_SPLIT_SIZE": 1}
    sql, (bot,) = dbm._recorder.reads[0]
    assert f"SELECT data FROM {table} WHERE bot_id = %s" in sql
    assert bot == "123"

    dbm._recorder._results = [None]
    assert await reader(dbm, bot_id="123") is None


@pytest.mark.parametrize(
    "writer",
    [
        DbManager.replace_config,
        DbManager.replace_deploy,
    ],
)
async def test_replace_setting_is_a_whole_document_replace(dbm, writer):
    await writer(dbm, {"LEECH_SPLIT_SIZE": 1}, bot_id="123")

    sql, (bot, data) = dbm._recorder.writes[0]
    assert "ON CONFLICT (bot_id) DO UPDATE SET data = EXCLUDED.data" in sql
    assert "||" not in sql
    assert bot == "123"
    assert data == {"LEECH_SPLIT_SIZE": 1}


async def test_update_config_merges_not_replaces(dbm):
    await dbm.update_config({"LEECH_SPLIT_SIZE": 1}, bot_id="123")

    sql, (bot, data) = dbm._recorder.writes[0]
    assert "settings_config.data || EXCLUDED.data" in sql
    assert bot == "123"
    assert data == {"LEECH_SPLIT_SIZE": 1}


@pytest.mark.parametrize(
    ("updater", "table", "key"),
    [
        (DbManager.update_aria2, "settings_aria2", "seed-ratio"),
        (DbManager.update_qbittorrent, "settings_qbit", "max_active_downloads"),
    ],
)
async def test_update_option_merges_one_key(dbm, updater, table, key):
    await updater(dbm, key, "0.5", bot_id="123")

    sql, (bot, data) = dbm._recorder.writes[0]
    assert f"{table}.data || EXCLUDED.data" in sql
    assert bot == "123"
    assert data == {key: "0.5"}


async def test_save_option_settings_merge_the_current_option_dict(dbm):
    qbit_options.update({"max_active_downloads": 4})
    aria2_options.update({"seed-ratio": "1.0"})

    await dbm.save_qbit_settings(bot_id="123")
    await dbm.save_aria2_settings(bot_id="123")

    _, (_, qbit) = dbm._recorder.writes[0]
    _, (_, aria) = dbm._recorder.writes[1]
    assert qbit == {"max_active_downloads": 4}
    assert aria == {"seed-ratio": "1.0"}


# ── users (global, no bot_id) ─────────────────────────────────────────


async def test_save_user_row_replaces_a_global_document(dbm):
    await dbm.save_user_row(5, {"THUMBNAIL": "thumbnails/5.jpg"})

    sql, params = dbm._recorder.writes[0]
    assert "INSERT INTO users (user_id, data)" in sql
    assert "data = EXCLUDED.data" in sql
    # no bot_id column: the tuple is exactly (user_id, data)
    assert params == (5, {"THUMBNAIL": "thumbnails/5.jpg"})


async def test_read_user_rows_returns_user_id_pairs(dbm):
    dbm._recorder._results = [
        [{"user_id": 5, "data": {"a": 1}}, {"user_id": 7, "data": {"b": 2}}],
    ]

    rows = await dbm.read_user_rows()

    assert rows == [(5, {"a": 1}), (7, {"b": 2})]
    assert "FROM users ORDER BY user_id" in dbm._recorder.reads[0][0]


async def test_update_user_data_strips_doc_keys_then_saves(dbm):
    user_data[5] = {"THUMBNAIL": "thumbnails/5.jpg", "AS_DOCUMENT": True}

    await dbm.update_user_data(5)

    sql, (user_id, data) = dbm._recorder.writes[0]
    assert "INSERT INTO users" in sql
    assert user_id == 5
    # the thumbnail path lives in the blobs table, never in the row
    assert data == {"AS_DOCUMENT": True}


async def test_update_user_doc_saves_or_deletes_the_blob(dbm):
    await dbm.update_user_doc(5, "THUMBNAIL")
    sql, (name,) = dbm._recorder.writes[0]
    assert "DELETE FROM blobs" in sql
    assert name.endswith("users/5/THUMBNAIL")


async def test_update_user_doc_with_a_path_writes_the_blob(dbm, monkeypatch):
    import bot.helper.ext_utils.db_handler as handler

    class _FakeOpen:
        def __init__(self, content):
            self._content = content

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):
            return self._content

    monkeypatch.setattr(
        handler, "aiopath", SimpleNamespace(exists=AsyncMock(return_value=True))
    )
    monkeypatch.setattr(handler, "aiopen", lambda *_a, **_k: _FakeOpen(b"pixels"))

    await dbm.update_user_doc(5, "THUMBNAIL", path="/tmp/5.jpg")

    sql, (name, data) = dbm._recorder.writes[0]
    assert "INSERT INTO blobs" in sql
    assert name.endswith("users/5/THUMBNAIL")
    assert data == b"pixels"


# ── rss ────────────────────────────────────────────────────────────────


async def test_read_rss_rows_returns_feed_pairs(dbm):
    dbm._recorder._results = [
        [{"user_id": 1, "data": {"a": "feed"}}, {"user_id": 2, "data": {}}],
    ]

    rows = await dbm.read_rss_rows(bot_id="123")

    assert rows == [(1, {"a": "feed"}), (2, {})]
    sql, (bot,) = dbm._recorder.reads[0]
    assert "FROM rss WHERE bot_id = %s ORDER BY user_id" in sql
    assert bot == "123"


async def test_rss_update_writes_one_users_feeds(dbm):
    rss_dict[7] = {"https://x/rss": {"title": "t"}}

    await dbm.rss_update(7, bot_id="123")

    sql, params = dbm._recorder.writes[0]
    assert "INSERT INTO rss (bot_id, user_id, data)" in sql
    assert params == ("123", 7, {"https://x/rss": {"title": "t"}})


async def test_rss_update_all_writes_every_user(dbm):
    rss_dict.update({1: {"one": 1}, 2: {"two": 2}})

    await dbm.rss_update_all(bot_id="123")

    written = {params[1]: params[2] for _, params in dbm._recorder.writes}
    assert written == {1: {"one": 1}, 2: {"two": 2}}
    assert {params[0] for _, params in dbm._recorder.writes} == {"123"}


async def test_rss_delete_targets_one_user_of_one_bot(dbm):
    await dbm.rss_delete(7, bot_id="123")

    sql, params = dbm._recorder.writes[0]
    assert "DELETE FROM rss WHERE bot_id = %s AND user_id = %s" in sql
    assert params == ("123", 7)


# ── incomplete tasks ──────────────────────────────────────────────────


async def test_add_incomplete_task_is_insert_or_nothing(dbm):
    await dbm.add_incomplete_task(-100, "magnet:x", "tag")

    sql, params = dbm._recorder.writes[0]
    assert "ON CONFLICT (bot_id, link) DO NOTHING" in sql
    assert params == (TgClient.ID, "magnet:x", -100, "tag")


async def test_rm_complete_task_deletes_one_link_of_this_bot(dbm):
    await dbm.rm_complete_task("magnet:x")

    sql, params = dbm._recorder.writes[0]
    assert "DELETE FROM incomplete_tasks WHERE bot_id = %s AND link = %s" in sql
    assert params == (TgClient.ID, "magnet:x")


async def test_get_incomplete_tasks_groups_then_forgets(dbm):
    dbm._recorder._results = [
        [
            {"cid": -100, "tag": "t", "link": "a"},
            {"cid": -100, "tag": "t", "link": "b"},
            {"cid": -100, "tag": "u", "link": "c"},
            {"cid": -200, "tag": "t", "link": "d"},
        ],
    ]

    got = await dbm.get_incomplete_tasks()

    assert got == {-100: {"t": ["a", "b"], "u": ["c"]}, -200: {"t": ["d"]}}
    # the notifier runs once per restart: the read is followed by a wipe
    delete_sql, _ = dbm._recorder.writes[0]
    assert "DELETE FROM incomplete_tasks WHERE bot_id = %s" in delete_sql


@pytest.mark.parametrize(
    ("name", "expected_table"),
    [("rss", "rss"), ("tasks", "incomplete_tasks"), ("unknown", None)],
)
async def test_trunc_table_maps_to_the_owned_table(dbm, name, expected_table):
    await dbm.trunc_table(name)

    if expected_table is None:
        assert dbm._recorder.writes == []
    else:
        sql, params = dbm._recorder.writes[0]
        assert f"DELETE FROM {expected_table} WHERE bot_id = %s" in sql
        assert params == (TgClient.ID,)


# ── the disconnected guard ────────────────────────────────────────────


async def test_every_method_is_a_noop_when_disconnected():
    """A fresh DbManager has no connection; nothing may touch the network."""
    dbm = DbManager()

    async def _exercise():
        await dbm.save_blob("a", b"b")
        assert await dbm.get_blob("a") is None
        assert await dbm.list_blobs() == []
        await dbm.delete_blob("a")
        await dbm.update_private_file("a")
        assert await dbm.read_config() is None
        assert await dbm.read_deploy() is None
        assert await dbm.read_aria2() is None
        assert await dbm.read_qbit() is None
        await dbm.replace_config({})
        await dbm.replace_deploy({})
        await dbm.update_deploy_config()
        await dbm.update_config({})
        await dbm.update_aria2("a", 1)
        await dbm.update_qbittorrent("a", 1)
        await dbm.save_qbit_settings()
        await dbm.save_aria2_settings()
        await dbm.save_user_row(1, {})
        assert await dbm.read_user_rows() == []
        await dbm.update_user_data(1)
        await dbm.update_user_doc(1, "THUMBNAIL")
        assert await dbm.read_rss_rows() == []
        await dbm.rss_update_all()
        await dbm.rss_update(1)
        await dbm.rss_delete(1)
        await dbm.add_incomplete_task(1, "a", "t")
        await dbm.rm_complete_task("a")
        assert await dbm.get_incomplete_tasks() == {}
        await dbm.trunc_table("rss")

    await _exercise()
