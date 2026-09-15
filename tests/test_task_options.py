"""Tests for the option keyboard a ``/leech`` or ``/ytdl`` shows before starting.

The keyboard is split on purpose: everything that decides *what a button does*
is a plain function over a plain dict, and the telegram half only moves those
decisions in and out of a message. These tests hold both halves to that -- the
state functions are called directly, and the exchange is driven through the
real callback with telegram replaced by recorders.
"""

from __future__ import annotations

from asyncio import CancelledError, Event, create_task, wait_for
from importlib import import_module
from types import SimpleNamespace

import pytest

from bot.core.config_manager import Config
from bot.helper.telegram import task_options as opts
from bot.helper.util.task_args import (
    LeechArgs,
    YtdlpArgs,
    parse_leech_args,
    parse_ytdlp_args,
)
from bot.modules.leech import Leech
from bot.modules.ytdlp import YtDlp

# ``bot.modules`` re-exports the *handlers* under these names, so the modules
# themselves are reached through the import system instead of an attribute.
leech_module = import_module("bot.modules.leech")
ytdlp_module = import_module("bot.modules.ytdlp")

DEST = -1001234567890
UID = 42
LINK = "http://example.com/a.mkv"
PRESETS = {"movies": ["-1001"], "music": ["-1002", "-1003"]}


def leech_args(*tokens):
    return parse_leech_args([LINK, *tokens])


def ytdl_args(*tokens):
    return parse_ytdlp_args([LINK, *tokens])


def state_of(*tokens, command="leech"):
    args = leech_args(*tokens) if command == "leech" else ytdl_args(*tokens)
    return opts.state_from(args, command)


# ── the starting state ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "flag, field",
    [("-e", "-e"), ("-z", "-z"), ("-s3", "-s3"), ("-doc", "-doc")],
)
def test_a_typed_flag_starts_switched_on(flag, field):
    """The command line is the first vote; the keyboard is the chance to change it."""
    assert state_of(LINK, flag)[field] is True


def test_a_flag_that_was_not_typed_starts_off():
    assert state_of()["-e"] is False


def test_typed_values_carry_into_the_state():
    state = state_of("-n", "my name", "-sp", "2gb")

    assert state["-n"] == "my name"
    assert state["-sp"] == "2gb"


def test_the_state_only_carries_the_flags_it_offers():
    assert set(state_of()) == set(opts.options_for("leech"))


# ── toggling ────────────────────────────────────────────────────────


def test_a_toggle_switches_both_ways():
    state = state_of()

    opts.toggle(state, "-e")
    assert state["-e"] is True
    opts.toggle(state, "-e")
    assert state["-e"] is False


@pytest.mark.parametrize(
    "on, off", [("-doc", "-med"), ("-med", "-doc")], ids=["doc", "med"]
)
def test_the_two_upload_formats_exclude_each_other(on, off):
    """Both at once is not a stricter request, it is an unanswered one."""
    state = state_of(on)

    opts.toggle(state, off)

    assert state[off] is True
    assert state[on] is False


@pytest.mark.parametrize(
    "on, off", [("-s3", "-tg"), ("-tg", "-s3")], ids=["s3", "tg"]
)
def test_the_two_destinations_exclude_each_other(on, off):
    state = state_of(on)

    opts.toggle(state, off)

    assert state[off] is True
    assert state[on] is False


def test_switching_a_group_off_leaves_its_partner_alone():
    """Turning -s3 off must not read as "use -tg"; it means the setting decides."""
    state = state_of("-s3")

    opts.toggle(state, "-s3")

    assert state["-s3"] is False
    assert state["-tg"] is False


def test_the_last_destination_pressed_is_the_one_that_holds():
    """The command said s3, the user tried tg, then went back."""
    state = state_of("-s3")

    opts.toggle(state, "-tg")
    opts.toggle(state, "-s3")

    assert state["-s3"] is True
    assert state["-tg"] is False


# ── presets ─────────────────────────────────────────────────────────


def test_pressing_a_preset_selects_it():
    state = state_of()

    opts.select_preset(state, "movies")

    assert state["-c"] == "movies"


def test_pressing_another_preset_moves_the_selection():
    state = state_of("-c", "movies")

    opts.select_preset(state, "music")

    assert state["-c"] == "music"


