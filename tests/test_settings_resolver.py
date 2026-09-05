"""Characterization tests for the settings that ``before_start`` resolves.

``before_start()`` used to be one 289-line method: fourteen unrelated decisions
sharing one scope, which is why nothing here could be tested without driving a
whole task. Extracting it into named steps opened those decisions to test, and
this file pins the behaviour that survived the extraction.

Three of the rules are worth naming, because they are not obvious from the code
and a future simplification would quietly break them:

* A key **present** in ``user_dict`` means the user has an opinion about that
  setting -- even an empty one -- so the bot-wide default stops applying.
* The user session is an optimisation: a destination it cannot manage costs
  user transmission and hybrid leech, and never fails the task.
* The bot is a requirement: a destination *it* cannot manage fails the task,
  unless the user session is there to carry the upload instead.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bot.helper.task.settings_resolver as sr
from bot.helper.task.settings_resolver import SettingsResolverMixin

DEST = -1001234567890
USER = 42


# ── fakes ───────────────────────────────────────────────────────────


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
    """The slice of ``TaskConfig`` state that ``before_start`` reads or writes."""

    def __init__(self, **overrides):
        self.user_id = USER
        self.user_dict = {}
        self.client = SimpleNamespace(me=SimpleNamespace(id=1))
        self.up_dest = ""
        self.chat_thread_id = None
        self.name_sub = ""
        self.thumbnail_layout = ""
        self.thumb = None
        self.split_size = 0
        self.max_split_size = 0
        self.clone_dump_chats = {}
        self.excluded_extensions = []
        self.included_extensions = []
        self.ffmpeg_cmds = None
        self.equal_splits = False
        self.user_transmission = False
        self.hybrid_leech = False
        self.as_doc = False
        self.as_med = False
        self.bot_trans = False
        self.user_trans = False
        self.is_super_chat = True
        self.__dict__.update(overrides)


@pytest.fixture
def dest(monkeypatch):
    """Script the destination lookups and record what was asked of whom."""

    calls = []

    async def get_dest_chat(client, chat_id):
        await asyncio.sleep(0)
        calls.append(("chat", client, chat_id))
        answer = state["chat"].pop(0) if state["chat"] else _chat()
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
    monkeypatch.setattr(sr.TgClient, "user", SimpleNamespace(me=SimpleNamespace(id=2)))
    monkeypatch.setattr(sr.TgClient, "IS_PREMIUM_USER", True)
    return state


# ── "the user has an opinion" beats the bot-wide default ────────────


def test_a_user_setting_wins_over_the_bot_default():
    assert sr._setting_for({"K": "mine"}, "K", "global") == "mine"


def test_the_bot_default_applies_when_the_user_never_said():
    assert sr._setting_for({}, "K", "global") == "global"


def test_an_emptied_user_setting_is_an_opinion_not_a_gap():
    """The point of the ``key not in user_dict`` test: an empty value is a no."""
    assert sr._setting_for({"K": ""}, "K", "global", "fallback") == "fallback"


def test_a_flag_the_user_turned_off_stays_off():
    assert sr._is_enabled({"K": False}, "K", True) is False


def test_a_flag_the_user_never_touched_follows_the_bot():
    assert sr._is_enabled({}, "K", True) is True


# ── name substitutions ──────────────────────────────────────────────


def test_rename_rules_split_into_pairs():
    listener = Listener(name_sub="old/new | spaced out/x")

    listener._resolve_name_substitutions()

    assert listener.name_sub == [["old", "new"], ["spaced out", "x"]]


def test_no_rename_rules_stays_falsy():
    listener = Listener()
    listener.user_dict = {"NAME_SUBSTITUTE": ""}

    listener._resolve_name_substitutions()

    assert not listener.name_sub


# ── ffmpeg presets ──────────────────────────────────────────────────


def test_a_literal_ffmpeg_command_passes_straight_through():
    listener = Listener(ffmpeg_cmds=[("-i", "in.mkv")])

    listener._resolve_ffmpeg_commands()

    assert listener.ffmpeg_cmds == ["-i", "in.mkv"]


def test_a_named_preset_is_looked_up_by_key():
    listener = Listener(ffmpeg_cmds=["mine"])
    listener.user_dict = {"FFMPEG_CMDS": {"mine": ["-c copy"]}}

    listener._resolve_ffmpeg_commands()

    assert listener.ffmpeg_cmds == ["-c copy"]


def test_a_preset_placeholder_is_filled_from_the_users_variables():
    listener = Listener(ffmpeg_cmds=["mine"])
    listener.user_dict = {
        "FFMPEG_CMDS": {"mine": ["-crf {q}"]},
        "FFMPEG_VARIABLES": {"mine": {"0": {"q": "18"}}},
    }

    listener._resolve_ffmpeg_commands()

    assert listener.ffmpeg_cmds == ["-crf 18"]


def test_a_half_filled_preset_is_dropped_rather_than_run():
    """A partial fill would hand ffmpeg a command line with braces still in it."""
    listener = Listener(ffmpeg_cmds=["mine"])
    listener.user_dict = {
        "FFMPEG_CMDS": {"mine": ["-crf {q} -vf {scale}"]},
        "FFMPEG_VARIABLES": {"mine": {"0": {"q": "18"}}},
    }

    listener._resolve_ffmpeg_commands()

    assert listener.ffmpeg_cmds == []


def test_an_unknown_preset_name_resolves_to_nothing():
    listener = Listener(ffmpeg_cmds=["absent"])
    listener.user_dict = {"FFMPEG_CMDS": {"mine": ["-c copy"]}}

    listener._resolve_ffmpeg_commands()

    assert listener.ffmpeg_cmds == []


# ── the destination string the user typed ───────────────────────────


@pytest.mark.parametrize(
    "typed, chat, thread",
    [
        ("-1001234567890", -1001234567890, None),
        ("-1001234567890|12", -1001234567890, 12),
        ("pm", USER, None),
        ("@somechannel", "@somechannel", None),
    ],
)
def test_a_typed_destination_reduces_to_a_chat_and_thread(typed, chat, thread):
    listener = Listener(up_dest=typed)

    listener._normalize_up_dest()

    assert listener.up_dest == chat
    assert listener.chat_thread_id == thread


def test_an_already_numeric_destination_is_left_alone():
    listener = Listener(up_dest=DEST)

    listener._normalize_up_dest()

    assert listener.up_dest == DEST


@pytest.mark.parametrize(
    "prefix, user_transmission, hybrid_leech",
    [("b:", False, False), ("u:", True, False), ("h:", True, True)],
)
def test_a_prefix_overrides_which_session_uploads(
    prefix, user_transmission, hybrid_leech, monkeypatch
):
    monkeypatch.setattr(sr.TgClient, "IS_PREMIUM_USER", True)
    listener = Listener(up_dest=f"{prefix}{DEST}", user_transmission=True)

    listener._normalize_up_dest()

    assert listener.up_dest == DEST
    assert listener.user_transmission is user_transmission
    assert listener.hybrid_leech is hybrid_leech


def test_hybrid_leech_needs_a_premium_user_session(monkeypatch):
    monkeypatch.setattr(sr.TgClient, "IS_PREMIUM_USER", False)
    listener = Listener(up_dest=f"h:{DEST}")

    listener._normalize_up_dest()

    assert listener.hybrid_leech is False


# ── the user session is an optimisation, never a requirement ────────


async def test_a_destination_the_user_session_manages_keeps_it(dest):
    listener = Listener(up_dest=DEST, user_transmission=True)

    await listener._verify_dest_for_user_session()

    assert listener.user_transmission is True


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(None, id="chat_not_found"),
        pytest.param(sr.ChatLookupError("rate limited"), id="could_not_ask"),
    ],
)
async def test_a_destination_the_user_session_cannot_see_costs_only_the_session(
    dest, answer
):
    """Even "could not ask" only downgrades here -- the bot can still upload."""
    dest["chat"] = [answer]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_user_session()

    assert listener.user_transmission is False
    assert listener.hybrid_leech is False


async def test_a_private_destination_is_not_for_the_user_session(dest):
    dest["chat"] = [_chat(kind="PRIVATE")]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_user_session()

    assert listener.user_transmission is False


async def test_a_user_session_that_is_not_admin_is_downgraded(dest):
    dest["chat"] = [_chat(is_admin=False)]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_user_session()

    assert listener.user_transmission is False


async def test_a_user_session_short_of_privileges_is_downgraded(dest):
    dest["member"] = [_member(can_delete=False)]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_user_session()

    assert listener.user_transmission is False
    assert listener.hybrid_leech is False


# ── the bot is a requirement ────────────────────────────────────────


async def test_a_destination_the_bot_manages_is_accepted(dest):
    listener = Listener(up_dest=DEST)

    await listener._verify_dest_for_bot()

    assert listener.hybrid_leech is False


async def test_a_missing_destination_fails_a_bot_only_task(dest):
    dest["chat"] = [None]
    listener = Listener(up_dest=DEST)

    with pytest.raises(ValueError, match="Chat not found"):
        await listener._verify_dest_for_bot()


async def test_a_missing_destination_only_costs_hybrid_when_the_session_can_upload(
    dest,
):
    dest["chat"] = [None]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_bot()

    assert listener.hybrid_leech is False
    assert listener.user_transmission is True


async def test_a_bot_that_is_not_admin_fails_the_task(dest):
    dest["chat"] = [_chat(is_admin=False)]
    listener = Listener(up_dest=DEST)

    with pytest.raises(ValueError, match="not admin"):
        await listener._verify_dest_for_bot()


async def test_a_bot_short_of_privileges_fails_a_bot_only_task(dest):
    dest["member"] = [_member(can_manage=False)]
    listener = Listener(up_dest=DEST)

    with pytest.raises(ValueError, match="enough privileges"):
        await listener._verify_dest_for_bot()


async def test_a_bot_short_of_privileges_only_costs_hybrid_with_a_user_session(dest):
    dest["member"] = [_member(can_manage=False)]
    listener = Listener(up_dest=DEST, user_transmission=True, hybrid_leech=True)

    await listener._verify_dest_for_bot()

    assert listener.hybrid_leech is False


async def test_an_unstarted_private_destination_fails_the_task(dest):
    dest["chat"] = [_chat(kind="PRIVATE")]
    dest["reach"] = [False]
    listener = Listener(up_dest=USER)

    with pytest.raises(ValueError, match="Start the bot"):
        await listener._verify_dest_for_bot()


async def test_an_unanswered_reachability_probe_does_not_fail_the_task(dest):
    """The bulk regression: "could not ask" must not read as "not started"."""
    dest["chat"] = [_chat(kind="PRIVATE")]
    dest["reach"] = [sr.ChatLookupError("rate limited")]
    listener = Listener(up_dest=USER, user_transmission=True)

    await listener._verify_dest_for_bot()

    assert listener.user_transmission is True


# ── which session, before the destination gets a say ────────────────


async def test_bot_transmission_requested_turns_the_user_session_off(dest):
    listener = Listener(bot_trans=True)
    listener.user_dict = {"USER_TRANSMISSION": True, "HYBRID_LEECH": True}

    listener._apply_transmission_defaults()

    assert listener.user_transmission is False
    assert listener.hybrid_leech is False


async def test_user_transmission_requested_turns_it_on(dest):
    listener = Listener(user_trans=True)

    listener._apply_transmission_defaults()

    assert listener.user_transmission is True


async def test_a_pm_upload_has_no_use_for_either_trick(dest):
    """Without a dump chat and outside a group, both tricks are meaningless."""
    listener = Listener(is_super_chat=False, user_trans=True)

    await listener._resolve_upload_destination()

    assert listener.user_transmission is False
    assert listener.hybrid_leech is False


async def test_the_bot_is_checked_when_hybrid_leech_needs_it(dest):
    """Hybrid leech uploads through both, so both have to be verified."""
    listener = Listener(up_dest=DEST, user_trans=True)
    listener.user_dict = {"HYBRID_LEECH": True}

    await listener._resolve_upload_destination()

    asked = [client for kind, client, _ in dest["calls"] if kind == "chat"]
    assert sr.TgClient.user in asked
    assert listener.client in asked


# ── splitting ───────────────────────────────────────────────────────


def test_a_human_split_size_becomes_bytes():
    listener = Listener(split_size="1GB")

    listener._resolve_split_sizes()

    assert listener.split_size == 1024**3


def test_a_numeric_split_size_is_taken_as_bytes():
    listener = Listener(split_size="5000")

    listener._resolve_split_sizes()

    assert listener.split_size == 5000


def test_a_split_size_cannot_exceed_what_the_session_may_send(monkeypatch):
    monkeypatch.setattr(sr.TgClient, "IS_PREMIUM_USER", False)
    listener = Listener(split_size="8GB")

    listener._resolve_split_sizes()

    assert listener.split_size == sr.BOT_MAX_SPLIT_SIZE


def test_a_premium_user_session_raises_the_ceiling(monkeypatch):
    monkeypatch.setattr(sr.TgClient, "IS_PREMIUM_USER", True)
    monkeypatch.setattr(sr.TgClient, "MAX_SPLIT_SIZE", 4194304000)
    listener = Listener(split_size="3GB", user_transmission=True)

    listener._resolve_split_sizes()

    assert listener.split_size == 3 * 1024**3


# ── upload format ───────────────────────────────────────────────────


def test_asking_for_media_rules_out_document():
    listener = Listener(as_med=True)

    listener._resolve_upload_format()

    assert listener.as_doc is False


def test_asking_for_document_is_kept():
    listener = Listener(as_doc=True)

    listener._resolve_upload_format()

    assert listener.as_doc is True


# ── clone dump chats ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "configured, expected",
    [
        pytest.param(DEST, {DEST: None}, id="bare_int"),
        pytest.param(str(DEST), {DEST: None}, id="numeric_string"),
        pytest.param(f"{DEST}|7", {DEST: 7}, id="chat_and_thread"),
        pytest.param("pm", {USER: None}, id="pm_means_the_requester"),
        pytest.param(f"[{DEST}, 'pm']", {DEST: None, USER: None}, id="list_literal"),
        pytest.param("@named", {"@named": None}, id="username"),
    ],
)
def test_every_shape_of_dump_chat_is_indexed_by_chat_and_thread(configured, expected):
    listener = Listener()
    listener.user_dict = {"CLONE_DUMP_CHATS": configured}

    listener._resolve_clone_dump_chats()

    # the keys are `(chat_id, thread_id)` pairs, which is the mapping itself
    assert dict(listener.clone_dump_chats.keys()) == expected


def test_two_topics_of_one_group_are_two_destinations():
    """Keying by chat alone used to keep only the last topic of a group."""
    listener = Listener()
    listener.user_dict = {"CLONE_DUMP_CHATS": f"['{DEST}|12', '{DEST}|34']"}

    listener._resolve_clone_dump_chats()

    assert set(listener.clone_dump_chats) == {(DEST, 12), (DEST, 34)}


def test_a_dump_chat_starts_with_nothing_sent_to_it():
    listener = Listener()
    listener.user_dict = {"CLONE_DUMP_CHATS": DEST}

    listener._resolve_clone_dump_chats()

    assert listener.clone_dump_chats[(DEST, None)]["last_sent_msg"] is None


def test_no_dump_chats_configured_stays_an_empty_mapping():
    listener = Listener()

    listener._resolve_clone_dump_chats()

    assert listener.clone_dump_chats == {}
