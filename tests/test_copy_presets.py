"""Tests for named copy presets and the ``-c`` flag that selects one.

Two halves, because the feature has two halves. The rules in
``ext_utils/copy_presets`` are shared by the ``/usetting`` editor that stores a
preset and the resolver that reads one back, so they are pinned on their own.
``_resolve_copy_preset`` is the other half: it turns a name into destinations and
refuses the task when the bot cannot post to one of them.

The refusal is the point of the whole check. Verifying at the moment the copy is
sent would mean discovering a bad chat after a finished download, so the
destinations are settled before ``before_start`` returns and every failure --
including "could not ask telegram" -- stops the task. Unlike the upload
destination there is no degraded mode to fall back on: a copy target has no
second session to try.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bot.helper.task_config.settings_resolver as sr
from bot.helper.ext_utils.copy_presets import (
    MAX_DESTS,
    MAX_PRESETS,
    additions_to,
    parse_destinations,
    presets_of,
    valid_name,
)
from bot.helper.task_config.settings_resolver import SettingsResolverMixin

DEST = -1001234567890
OTHER = -1009876543210
USER = 42


# ── the rules the menu and the resolver share ───────────────────────


@pytest.mark.parametrize("name", ["anime", "a", "Raw_01", "with-dash", "x" * 24])
def test_a_usable_preset_name_is_accepted(name):
    assert valid_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("two words", id="space_would_break_callback_data"),
        pytest.param("x" * 25, id="too_long_for_callback_data"),
        pytest.param("dots.and|pipes", id="punctuation"),
        pytest.param("emoji🙂", id="non_ascii"),
    ],
)
def test_an_unusable_preset_name_is_refused(name):
    """A name travels through callback data and through ``-c``: no whitespace."""
    assert valid_name(name) is False


def test_presets_read_back_as_a_mapping():
    assert presets_of({"COPY_PRESETS": {"a": ["pm"]}}) == {"a": ["pm"]}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({}, id="never_set"),
        pytest.param({"COPY_PRESETS": ""}, id="removed_leaves_an_empty_string"),
        pytest.param({"COPY_PRESETS": None}, id="explicit_none"),
    ],
)
def test_no_presets_reads_back_as_an_empty_mapping(stored):
    """Callers index the result, so "nothing saved" must still be a mapping."""
    assert presets_of(stored) == {}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"{DEST}|12\n{OTHER}", id="newlines"),
        pytest.param(f"{DEST}|12, {OTHER}", id="commas"),
        pytest.param(f"{DEST}|12 {OTHER}", id="spaces"),
        pytest.param(f"  {DEST}|12 ,\n\n {OTHER}  ", id="mixed_and_padded"),
    ],
)
def test_destinations_are_read_however_they_were_separated(text):
    """Users paste these a group at a time; no shape contains any separator."""
    assert parse_destinations(text) == ([f"{DEST}|12", str(OTHER)], "")


def test_a_repeated_destination_is_only_kept_once():
    found, error = parse_destinations(f"{DEST} {DEST}")

    assert found == [str(DEST)]
    assert error == ""


@pytest.mark.parametrize(
    "text, complaint",
    [
        pytest.param(f"{DEST}|abc", "not a thread id", id="thread_is_not_a_number"),
        pytest.param(f"{DEST}|1|2", "more than one", id="two_pipes"),
        pytest.param("|12", "missing the chat", id="no_chat"),
        pytest.param("some-group", "not a chat id", id="not_an_id_or_username"),
        pytest.param("   ", "No destination", id="nothing_at_all"),
    ],
)
def test_a_malformed_destination_is_reported_not_stored(text, complaint):
    found, error = parse_destinations(text)

    assert found == []
    assert complaint in error


@pytest.mark.parametrize("entry", ["pm", "PM", "@named", "@named|8"])
def test_the_shapes_the_dump_resolver_understands_are_allowed(entry):
    assert parse_destinations(entry) == ([entry], "")


def test_new_destinations_fit_while_there_is_room():
    fresh, error = additions_to(["pm"], [str(DEST), str(OTHER)])

    assert fresh == [str(DEST), str(OTHER)]
    assert error == ""


def test_a_destination_already_in_the_preset_is_not_added_twice():
    """Re-sending a list with one new entry should add the one new entry."""
    fresh, error = additions_to(["pm", str(DEST)], ["pm", str(OTHER)])

    assert fresh == [str(OTHER)]
    assert error == ""


def test_more_destinations_than_fit_are_refused_whole():
    existing = [str(n) for n in range(MAX_DESTS - 1)]

    fresh, error = additions_to(existing, ["pm", str(DEST)])

    assert fresh == []
    assert "room for 1" in error


# ── fakes for the resolver half ─────────────────────────────────────


def _chat(chat_id=DEST, kind="CHANNEL", is_admin=True):
    return SimpleNamespace(
        id=chat_id, type=SimpleNamespace(name=kind), is_admin=is_admin
    )


def _member(can_manage=True, can_delete=True):
    return SimpleNamespace(
        privileges=SimpleNamespace(
            can_manage_chat=can_manage, can_delete_messages=can_delete
        )
    )


class Listener(SettingsResolverMixin):
    """The slice of ``TaskConfig`` state ``_resolve_copy_preset`` touches."""

    def __init__(self, **overrides):
        self.user_id = USER
        self.user_dict = {}
        self.client = SimpleNamespace(me=SimpleNamespace(id=1))
        self.copy_preset = ""
        self.clone_dump_chats = {}
        self.__dict__.update(overrides)


@pytest.fixture
def dest(monkeypatch):
    """Script the destination lookups and record which client did the asking."""

    calls = []

    async def get_dest_chat(client, chat_id):
        await asyncio.sleep(0)
        calls.append(("chat", client, chat_id))
        answer = state["chat"].pop(0) if state["chat"] else _chat(chat_id)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def get_dest_member(client, chat_id, user_id):
        await asyncio.sleep(0)
        calls.append(("member", client, chat_id))
        answer = state["member"].pop(0) if state["member"] else _member()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def can_reach_dest(client, chat_id):
        await asyncio.sleep(0)
        calls.append(("reach", client, chat_id))
        answer = state["reach"].pop(0) if state["reach"] else True
        if isinstance(answer, BaseException):
            raise answer
        return answer

    state = {"chat": [], "member": [], "reach": [], "calls": calls}
    monkeypatch.setattr(sr, "get_dest_chat", get_dest_chat)
    monkeypatch.setattr(sr, "get_dest_member", get_dest_member)
    monkeypatch.setattr(sr, "can_reach_dest", can_reach_dest)
    monkeypatch.setattr(
        sr.TgClient, "bot", SimpleNamespace(me=SimpleNamespace(id=7)), raising=False
    )
    return state


def _with_preset(name="anime", entries=(f"{DEST}|12",), **overrides):
    return Listener(
        copy_preset=name,
        user_dict={"COPY_PRESETS": {"anime": list(entries)}},
        **overrides,
    )


# ── naming a preset ─────────────────────────────────────────────────


async def test_a_task_without_the_flag_is_left_alone(dest):
    listener = Listener(clone_dump_chats={(DEST, None): {"last_sent_msg": None}})

    await listener._resolve_copy_preset()

    assert listener.clone_dump_chats == {(DEST, None): {"last_sent_msg": None}}
    assert dest["calls"] == []


async def test_an_unknown_preset_name_fails_the_task(dest):
    listener = _with_preset(name="absent")

    with pytest.raises(ValueError, match="no copy preset named 'absent'"):
        await listener._resolve_copy_preset()


async def test_an_unknown_name_is_answered_with_the_names_that_do_exist(dest):
    listener = Listener(
        copy_preset="absent",
        user_dict={"COPY_PRESETS": {"anime": ["pm"], "music": ["pm"]}},
    )

    with pytest.raises(ValueError, match="anime, music"):
        await listener._resolve_copy_preset()


async def test_a_preset_with_no_destinations_fails_the_task(dest):
    """Nowhere to copy to is a mistake, not a task that quietly copies nothing."""
    listener = _with_preset(entries=())

    with pytest.raises(ValueError, match="no destinations"):
        await listener._resolve_copy_preset()


async def test_nothing_is_looked_up_before_the_name_is_known(dest):
    listener = _with_preset(name="absent")

    with pytest.raises(ValueError):
        await listener._resolve_copy_preset()

    assert dest["calls"] == []


# ── what the preset becomes ─────────────────────────────────────────


async def test_a_preset_becomes_the_tasks_copy_destinations(dest):
    listener = _with_preset(entries=(f"{DEST}|12", "pm"))

    await listener._resolve_copy_preset()

    assert set(listener.clone_dump_chats) == {(DEST, 12), (USER, None)}


async def test_a_preset_replaces_the_standing_clone_dump_chats(dest):
    """Naming a preset says where *this* task goes; the default tagging along
    would be a surprise."""
    listener = _with_preset(
        entries=(str(OTHER),),
        clone_dump_chats={(DEST, None): {"last_sent_msg": None}},
    )

    await listener._resolve_copy_preset()

    assert set(listener.clone_dump_chats) == {(OTHER, None)}


async def test_two_topics_of_one_group_are_two_destinations(dest):
    """The headline use case: keying by chat alone would keep only one of them."""
    listener = _with_preset(entries=(f"{DEST}|12", f"{DEST}|34"))

    await listener._resolve_copy_preset()

    assert set(listener.clone_dump_chats) == {(DEST, 12), (DEST, 34)}


async def test_every_destination_starts_with_nothing_sent_to_it(dest):
    listener = _with_preset()

    await listener._resolve_copy_preset()

    assert listener.clone_dump_chats[(DEST, 12)] == {"last_sent_msg": None}


# ── the bot's rights decide, and they decide before the download ────


async def test_the_bot_is_the_account_that_gets_checked(dest):
    """The copies are sent with ``TgClient.bot``, so its rights are the ones
    that matter -- even on a task the user session uploads."""
    listener = _with_preset()

    await listener._resolve_copy_preset()

    asked = [client for _, client, _ in dest["calls"]]
    assert asked and all(client is sr.TgClient.bot for client in asked)
    assert listener.client not in asked


async def test_a_missing_destination_fails_the_task_by_name(dest):
    listener = _with_preset(entries=(str(DEST), f"{OTHER}|9"))
    dest["chat"] = [_chat(DEST), None]

    with pytest.raises(ValueError, match=f"{OTHER}\\|9 was not found"):
        await listener._resolve_copy_preset()


async def test_a_destination_the_bot_is_not_admin_in_fails_the_task(dest):
    listener = _with_preset()
    dest["chat"] = [_chat(DEST, is_admin=False)]

    with pytest.raises(ValueError, match=f"not admin in copy destination {DEST}"):
        await listener._resolve_copy_preset()


async def test_a_destination_the_bot_lacks_privileges_in_fails_the_task(dest):
    listener = _with_preset()
    dest["member"] = [_member(can_delete=False)]

    with pytest.raises(ValueError, match="Not enough privileges"):
        await listener._resolve_copy_preset()


async def test_a_private_destination_that_never_started_the_bot_fails(dest):
    listener = _with_preset(entries=("pm",))
    dest["chat"] = [_chat(USER, kind="PRIVATE")]
    dest["reach"] = [False]

    with pytest.raises(ValueError, match="has not started the bot"):
        await listener._resolve_copy_preset()


@pytest.mark.parametrize(
    "scripted",
    [
        pytest.param({"chat": [sr.ChatLookupError("rate limited")]}, id="chat_lookup"),
        pytest.param(
            {"member": [sr.ChatLookupError("rate limited")]}, id="member_lookup"
        ),
    ],
)
async def test_a_check_that_could_not_be_made_fails_the_task_too(dest, scripted):
    """No degraded mode exists here: a copy target has no second session, so
    "could not ask" cannot be read as "probably fine"."""
    listener = _with_preset()
    dest.update(scripted)

    with pytest.raises(ValueError, match="Try again in a moment"):
        await listener._resolve_copy_preset()


async def test_a_failed_destination_leaves_the_earlier_ones_unused(dest):
    """The task is refused, so a half-built destination map must not survive."""
    listener = _with_preset(entries=(str(DEST), str(OTHER)))
    dest["chat"] = [_chat(DEST), None]

    with pytest.raises(ValueError):
        await listener._resolve_copy_preset()

    assert listener.clone_dump_chats == {}


# ── the limits the editor enforces ──────────────────────────────────


def test_the_advertised_limits_are_five_each():
    """The user asked for "up to 5", applied to presets and to destinations."""
    assert (MAX_PRESETS, MAX_DESTS) == (5, 5)