def test_pressing_the_selected_preset_again_gives_it_up():
    state = state_of("-c", "movies")

    opts.select_preset(state, "movies")

    assert state["-c"] == ""


# ── typed values ────────────────────────────────────────────────────


def test_a_name_is_taken_as_typed():
    state = state_of()

    assert opts.set_value(state, "-n", "  my name  ") == ""
    assert state["-n"] == "my name"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_an_empty_message_sets_nothing(blank):
    state = state_of("-n", "kept")

    assert opts.set_value(state, "-n", blank) != ""
    assert state["-n"] == "kept"


@pytest.mark.parametrize("size", ["2gb", "500mb", "1024", "1t"])
def test_a_split_size_the_resolver_would_use_is_accepted(size):
    """Anything refused here falls back to the user's setting instead."""
    state = state_of()

    assert opts.set_value(state, "-sp", size) == ""
    assert state["-sp"] == size


@pytest.mark.parametrize("junk", ["abc", "0", "-5", "2 elephants"])
def test_a_split_size_the_resolver_would_ignore_is_refused(junk):
    state = state_of("-sp", "2gb")

    assert opts.set_value(state, "-sp", junk) != ""
    assert state["-sp"] == "2gb"


@pytest.mark.parametrize("flag", ["-e", "-z"], ids=["extract", "zip"])
def test_the_flags_that_carry_a_password_ask_for_one(flag):
    """``-e password`` and ``-z password`` are what the pipeline reads."""
    assert opts.OPTIONS[flag].kind == opts.EXTRA


def test_a_screenshot_count_is_not_asked_for():
    """``-ss`` takes a number, but the default is a usable answer."""
    assert opts.OPTIONS["-ss"].kind == opts.BOOL


@pytest.mark.parametrize("flag", ["-e", "-z"], ids=["extract", "zip"])
def test_a_password_is_taken_as_typed(flag):
    state = state_of()

    assert opts.set_value(state, flag, " hunter2 ") == ""
    assert state[flag] == "hunter2"


# ── writing back ────────────────────────────────────────────────────


def test_applying_the_state_writes_the_field_each_flag_names():
    args = leech_args()
    state = opts.state_from(args, "leech")
    opts.toggle(state, "-doc")
    opts.toggle(state, "-s3")
    opts.select_preset(state, "movies")
    opts.set_value(state, "-n", "renamed")

    opts.apply_state(state, args)

    assert (args.as_doc, args.is_s3, args.copy_preset, args.name) == (
        True,
        True,
        "movies",
        "renamed",
    )


def test_applying_twice_leaves_the_args_where_once_did():
    args = leech_args(LINK, "-e")
    state = opts.state_from(args, "leech")
    opts.toggle(state, "-ss")

    opts.apply_state(state, args)
    once = vars(args).copy()
    opts.apply_state(state, args)

    assert vars(args) == once


@pytest.mark.parametrize(
    "command, dataclass",
    [("leech", LeechArgs), ("ytdl", YtdlpArgs)],
)
def test_every_offered_field_exists_on_the_args_it_writes_to(command, dataclass):
    """A renamed field would otherwise only show up as a broken button."""
    for flag in opts.options_for(command):
        assert hasattr(dataclass(), opts.OPTIONS[flag].field), flag


# ── which flags each command offers ─────────────────────────────────


def test_ytdl_is_not_offered_the_leech_only_flags():
    """``/ytdl`` has no extract step and no telegram stream upload."""
    offered = opts.options_for("ytdl")

    assert "-e" not in offered
    assert "-su" not in offered
    assert "-doc" in offered


def test_leech_is_offered_them():
    offered = opts.options_for("leech")

    assert "-e" in offered
    assert "-su" in offered


def test_the_state_of_a_command_holds_only_its_own_flags():
    state = opts.state_from(ytdl_args(), "ytdl")

    assert "-e" not in state
    assert "-ss" in state


# ── rendering ───────────────────────────────────────────────────────


def buttons_of(markup):
    return [button for row in markup.inline_keyboard for button in row]


def labelled(markup, fragment):
    """The button whose text contains *fragment*, or an assertion failure."""
    for button in buttons_of(markup):
        if fragment in button.text:
            return button
    raise AssertionError(f"no button labelled {fragment!r}")


