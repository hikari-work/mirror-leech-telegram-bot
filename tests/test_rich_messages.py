"""Every text the bot sends travels as a rich message.

Kurigram's rich format is HTML rather than Telegram's own parse mode, so a
message written with ``\\n`` would arrive as one long line: rich HTML treats a
newline as ordinary whitespace. ``rich_html`` is the single translation between
the two, and ``rich_input`` is the wrapper every send and edit goes through.

These tests pin that boundary and the buttons that ride on it: the per-task
Cancel/Select/Force buttons in a status line, and the callbacks that answer
them. Each one is written to fail if the message is sent as plain text again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardMarkup, InputRichMessage

import bot.helper.telegram.button_build as button_build
import bot.helper.telegram.message_utils as mu
import bot.helper.util.bot_utils as bot_utils
import bot.helper.util.status_utils as su
from bot.helper.telegram.button_build import ButtonMaker
from bot.helper.util.status_utils import MirrorStatus
from bot.modules import cancel_task, file_selector, force_start

# ── the conversion layer ────────────────────────────────────────────


def test_a_newline_becomes_the_break_rich_html_understands():
    assert mu.rich_html("first\nsecond") == "first<br>second"


def test_a_windows_newline_becomes_one_break_not_two():
    """A ``\\r`` left behind would be sent as a character of its own."""
    assert mu.rich_html("first\r\nsecond\nthird") == "first<br>second<br>third"


def test_plain_text_is_wrapped_as_a_rich_message():
    rich = mu.rich_input("a\nb")

    assert isinstance(rich, InputRichMessage)
    assert rich.html == "a<br>b"


def test_an_already_built_rich_message_is_left_alone():
    rich = InputRichMessage(html="<b>done</b>")

    assert mu.rich_input(rich) is rich


def test_nothing_becomes_an_empty_rich_message():
    """``edit_message`` may be called with neither text nor rich message."""
    assert mu.rich_input(None).html == ""


class _Replier:
    def __init__(self):
        self.calls: list[dict] = []

    async def reply_rich(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=1)


async def test_send_message_replies_with_a_rich_message():
    message = _Replier()
    buttons = InlineKeyboardMarkup([[]])

    sent = await mu.send_message(message, "a\nb", buttons)

    assert sent.id == 1
    (call,) = message.calls
    assert call["rich_message"].html == "a<br>b"
    assert call["reply_markup"] is buttons
    assert call["disable_notification"] is True


class _Editable:
    def __init__(self):
        self.calls: list[dict] = []

    async def edit(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=1)


async def test_edit_message_edits_with_a_rich_message_text_not_plain_text():
    message = _Editable()

    await mu.edit_message(message, "a\nb")

    (call,) = message.calls
    assert call["text"] is None
    assert call["rich_message"].html == "a<br>b"


async def test_edit_message_accepts_a_ready_made_rich_message():
    message = _Editable()
    rich = InputRichMessage(html="<i>x</i>")

    await mu.edit_message(message, rich_message=rich)

    assert message.calls[0]["rich_message"] is rich


# ── reading a message back ──────────────────────────────────────────


def _rich(blocks):
    return SimpleNamespace(
        text=None, caption=None, rich_message=SimpleNamespace(blocks=blocks)
    )


def test_a_plain_message_still_answers_with_its_text():
    assert mu.message_text(SimpleNamespace(text="/leech x")) == "/leech x"


def test_a_rich_message_answers_with_the_text_inside_its_blocks():
    """A command chained through a rich message must still parse."""
    message = _rich(
        [
            SimpleNamespace(text="Hello"),
            SimpleNamespace(
                text=[SimpleNamespace(text="wor"), SimpleNamespace(text="ld")]
            ),
        ]
    )

    assert mu.message_text(message) == "Hello\nworld"


def test_a_custom_emoji_contributes_its_fallback_text():
    message = _rich([SimpleNamespace(alternative_text="😀")])

    assert mu.message_text(message) == "😀"


def test_a_caption_is_the_last_resort():
    message = SimpleNamespace(text=None, caption="a caption")

    assert mu.message_text(message) == "a caption"


# ── button styles ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("primary", ButtonStyle.PRIMARY),
        ("blue", ButtonStyle.PRIMARY),
        ("danger", ButtonStyle.DANGER),
        ("red", ButtonStyle.DANGER),
        ("success", ButtonStyle.SUCCESS),
        ("green", ButtonStyle.SUCCESS),
        ("unknown", ButtonStyle.DEFAULT),
    ],
)
def test_both_button_colour_spellings_reach_the_same_style(name, expected):
    maker = ButtonMaker()
    maker.data_button("x", "data", style=name)

    assert maker.build_menu().inline_keyboard[0][0].style is expected


def test_a_numeric_label_is_sent_as_a_string():
    """``ytdlp`` labels its quality buttons with the index itself."""
    maker = ButtonMaker()
    maker.data_button(720, "ytq 720")

    button = maker.build_menu().inline_keyboard[0][0]
    assert button.text == "720"
    assert button.callback_data == "ytq 720"


def test_a_url_button_carries_its_style_too():
    maker = ButtonMaker()
    maker.url_button("Open", "https://example.invalid", style="green")

    assert maker.build_menu().inline_keyboard[0][0].style is ButtonStyle.SUCCESS


# ── the buttons inside a status line ────────────────────────────────


class _StatusTask:
    """One line of a status page: a gid, a size and a tool name."""

    def __init__(
        self,
        gid: str,
        status: str,
        *,
        tool: str = "",
        user_id: int = 1,
        force_upload: bool = False,
        force_run: bool = False,
    ) -> None:
        self._gid = gid
        self._status = status
        self.tool = tool
        self.listener = SimpleNamespace(
            mid=1,
            user_id=user_id,
            is_super_chat=False,
            subname=None,
            progress=False,
            force_upload=force_upload,
            force_run=force_run,
        )

    def gid(self) -> str:
        return self._gid

    def status(self) -> str:
        return self._status

    def name(self) -> str:
        return f"name-{self._gid}"

    def size(self) -> str:
        return "1.00KB"


@pytest.fixture
def status_page(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(su.Config, "STATUS_LIMIT", 10)
    monkeypatch.setattr(su.Config, "QUEUE_ALL", False)
    monkeypatch.setattr(su.Config, "QUEUE_DOWNLOAD", False)
    monkeypatch.setattr(su.Config, "QUEUE_UPLOAD", False)

    def render(*tasks):
        monkeypatch.setattr(su, "task_dict", {i: task for i, task in enumerate(tasks)})
        return tasks

    return render


async def test_every_task_line_offers_its_own_cancel_button(status_page):
    task = _StatusTask("g1", MirrorStatus.STATUS_UPLOAD)
    status_page(task)

    msg, _ = await su.get_readable_message(1, False, status="All")

    assert "data='cancel canconf g1'" in msg
    assert "style='danger'" in msg


async def test_a_download_offers_select_only_for_the_tools_that_can_pause(
    status_page,
):
    aria = _StatusTask("g1", MirrorStatus.STATUS_DOWNLOAD, tool="aria2")
    telegram = _StatusTask("g2", MirrorStatus.STATUS_DOWNLOAD, tool="telegram")
    status_page(aria, telegram)

    msg, _ = await su.get_readable_message(1, False, status="All")

    assert "data='sel select g1'" in msg
    assert "data='sel select g2'" not in msg


async def test_a_queued_download_offers_the_three_queue_buttons(
    status_page, monkeypatch
):
    monkeypatch.setattr(su.Config, "QUEUE_DOWNLOAD", True, raising=False)
    task = _StatusTask("g1", MirrorStatus.STATUS_QUEUEDL)
    status_page(task)

    msg, _ = await su.get_readable_message(1, False, status="All")

    for label, data in (
        ("ForceRun", "force run g1"),
        ("ForceDl", "force fd g1"),
        ("ForceUp", "force fu g1"),
    ):
        assert f"data='{data}'" in msg
        assert f">{label}<" in msg


async def test_an_upload_offers_no_queue_buttons(status_page, monkeypatch):
    monkeypatch.setattr(su.Config, "QUEUE_UPLOAD", True, raising=False)
    task = _StatusTask("g1", MirrorStatus.STATUS_UPLOAD)
    status_page(task)

    msg, _ = await su.get_readable_message(1, False, status="All")

    assert "data='force" not in msg


# ── the callbacks behind those buttons ──────────────────────────────


class _Query:
    def __init__(self, data: str, user_id: int = 7) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(id=99)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
def callback_world(monkeypatch):
    """The bot state and Telegram calls a callback touches, all recorded."""
    world = SimpleNamespace(
        multi_tags={},
        deleted=[],
        sent=[],
        sudo=False,
        task=None,
    )

    async def is_sudo(_):
        return world.sudo

    async def get_task_by_gid(gid):
        return world.task

    async def send_message(message, text, buttons=None, **kwargs):
        world.sent.append((text, buttons))
        return SimpleNamespace(id=1)

    async def delete_message(message):
        world.deleted.append(message)

    async def auto_delete_message(*messages):
        world.deleted.extend(messages)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(cancel_task, "multi_tags", world.multi_tags)
    monkeypatch.setattr(cancel_task, "get_task_by_gid", get_task_by_gid)
    monkeypatch.setattr(cancel_task.CustomFilters, "is_sudo", is_sudo)
    monkeypatch.setattr(cancel_task, "send_message", send_message)
    monkeypatch.setattr(cancel_task, "delete_message", delete_message)
    monkeypatch.setattr(cancel_task, "auto_delete_message", auto_delete_message)
    monkeypatch.setattr(force_start, "get_task_by_gid", get_task_by_gid)
    monkeypatch.setattr(force_start.CustomFilters, "is_sudo", is_sudo)
    monkeypatch.setattr(force_start, "queued_up", set())
    monkeypatch.setattr(force_start, "queued_dl", set())
    monkeypatch.setattr(force_start, "start_up_from_queued", nothing)
    monkeypatch.setattr(force_start, "start_dl_from_queued", nothing)
    monkeypatch.setattr(file_selector, "get_task_by_gid", get_task_by_gid)
    monkeypatch.setattr(file_selector.CustomFilters, "is_sudo", is_sudo)
    monkeypatch.setattr(file_selector, "delete_message", delete_message)
    return world


class _TaskObj:
    def __init__(self) -> None:
        self.cancelled = False

    async def cancel_task(self) -> None:
        self.cancelled = True


def _task(user_id: int = 7):
    listener = SimpleNamespace(
        user_id=user_id,
        mid=1,
        force_upload=False,
        force_download=False,
    )
    obj = _TaskObj()
    return SimpleNamespace(listener=listener, task=lambda: obj), obj


async def test_cancel_close_only_closes_the_confirmation(callback_world):
    query = _Query("cancel close")

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert callback_world.deleted == [query.message]
    assert callback_world.multi_tags == {}


async def test_a_multi_tag_belongs_to_the_user_who_started_it(callback_world):
    callback_world.multi_tags["Tg1"] = 8
    query = _Query("cancel multi Tg1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert callback_world.multi_tags == {"Tg1": 8}
    assert query.answers == [("Not Yours!", True)]


async def test_the_owner_stops_their_multi(callback_world):
    callback_world.multi_tags["Tg1"] = 7
    query = _Query("cancel multi Tg1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert callback_world.multi_tags == {}
    assert query.answers == [("Stopped!", True)]


async def test_a_sudo_may_stop_someone_elses_multi(callback_world):
    callback_world.multi_tags["Tg1"] = 8
    callback_world.sudo = True
    query = _Query("cancel multi Tg1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert callback_world.multi_tags == {}


async def test_a_finished_multi_is_reported_as_such(callback_world):
    query = _Query("cancel multi Gone", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert query.answers == [("Already Stopped/Finished!", True)]


async def test_cancel_asks_before_cancelling_and_offers_both_answers(callback_world):
    task, obj = _task(user_id=7)
    callback_world.task = task
    query = _Query("cancel canconf g1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert not obj.cancelled
    (text, menu), = callback_world.sent
    assert text == "Are you sure you want to cancel this task?"
    yes, no = menu.inline_keyboard[0]
    assert (yes.text, yes.callback_data) == ("Yes", "cancel conf g1")
    assert (no.text, no.callback_data) == ("No", "cancel close")


async def test_cancel_conf_cancels_after_confirmation(callback_world):
    task, obj = _task(user_id=7)
    callback_world.task = task
    query = _Query("cancel conf g1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert obj.cancelled
    assert callback_world.deleted == [query.message]


async def test_a_task_of_someone_else_is_not_cancelled(callback_world):
    task, obj = _task(user_id=8)
    callback_world.task = task
    query = _Query("cancel conf g1", user_id=7)

    await cancel_task.cancel_updates.__wrapped__(None, query)

    assert not obj.cancelled
    assert query.answers == [("Not Yours!", True)]


async def test_force_up_starts_a_queued_upload(callback_world):
    task, _ = _task(user_id=7)
    callback_world.task = task
    force_start.queued_up.add(1)
    query = _Query("force fu g1", user_id=7)

    await force_start.remove_from_queue_callback.__wrapped__(None, query)

    assert task.listener.force_upload is True
    # the "run" branch, which is the fallback for every other word, sets this
    # too; fu must not take it
    assert task.listener.force_download is False
    assert query.answers == [("Task have been force started to upload!", True)]


async def test_force_download_on_a_task_not_in_the_queue_only_flags_it(
    callback_world,
):
    task, _ = _task(user_id=7)
    callback_world.task = task
    query = _Query("force fd g1", user_id=7)

    await force_start.remove_from_queue_callback.__wrapped__(None, query)

    assert task.listener.force_download is True
    assert task.listener.force_upload is False
    assert query.answers == [("This task not in download queue!", True)]


async def test_force_run_starts_a_queued_download(callback_world):
    task, _ = _task(user_id=7)
    callback_world.task = task
    force_start.queued_dl.add(1)
    query = _Query("force run g1", user_id=7)

    await force_start.remove_from_queue_callback.__wrapped__(None, query)

    assert task.listener.force_download is True
    assert task.listener.force_upload is True
    assert query.answers == [
        (
            "Task have been force started to download and upload will "
            "start once download finish!",
            True,
        )
    ]


async def test_a_force_callback_for_someone_elses_task_does_nothing(callback_world):
    task, _ = _task(user_id=8)
    callback_world.task = task
    query = _Query("force fu g1", user_id=7)

    await force_start.remove_from_queue_callback.__wrapped__(None, query)

    assert task.listener.force_upload is False
    assert query.answers == [("This task is not for you!", True)]


def test_the_button_style_accepts_the_enum_itself():
    assert button_build._button_style(ButtonStyle.SUCCESS) is ButtonStyle.SUCCESS


# ── the file-selection menu ─────────────────────────────────────────


def test_the_selection_menu_closes_through_its_own_callback(monkeypatch):
    monkeypatch.setattr(bot_utils.Config, "WEB_PINCODE", False)
    monkeypatch.setattr(bot_utils.Config, "BASE_URL", "https://example.invalid")

    menu = bot_utils.bt_selection_buttons("a" * 30)
    close = next(
        button
        for row in menu.inline_keyboard
        for button in row
        if button.text == "Close"
    )
    assert close.callback_data == "sel close"


async def test_the_close_press_deletes_the_selection_menu(callback_world):
    query = _Query("sel close")

    await file_selector.confirm_selection.__wrapped__(None, query)

    assert query.answers == [(None, False)]
    assert callback_world.deleted == [query.message]


class _SelectTask:
    def __init__(self, user_id: int = 7, status=MirrorStatus.STATUS_DOWNLOAD):
        self.listener = SimpleNamespace(user_id=user_id)
        self._status = status

    async def status(self):
        return self._status

    def name(self) -> str:
        return "a-file.mkv"


async def test_select_answers_on_a_finished_task(callback_world):
    class Finished(_SelectTask):
        def status(self):  # a plain method: no download stage left
            return MirrorStatus.STATUS_UPLOAD

    callback_world.task = Finished()
    query = _Query("sel select g1", user_id=7)

    await file_selector.select_callback(query)

    assert query.answers == [
        ("The task have finished the download stage!", True)
    ]


async def test_select_refuses_an_uploading_task(callback_world):
    callback_world.task = _SelectTask(status=MirrorStatus.STATUS_UPLOAD)
    query = _Query("sel select g1", user_id=7)

    await file_selector.select_callback(query)

    assert query.answers == [
        ("Task should be in download or pause or queued status", True)
    ]


async def test_select_belongs_to_the_task_owner(callback_world):
    callback_world.task = _SelectTask(user_id=8)
    query = _Query("sel select g1", user_id=7)

    await file_selector.select_callback(query)

    assert query.answers == [("This task is not for you!", True)]
