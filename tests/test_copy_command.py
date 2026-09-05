"""Tests for /copy: the command that replays a finished task.

The prompt half and the button half are exercised through the module with
its bot-side dependencies stubbed, the way ``test_bypass_command`` drives
/bypass -- the command's job is mostly what it refuses (no database, no
record, no presets, not your button) and what it offers instead.
``copy_unit`` is pinned on its own below that: the primary path by message
coordinates, the ``file_id`` fallback when the message is gone, and the
invariant that one destination's failure never stops another's.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pyrogram.errors import FloodWait

from bot.helper.storage.copy_records import copy_unit

_ROOT = Path(__file__).resolve().parent.parent
DEST = -1001234567890
OTHER = -1009876543210
USER = 42
CHATTER = 43
CHAT = -100555


class _Pacer:
    """The stand-in FloodPacer: no pacing, one retry for a flood."""

    def __init__(self, is_cancelled=None):
        self._is_cancelled = is_cancelled or (lambda: False)

    async def guard(self, func, *args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except FloodWait:
            return await func(*args, **kwargs)


@pytest.fixture
def copy_mod(monkeypatch):
    """Import ``bot.modules.copy`` with its bot-side dependencies stubbed.

    Returns (module, stubs) where ``stubs`` exposes the doubles the command
    talks to, so a test can assert on what the user was actually shown.
    """
    fake_bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(id=900)),
        copy_media_group=AsyncMock(
            return_value=[SimpleNamespace(id=901), SimpleNamespace(id=902)]
        ),
        send_document=AsyncMock(return_value=SimpleNamespace(id=903)),
        send_video=AsyncMock(return_value=SimpleNamespace(id=904)),
        send_audio=AsyncMock(return_value=SimpleNamespace(id=905)),
        send_photo=AsyncMock(return_value=SimpleNamespace(id=906)),
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(id=907), SimpleNamespace(id=908)]
        ),
    )
    stubs = SimpleNamespace(
        bot=fake_bot,
        user_data={},
        send_message=AsyncMock(
            return_value=SimpleNamespace(id=77, chat=SimpleNamespace(id=CHAT))
        ),
        edit_message=AsyncMock(),
        auto_delete=AsyncMock(),
        database=SimpleNamespace(find_copy_records=AsyncMock(return_value=[])),
    )

    def _pkg(name, path=None):
        mod = ModuleType(name)
        mod.__path__ = [] if path is None else [path]
        return mod

    bot_pkg = _pkg("bot", str(_ROOT / "bot"))
    bot_pkg.user_data = stubs.user_data
    # dest_chat logs through the bot package's LOGGER when a check misfires.
    bot_pkg.LOGGER = SimpleNamespace(
        info=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )

    config = SimpleNamespace(DATABASE_URL="mongodb://test")
    config_mod = ModuleType("bot.core.config_manager")
    config_mod.Config = config
    bot_utils = ModuleType("bot.helper.util.bot_utils")
    # The real decorator hands the coroutine to bot_loop.create_task; the
    # test awaits the command directly instead.
    bot_utils.new_task = lambda func: func

    db_handler = ModuleType("bot.helper.storage.db_handler")
    db_handler.database = stubs.database

    tg_manager = ModuleType("bot.core.telegram_manager")
    tg_manager.TgClient = SimpleNamespace(bot=stubs.bot)
    tg_manager.own_account = lambda _bot: SimpleNamespace(id=1)

    message_utils = ModuleType("bot.helper.telegram.message_utils")
    message_utils.send_message = stubs.send_message
    message_utils.edit_message = stubs.edit_message
    message_utils.auto_delete_message = stubs.auto_delete
    message_utils.chat_of = lambda message: message.chat

    flood_mod = ModuleType("bot.helper.upload.flood_pacer")
    flood_mod.FloodPacer = _Pacer

    modules_pkg = _pkg("bot.modules", str(_ROOT / "bot" / "modules"))

    for name, mod in {
        "bot": bot_pkg,
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": config_mod,
        "bot.core.telegram_manager": tg_manager,
        "bot.helper": _pkg("bot.helper"),
        # Real path: ``copy_presets`` and ``copy_records`` import nothing
        # stubbed, so the rules under test are the real ones.
        "bot.helper.util": _pkg("bot.helper.util"),
        "bot.helper.storage": _pkg(
            "bot.helper.storage", str(_ROOT / "bot" / "helper" / "storage")
        ),
        "bot.helper.util.bot_utils": bot_utils,
        "bot.helper.storage.db_handler": db_handler,
        "bot.helper.upload": _pkg("bot.helper.upload"),
        "bot.helper.upload.flood_pacer": flood_mod,
        "bot.helper.telegram": _pkg(
            "bot.helper.telegram",
            str(_ROOT / "bot" / "helper" / "telegram"),
        ),
        "bot.helper.telegram.message_utils": message_utils,
        "bot.modules": modules_pkg,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("bot.modules.copy", None)
    sys.modules.pop("bot.helper.telegram.dest_chat", None)
    sys.modules.pop("bot.helper.telegram.button_build", None)
    module = importlib.import_module("bot.modules.copy")
    # The checks and sends go through doubles, not telegram.
    monkeypatch.setattr(
        module, "verify_copy_target", AsyncMock(), raising=False
    )
    stubs.config = module.Config
    return module, stubs


def _message(text, user=USER, chat=CHAT):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user),
        chat=SimpleNamespace(id=chat),
    )


def _prompt(stubs, id_=77):
    """The prompt message send_message answers with (id 77 unless changed)."""
    prompt = SimpleNamespace(id=id_, chat=SimpleNamespace(id=CHAT))
    stubs.send_message.return_value = prompt
    return prompt


def _record(cid=CHAT, mid=123, units=None):
    return {
        "cid": cid,
        "mid": mid,
        "user": USER,
        "name": "Some Task",
        "at": 1,
        "units": units
        or [{"mode": "single", "chat": cid, "msg": 500, "media": [
            {"kind": "document", "file_id": "f1", "caption": "a"}
        ]}],
    }


def _shown(mock):
    """Every message body passed to a send/edit double."""
    return [call.args[1] for call in mock.await_args_list]


def _keyboard(stubs):
    """The buttons of the one message the command sent."""
    call = stubs.send_message.await_args
    return call.args[2].inline_keyboard


def _query(data, user=USER, prompt_id=77):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user),
        message=SimpleNamespace(id=prompt_id, chat=SimpleNamespace(id=CHAT)),
        answer=AsyncMock(),
    )


# ── the command: what it refuses, what it offers ────────────────────


async def test_no_argument_is_answered_with_usage(copy_mod):
    module, stubs = copy_mod

    await module.copy_task(None, _message("/copy"))
    await module.copy_task(None, _message("/copy 123 456"))

    assert "task_id" in _shown(stubs.send_message)[0]
    stubs.database.find_copy_records.assert_not_awaited()


async def test_no_database_means_no_copy_feature(copy_mod):
    module, stubs = copy_mod
    module.Config.DATABASE_URL = ""

    await module.copy_task(None, _message("/copy 123"))

    assert "database" in _shown(stubs.send_message)[0]
    stubs.database.find_copy_records.assert_not_awaited()


async def test_an_unknown_task_id_has_no_record(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = []

    await module.copy_task(None, _message("/copy 999999"))

    assert "No record" in _shown(stubs.send_message)[0]
    stubs.send_message.assert_awaited_once()


async def test_a_bare_id_is_ambiguous_across_chats_until_spelled_out(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [
        _record(cid=CHAT, mid=123),
        _record(cid=OTHER, mid=123),
    ]

    # Run from a third chat: both candidates are equally foreign, so the
    # command refuses and spells out the chat-qualified forms.
    await module.copy_task(None, _message("/copy 123", chat=-100777))

    body = _shown(stubs.send_message)[0]
    assert "several chats" in body
    assert f"{CHAT}:123" in body and f"{OTHER}:123" in body

    # The chat-qualified form names its chat and needs no disambiguation.
    stubs.send_message.reset_mock()
    await module.copy_task(None, _message(f"/copy {OTHER}:123", chat=-100777))
    assert "several chats" not in _shown(stubs.send_message)[0]


async def test_a_bare_id_from_its_own_chat_is_not_ambiguous(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [
        _record(cid=CHAT, mid=123),
        _record(cid=OTHER, mid=123),
    ]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}

    await module.copy_task(None, _message("/copy 123"))
    # The record of this chat was the obvious pick, so the keyboard came up.
    stubs.send_message.assert_awaited_once()
    assert module.pending[(CHAT, _prompt(stubs).id)]["record"]["cid"] == CHAT


async def test_no_presets_points_at_the_settings_menu(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]

    await module.copy_task(None, _message("/copy 123"))

    assert "no copy presets" in _shown(stubs.send_message)[0]
    # A keyboard would be a choice between zero things.
    assert len(stubs.send_message.await_args.args) == 2


async def test_one_button_per_preset_and_a_cancel(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {
        "COPY_PRESETS": {"anime": [str(DEST)], "movies": ["pm"], "music": ["@x"]}
    }

    await module.copy_task(None, _message("/copy 123"))

    rows = _keyboard(stubs)
    labels = [b.text for row in rows for b in row]
    assert labels == ["anime", "movies", "music", "Cancel"]
    data = [b.callback_data for row in rows for b in row]
    assert data[:3] == [f"copyt {USER} 0", f"copyt {USER} 1", f"copyt {USER} 2"]
    assert data[3] == f"copyt {USER} x"
    assert all(len(d.encode()) <= 64 for d in data)


async def test_the_prompt_carries_a_snapshot_of_the_presets(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}

    await module.copy_task(None, _message("/copy 123"))
    # The user re-edits their presets after the prompt went up.
    stubs.user_data[USER]["COPY_PRESETS"] = {"movies": [str(OTHER)]}

    await module.copy_choice(None, _query(f"copyt {USER} 0"))

    # Button 0 still means what it said: "anime", the snapshot's first entry.
    sent = stubs.bot.copy_message.await_args_list
    assert sent[0].kwargs["chat_id"] == DEST


# ── the callback: whose button it is, and what a stale one gets ──────


async def test_someone_elses_button_is_refused(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)
    key = (CHAT, prompt.id)

    query = _query(f"copyt {USER} 0", user=CHATTER, prompt_id=prompt.id)
    await module.copy_choice(None, query)

    query.answer.assert_awaited_once_with("Not yours!", show_alert=True)
    stubs.bot.copy_message.assert_not_awaited()
    # Not answered is not consumed: the owner can still press it.
    assert key in module.pending


async def test_cancel_sends_nothing_and_clears_the_prompt(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)

    await module.copy_choice(None, _query(f"copyt {USER} x", prompt_id=prompt.id))

    assert "cancelled" in _shown(stubs.edit_message)[0]
    stubs.bot.copy_message.assert_not_awaited()
    assert module.pending == {}


async def test_an_expired_prompt_says_so(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)
    key = (CHAT, prompt.id)
    module.pending[key]["at"] -= module.PENDING_TTL + 1

    query = _query(f"copyt {USER} 0", prompt_id=prompt.id)
    await module.copy_choice(None, query)

    query.answer.assert_awaited_once()
    assert "Run /copy again" in query.answer.await_args.args[0]
    stubs.bot.copy_message.assert_not_awaited()
    # The stale entry is gone either way -- it cannot be answered later.
    assert key not in module.pending


async def test_a_second_copy_while_one_runs_is_refused(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST)]}}
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)
    module.running.add(USER)

    query = _query(f"copyt {USER} 0", prompt_id=prompt.id)
    await module.copy_choice(None, query)

    query.answer.assert_awaited_once()
    stubs.bot.copy_message.assert_not_awaited()


async def test_an_unwritable_target_stops_everything_before_a_send(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [_record()]
    stubs.user_data[USER] = {
        "COPY_PRESETS": {"anime": [str(DEST), str(OTHER)]}
    }
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)

    async def _fails_only_for_bad_chat(entry, chat_id):
        if chat_id == OTHER:
            raise ValueError(f"Bot is not admin in copy destination {OTHER}!")

    module.verify_copy_target.side_effect = _fails_only_for_bad_chat
    await module.copy_choice(None, _query(f"copyt {USER} 0", prompt_id=prompt.id))

    assert "No copy was sent" in _shown(stubs.edit_message)[0]
    assert str(OTHER) in _shown(stubs.edit_message)[0]
    stubs.bot.copy_message.assert_not_awaited()


async def test_a_successful_copy_summarises_per_destination(copy_mod):
    module, stubs = copy_mod
    stubs.database.find_copy_records.return_value = [
        _record(
            units=[
                {"mode": "single", "chat": CHAT, "msg": 500, "media": [
                    {"kind": "document", "file_id": "f1", "caption": "a"}
                ]},
                {"mode": "single", "chat": CHAT, "msg": 501, "media": [
                    {"kind": "video", "file_id": "f2", "caption": "b"}
                ]},
            ]
        )
    ]
    stubs.user_data[USER] = {"COPY_PRESETS": {"anime": [str(DEST), "pm"]}}
    await module.copy_task(None, _message("/copy 123"))
    prompt = _prompt(stubs)

    await module.copy_choice(None, _query(f"copyt {USER} 0", prompt_id=prompt.id))

    # Every unit reached every target, and the summary says so.
    assert stubs.bot.copy_message.await_count == 4
    summary = _shown(stubs.edit_message)[-1]
    assert f"<code>{DEST}</code>: 2/2 sent" in summary
    assert f"<code>{USER}</code>: 2/2 sent" in summary
    assert module.running == set()
    # Units went out in record order.
    sent = [c.kwargs["message_id"] for c in stubs.bot.copy_message.await_args_list]
    assert sent == [500, 500, 501, 501]


# ── copy_unit: the replay itself ────────────────────────────────────


def _client():
    return SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(id=900)),
        copy_media_group=AsyncMock(
            return_value=[SimpleNamespace(id=901), SimpleNamespace(id=902)]
        ),
        send_document=AsyncMock(return_value=SimpleNamespace(id=903)),
        send_video=AsyncMock(return_value=SimpleNamespace(id=904)),
        send_audio=AsyncMock(return_value=SimpleNamespace(id=905)),
        send_photo=AsyncMock(return_value=SimpleNamespace(id=906)),
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(id=907), SimpleNamespace(id=908)]
        ),
    )


def _targets(*chats):
    return {(chat, None): {"last_sent_msg": None} for chat in chats}


SINGLE = {
    "mode": "single",
    "chat": 1,
    "msg": 500,
    "media": [{"kind": "document", "file_id": "f1", "caption": "a"}],
}
GROUP = {
    "mode": "group",
    "chat": 1,
    "msg": 600,
    "media": [
        {"kind": "photo", "file_id": "g1", "caption": "p"},
        {"kind": "photo", "file_id": "g2", "caption": ""},
    ],
}


async def test_a_single_unit_is_copied_by_its_coordinates():
    client = _client()

    errors = await copy_unit(_Pacer(), _targets(DEST), SINGLE, client)

    assert errors == {}
    client.copy_message.assert_awaited_once_with(
        chat_id=DEST,
        from_chat_id=1,
        message_id=500,
        disable_notification=True,
        message_thread_id=None,
        reply_to_message_id=None,
    )
    client.send_document.assert_not_awaited()


async def test_a_group_unit_is_copied_as_a_media_group():
    client = _client()

    errors = await copy_unit(_Pacer(), _targets(DEST), GROUP, client)

    assert errors == {}
    client.copy_media_group.assert_awaited_once_with(
        chat_id=DEST,
        from_chat_id=1,
        message_id=600,
        disable_notification=True,
        message_thread_id=None,
        reply_to_message_id=None,
    )


async def test_a_gone_message_falls_back_to_its_file_id():
    client = _client()
    client.copy_message.side_effect = Exception("MESSAGE_ID_INVALID")

    errors = await copy_unit(_Pacer(), _targets(DEST), SINGLE, client)

    assert errors == {}
    client.send_document.assert_awaited_once()
    kwargs = client.send_document.await_args.kwargs
    assert kwargs["document"] == "f1"
    assert kwargs["caption"] == "a"
    assert kwargs["chat_id"] == DEST


async def test_a_gone_album_falls_back_to_send_media_group():
    client = _client()
    client.copy_media_group.side_effect = Exception("MESSAGE_ID_INVALID")

    errors = await copy_unit(_Pacer(), _targets(DEST), GROUP, client)

    assert errors == {}
    client.send_media_group.assert_awaited_once()
    media = client.send_media_group.await_args.kwargs["media"]
    assert [m.media for m in media] == ["g1", "g2"]
    assert media[0].caption == "p"
    assert media[1].caption == ""


async def test_both_paths_failing_is_reported_not_hidden():
    client = _client()
    client.copy_message.side_effect = Exception("MESSAGE_ID_INVALID")
    client.send_document.side_effect = Exception("FILE_REFERENCE_EXPIRED")

    errors = await copy_unit(_Pacer(), _targets(DEST), SINGLE, client)

    assert "MESSAGE_ID_INVALID" in errors[(DEST, None)]
    assert "FILE_REFERENCE_EXPIRED" in errors[(DEST, None)]


async def test_one_target_failing_leaves_the_others_alone():
    client = _client()

    async def _copy(**kwargs):
        if kwargs["chat_id"] == OTHER:
            raise Exception("CHAT_WRITE_FORBIDDEN")
        return SimpleNamespace(id=900)

    async def _send_fails(**kwargs):
        raise Exception("FILE_REFERENCE_EXPIRED")

    client.copy_message.side_effect = _copy
    # The fallback failing too is what makes the failure stick; an expired
    # file_id is also the realistic reason a copy of a gone message fails.
    client.send_document.side_effect = _send_fails

    errors = await copy_unit(_Pacer(), _targets(DEST, OTHER), SINGLE, client)

    assert list(errors) == [(OTHER, None)]
    assert "CHAT_WRITE_FORBIDDEN" in errors[(OTHER, None)]
    assert "FILE_REFERENCE_EXPIRED" in errors[(OTHER, None)]
    # The healthy target still got its copy -- one chat's refusal is not
    # the other's problem.
    assert [c.kwargs["chat_id"] for c in client.copy_message.await_args_list] == [
        DEST,
        OTHER,
    ]


async def test_units_chain_onto_what_the_previous_one_sent():
    client = _client()

    targets = _targets(DEST)
    await copy_unit(_Pacer(), targets, SINGLE, client)
    await copy_unit(_Pacer(), targets, GROUP, client)

    # The album copied second answers the single copied first.
    assert client.copy_media_group.await_args.kwargs["reply_to_message_id"] == 900


async def test_a_flood_wait_is_retried_not_fallen_back_from():
    client = _client()

    calls = []

    async def _copy(**kwargs):
        calls.append(kwargs["chat_id"])
        if len(calls) == 1:
            raise FloodWait(42)
        return SimpleNamespace(id=900)

    client.copy_message.side_effect = _copy

    errors = await copy_unit(_Pacer(), _targets(DEST), SINGLE, client)

    assert errors == {}
    assert len(calls) == 2
    client.send_document.assert_not_awaited()