def data_for(markup, action):
    """The callback data of the button that carries *action*."""
    for button in buttons_of(markup):
        if button.callback_data.endswith(f" {action}"):
            return button.callback_data
    raise AssertionError(f"no button for action {action!r}")


def rendered(*tokens, command="leech", presets=PRESETS, view=opts.MAIN_VIEW):
    state = state_of(*tokens, command=command)
    text, markup = opts.render(state, presets, LINK, command, UID, view)
    return state, text, markup


def test_the_keyboard_marks_what_is_on():
    _, _, markup = rendered(LINK, "-z", "-s3")

    assert labelled(markup, "Zip").text.startswith("✅")
    assert labelled(markup, "Extract").text.startswith("⬜")
    assert labelled(markup, "S3").text.startswith("✅")


def test_a_password_is_marked_as_set_and_never_printed():
    _, text, markup = rendered(LINK, "-e", "hunter2")

    assert "hunter2" not in text
    assert "hunter2" not in labelled(markup, "Extract").text
    assert "🔒" in labelled(markup, "Extract").text


def test_a_typed_value_is_shown_on_its_button():
    _, text, markup = rendered(LINK, "-n", "renamed")

    assert "renamed" in labelled(markup, "Name").text
    assert "-n renamed" in text


def test_every_preset_gets_a_button_and_only_one_is_marked():
    _, _, markup = rendered(LINK, "-c", "movies", view=opts.PRESET_VIEW)

    assert labelled(markup, "movies").text.startswith("✅")
    assert labelled(markup, "music").text.startswith("⬜")


def test_a_preset_named_in_the_command_but_not_saved_still_gets_a_button():
    """Otherwise a typo'd ``-c`` could not be switched off."""
    _, _, markup = rendered(LINK, "-c", "typo", view=opts.PRESET_VIEW)

    assert labelled(markup, "typo") is not None


def test_the_user_without_presets_is_told_where_they_are_made():
    state, text, _ = rendered(presets={})

    assert not state["-c"]
    assert "no copy presets" in text


def button_texts(markup):
    """Every button's text, for the tests that assert something is *not* there."""
    return [button.text for button in buttons_of(markup)]


def test_the_main_page_keeps_the_presets_behind_one_button():
    """A user with five presets would otherwise push Start off the keyboard."""
    _, _, markup = rendered()

    assert labelled(markup, "Copy Destination") is not None
    assert not any("movies" in text for text in button_texts(markup))


def test_a_user_without_presets_is_offered_no_preset_page():
    """A button with nothing behind it is a dead end; the text says how."""
    _, _, markup = rendered(presets={})

    assert not any("Copy Destination" in text for text in button_texts(markup))


def test_the_copy_button_names_what_is_selected():
    _, _, markup = rendered(LINK, "-c", "movies")

    assert "movies" in labelled(markup, "Copy Destination").text


def test_the_preset_page_carries_a_way_back():
    _, _, markup = rendered(view=opts.PRESET_VIEW)

    assert data_for(markup, "b")
    assert [button.text for button in markup.inline_keyboard[-1]] == [
        "↩️ Back",
        "✖️ Cancel",
    ]


def test_the_preset_page_says_what_is_selected():
    _, text, _ = rendered(LINK, "-c", "movies", view=opts.PRESET_VIEW)

    assert "<b>Copy Destination:</b> <code>movies</code>" in text


def test_the_preset_page_says_when_nothing_is_selected():
    _, text, _ = rendered(view=opts.PRESET_VIEW)

    assert "<b>Copy Destination:</b> <code>none</code>" in text


def test_the_flags_are_not_on_the_preset_page():
    _, _, markup = rendered("-z", view=opts.PRESET_VIEW)

    assert not any("Zip" in text for text in button_texts(markup))
    assert labelled(markup, "movies") is not None


def test_start_and_cancel_sit_on_the_last_row():
    _, _, markup = rendered()

    assert [button.text for button in markup.inline_keyboard[-1]] == [
        "▶️ Start",
        "✖️ Cancel",
    ]


def test_every_button_carries_the_owner():
    """The handler re-checks the id it finds in the data, so it has to be there."""
    _, _, markup = rendered()

    for button in buttons_of(markup):
        assert button.callback_data.split()[1] == str(UID)
        assert button.callback_data.startswith(opts.PREFIX)


def test_the_link_is_shown_but_cannot_break_the_message():
    text, _ = opts.render(
        opts.state_from(leech_args(), "leech"), {}, f"{LINK}<b>x</b>", "leech", UID
    )

    assert "&lt;b&gt;" in text


# ── when there is no keyboard ───────────────────────────────────────


class FakeListener:
    """The listener surface ``offers_keyboard`` and the exchange read."""

    def __init__(self, **overrides):
        self.user_id = UID
        self.user_dict = {"COPY_PRESETS": PRESETS}
        self.link = LINK
        self.message = SimpleNamespace(id=7, chat=SimpleNamespace(id=DEST))
        self.is_rss = False
        self.bulk_child = False
        self.multi = 0
        self.multi_tag = None
        self.applied = []
        for key, value in overrides.items():
            setattr(self, key, value)

    def apply_options(self, args):
        self.applied.append(vars(args).copy())


@pytest.mark.parametrize(
    "reason",
    ["is_rss", "bulk_child", "multi_tag"],
)
def test_a_task_without_a_user_waiting_gets_no_keyboard(reason):
    overrides = {
        "is_rss": True,
        "bulk_child": True,
        "multi_tag": "@chan",
    }
    listener = FakeListener(**{reason: overrides[reason]})

    assert opts.offers_keyboard(listener, leech_args()) is False


def test_a_bulk_command_gets_no_keyboard():
    """``-b`` dispatches one task per link; the flags decide how, not the text."""
    assert opts.offers_keyboard(FakeListener(), leech_args("-b")) is False


def test_a_multi_chain_gets_no_keyboard():
    assert opts.offers_keyboard(FakeListener(), leech_args("-i", "3")) is False


def test_a_plain_command_gets_one():
    assert opts.offers_keyboard(FakeListener(), leech_args()) is True


def test_a_command_from_a_chat_gets_no_keyboard():
    """A channel post or an anonymous admin has no user the buttons belong to."""
    listener = FakeListener()
    listener.message = SimpleNamespace(
        id=7,
        chat=SimpleNamespace(id=DEST),
        sender_chat=SimpleNamespace(id=DEST),
    )

    assert opts.offers_keyboard(listener, leech_args()) is False


# ── the exchange ────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, message_id=11):
        self.id = message_id
        self.chat = SimpleNamespace(id=DEST)


class FakeQuery:
    def __init__(self, data, message, user_id=UID):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class Wire:
    """The telegram half of the keyboard, replaced by recorders."""

    def __init__(self, prompt):
        self.prompt = prompt
        self.sent = []
        self.edits = []
        self.deleted = []
        self.sent_event = Event()
        self.reply = None
        # a value prompt that is still open while other buttons get pressed
        self.hold_reply = False
        self.reply_started = Event()
        self.reply_ready = Event()

    async def send_message(self, message, text, buttons=None, block=True):
        self.sent.append((text, buttons))
        self.sent_event.set()
        return self.prompt

    async def edit_message(self, message, text, buttons=None, block=True):
        self.edits.append((text, buttons))
        return message

    async def auto_delete_message(self, message, *args, **kwargs):
        self.deleted.append(message)

    async def delete_message(self, message, *args, **kwargs):
        self.deleted.append(message)

    async def wait_for_reply(self, client, message, user_id, timeout=60):
        if self.hold_reply:
            self.reply_started.set()
            await self.reply_ready.wait()
        return self.reply


@pytest.fixture
def wire(monkeypatch):
    """A recorder standing in for every telegram call the keyboard makes."""
    recorder = Wire(FakeMessage())
    for name in (
        "send_message",
        "edit_message",
        "auto_delete_message",
        "delete_message",
    ):
        monkeypatch.setattr(opts, name, getattr(recorder, name))
    monkeypatch.setattr(opts, "wait_for_reply", recorder.wait_for_reply)
    return recorder


async def opening(listener, args, wire, command="leech"):
    """Run ``ask_options`` until its prompt is out, then hand back the task."""
    task = create_task(opts.ask_options(listener, args, command))
    await wait_for(wire.sent_event.wait(), 1)
    return task


async def press(wire, action, flag="", owner_id=UID, presser_id=UID):
    """Press the button carrying *action*, the way the handler receives it.

    The data names the user the keyboard was sent to; *presser_id* is who
    telegram says is pressing. They are the same person unless a test says
    otherwise.
    """
    query = FakeQuery(
        f"{opts.PREFIX} {owner_id} {action}{flag}", wire.prompt, presser_id
    )
    await opts.task_options_callback(None, query)
    return query


async def close_unpressed(task):
    """Drop a keyboard no test pressed, so the test leaves no task behind."""
    task.cancel()
    with pytest.raises(CancelledError):
        await task


async def test_starting_lets_the_task_run(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "go")

    assert await task is True


async def test_starting_takes_the_keyboard_away(wire):
    """The task's own status message is the next thing in the chat."""
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "go")

    assert await task is True
    assert wire.deleted == [wire.prompt]
    assert wire.edits == []


async def test_cancelling_drops_the_task(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "x")

    assert await task is False
    assert wire.deleted == [wire.prompt]


async def test_the_prompt_is_closed_once_decided(wire):
    """The buttons must not stay pressable after the task has gone."""
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "go")

    assert await task is True
    assert opts.pending == {}


async def test_a_keyboard_nobody_presses_expires(monkeypatch, wire):
    monkeypatch.setattr(opts, "PROMPT_TTL", 0.05)
    listener = FakeListener()

    assert await opts.ask_options(listener, leech_args(), "leech") is False
    assert "expired" in wire.edits[-1][0]


async def test_a_toggle_redraws_and_re_settles_the_task(wire):
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)

    await press(wire, "t:", "-s3")

    assert args.is_s3 is True
    assert listener.applied[-1]["is_s3"] is True
    assert "S3" in wire.edits[-1][0] or "-s3" in wire.edits[-1][0]
    await press(wire, "x")
    assert await task is False


async def test_a_started_task_keeps_what_was_toggled(wire):
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)

    await press(wire, "t:", "-doc")
    await press(wire, "go")

    assert await task is True
    assert args.as_doc is True


async def test_pressing_a_preset_reaches_the_task(wire):
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)

    await press(wire, "p:", "music")

    assert args.copy_preset == "music"
    await press(wire, "x")
    assert await task is False


async def test_the_copy_button_opens_the_preset_page(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "c")

    _, markup = wire.edits[-1]
    assert labelled(markup, "music") is not None
    assert labelled(markup, "Back") is not None
    await press(wire, "x")
    assert await task is False


async def test_a_preset_picked_on_its_page_stays_there(wire):
    """Picking one is not the end of the errand: a second one may follow."""
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)

    await press(wire, "c")
    await press(wire, "p:", "music")

    assert args.copy_preset == "music"
    _, markup = wire.edits[-1]
    assert labelled(markup, "music").text.startswith("✅")
    assert labelled(markup, "Back") is not None
    await press(wire, "x")
    assert await task is False


async def test_back_returns_to_the_flags(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    await press(wire, "c")
    await press(wire, "b")

    _, markup = wire.edits[-1]
    assert labelled(markup, "S3") is not None
    assert not any("music" in text for text in button_texts(markup))
    await press(wire, "x")
    assert await task is False


async def test_starting_from_the_preset_page_works_too(wire):
    """Start and Cancel are on both pages, so neither is a trap."""
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)

    await press(wire, "c")
    await press(wire, "p:", "music")
    await press(wire, "go")

    assert await task is True
    assert args.copy_preset == "music"


@pytest.mark.parametrize("flag", ["-e", "-z"], ids=["extract", "zip"])
async def test_pressing_a_password_flag_asks_for_one(wire, flag):
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)
    wire.reply = "hunter2"

    await press(wire, "t:", flag)

    assert getattr(args, opts.OPTIONS[flag].field) == "hunter2"
    asked = wire.edits[0][0]  # the prompt, before the reply was taken
    assert "password" in asked
    assert "hunter2" not in asked
    assert "hunter2" not in wire.edits[-1][0]
    await press(wire, "x")
    assert await task is False


@pytest.mark.parametrize("flag", ["-e", "-z"], ids=["extract", "zip"])
async def test_a_flag_whose_password_never_arrives_stays_on(wire, flag):
    """No password is a usable answer; the archive may not have one."""
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)
    wire.reply = None

    await press(wire, "t:", flag)

    assert getattr(args, opts.OPTIONS[flag].field) is True
    await press(wire, "x")
    assert await task is False


async def test_the_extract_button_switches_a_password_off(wire):
    listener = FakeListener()
    args = leech_args("-e", "hunter2")
    task = await opening(listener, args, wire)

    await press(wire, "t:", "-e")

    assert args.extract is False
    # one redraw for the press, and no prompt for a password it is not taking
    assert len(wire.edits) == 1
    await press(wire, "x")
    assert await task is False


async def test_a_value_prompt_stores_what_was_typed(wire):
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)
    wire.reply = "  renamed  "

    await press(wire, "v:", "-n")

    assert args.name == "renamed"
    assert listener.applied[-1]["name"] == "renamed"
    await press(wire, "x")
    assert await task is False


async def test_a_value_that_never_arrives_changes_nothing(wire):
    listener = FakeListener()
    args = leech_args("-n", "kept")
    task = await opening(listener, args, wire)
    wire.reply = None

    await press(wire, "v:", "-n")

    assert args.name == "kept"
    assert "nothing changed" in wire.edits[-1][0]
    await press(wire, "x")
    assert await task is False


async def test_a_refused_value_says_so_and_keeps_the_old_one(wire):
    listener = FakeListener()
    args = leech_args("-sp", "2gb")
    task = await opening(listener, args, wire)
    wire.reply = "abc"

    await press(wire, "v:", "-sp")

    assert args.split_size == "2gb"
    assert "not a size" in wire.edits[-1][0]
    await press(wire, "x")
    assert await task is False


async def test_someone_else_cannot_press_the_buttons(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    query = await press(wire, "go", presser_id=UID + 1)

    assert query.answers[-1][1] is True  # shown as an alert
    assert "not yours" in query.answers[-1][0]
    await close_unpressed(task)


@pytest.mark.parametrize(
    "data",
    ["", "lopt", "lopt 42", "lopt who go", "lopt 42 go extra"],
    ids=["empty", "short", "no-action", "no-id", "long"],
)
async def test_data_this_handler_did_not_write_is_ignored(wire, data):
    """A press that is not one of these buttons must not reach the state."""
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)

    query = FakeQuery(data, wire.prompt)
    await opts.task_options_callback(None, query)

    assert query.answers == [(None, False)]
    assert opts.pending  # the keyboard is still open
    await close_unpressed(task)


async def test_a_press_after_the_keyboard_gave_up_is_refused(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)
    opts.pending[(DEST, wire.prompt.id)].at -= opts.PROMPT_TTL + 1

    query = await press(wire, "go")

    assert "expired" in query.answers[-1][0]
    assert opts.pending == {}
    await close_unpressed(task)


async def test_pressing_a_closed_keyboard_says_so(wire):
    listener = FakeListener()
    task = await opening(listener, leech_args(), wire)
    await press(wire, "x")
    assert await task is False

    query = await press(wire, "go")

    assert "closed" in query.answers[-1][0]


@pytest.mark.parametrize("flag", ["-e", "-su"], ids=["extract", "stream"])
async def test_a_flag_this_command_does_not_offer_is_ignored(wire, flag):
    """``/ytdl`` has no extract step, so a press for one changes nothing."""
    listener = FakeListener()
    args = ytdl_args()
    task = await opening(listener, args, wire, command="ytdl")

    query = await press(wire, "t:", flag)

    assert query.answers == [(None, False)]
    assert listener.applied == []
    await close_unpressed(task)


async def test_a_value_that_arrives_after_start_does_not_reopen_the_keyboard(wire):
    """The task has already gone on; a late reply must not put buttons back."""
    listener = FakeListener()
    args = leech_args()
    task = await opening(listener, args, wire)
    wire.hold_reply = True
    wire.reply = "late"
    typing = create_task(press(wire, "v:", "-n"))
    await wait_for(wire.reply_started.wait(), 1)

    await press(wire, "go")
    assert await task is True
    wire.reply_ready.set()
    await typing

    assert args.name == ""  # the value never reached the args
    assert "-n late" not in wire.edits[-1][0]


async def test_a_keyboard_that_cannot_be_sent_still_runs_the_task(monkeypatch):
    """Telegram refusing the prompt is no reason to drop what the user typed."""

    async def refuse(message, text, buttons=None, block=True):
        return "FloodWait: 30 seconds"

    monkeypatch.setattr(opts, "send_message", refuse)
    listener = FakeListener()

    assert await opts.ask_options(listener, leech_args(), "leech") is True
    assert opts.pending == {}


async def test_a_task_that_needs_no_keyboard_is_not_asked(monkeypatch):
    async def unreachable(*args, **kwargs):
        raise AssertionError("a keyboard was sent to an RSS task")

    monkeypatch.setattr(opts, "send_message", unreachable)
    listener = FakeListener(is_rss=True)

    assert await opts.ask_options(listener, leech_args(), "leech") is True


# ── the toggled options reach the real task ─────────────────────────


def command_message(text=f"/leech {LINK}"):
    """The message fields ``TaskConfig.__init__`` reads off a real command."""
    return SimpleNamespace(
        id=7,
        text=text,
        from_user=SimpleNamespace(id=UID),
        chat=SimpleNamespace(
            id=DEST, type=SimpleNamespace(name="SUPERGROUP"), is_admin=True
        ),
    )


def test_a_toggle_on_the_keyboard_moves_a_real_leech(monkeypatch):
    """The keyboard writes args and the task re-reads them -- end to end."""
    monkeypatch.setattr(Config, "UPLOAD_DESTINATION", "tg")
    args = leech_args()
    listener = Leech(SimpleNamespace(), command_message())
    listener._apply_args(args)
    listener.user_dict = {"COPY_PRESETS": PRESETS}
    state = opts.state_from(args, "leech")

    opts.toggle(state, "-s3")
    opts.apply_state(state, args)
    listener.apply_options(args)
    assert listener.destination == "s3"
    assert listener.stream_upload is False

    # and back off again: a state that only ever turns things on would leave
    # the task on the bucket with no way back
    opts.toggle(state, "-s3")
    opts.apply_state(state, args)
    listener.apply_options(args)
    assert listener.destination == "tg"


class ReachedTask(Exception):
    """Raised where the real download would begin, to stop the flow there."""


def wiring_listener(cls, text):
    """*cls* with everything around the gate stubbed out.

    What is left real is the part under test: the order the keyboard sits in,
    and the arguments it is handed. ``calls`` records what ran.
    """
    listener = cls(SimpleNamespace(), command_message(text))
    calls = []

    async def noop(*args, **kwargs):
        return None

    async def reply(_):
        return (None, None, None)

    async def resolved(_):
        return True

    async def before_start():
        calls.append("before_start")
        raise ReachedTask

    listener.register_same_dir = noop
    listener.run_multi = noop
    listener.get_tag = noop
    listener.fail_task = noop
    listener._resolve_reply = reply
    listener._resolve_special_links = resolved
    listener.before_start = before_start
    return listener, calls


@pytest.mark.parametrize(
    "module_name, cls, text, flag",
    [
        ("leech", Leech, f"/leech {LINK} -s3", "is_s3"),
        ("ytdl", YtDlp, f"/ytdl {LINK} -z", "compress"),
    ],
)
async def test_the_task_waits_for_the_keyboard(
    monkeypatch, module_name, cls, text, flag
):
    """Without the gate the task would go straight to ``before_start``."""
    module = leech_module if module_name == "leech" else ytdlp_module
    listener, calls = wiring_listener(cls, text)
    handed = []

    async def ask(got, args, command):
        assert got is listener
        assert command == module_name
        handed.append(getattr(args, flag))
        calls.append("asked")
        return False

    monkeypatch.setattr(module, "ask_options", ask)

    await listener.new_event()

    assert calls == ["asked"]
    assert handed == [True]  # the flags the command carried, not the defaults


@pytest.mark.parametrize(
    "module_name, cls, text",
    [
        ("leech", Leech, f"/leech {LINK}"),
        ("ytdl", YtDlp, f"/ytdl {LINK}"),
    ],
)
async def test_starting_lets_the_task_go_on(monkeypatch, module_name, cls, text):
    module = leech_module if module_name == "leech" else ytdlp_module
    listener, calls = wiring_listener(cls, text)

    async def ask(got, args, command):
        calls.append("asked")
        return True

    monkeypatch.setattr(module, "ask_options", ask)

    await listener.new_event()

    assert calls == ["asked", "before_start"]
