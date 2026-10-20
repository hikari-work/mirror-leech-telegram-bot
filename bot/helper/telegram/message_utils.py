import html
from asyncio import sleep
from collections.abc import Sequence
from dataclasses import dataclass
from re import compile as re_compile
from time import time
from typing import TYPE_CHECKING

from pyrogram.errors import FloodPremiumWait, FloodWait

from ... import DOWNLOAD_DIR, LOGGER, intervals, status_dict, task_dict_lock
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient, get_user_client
from ..util.bot_utils import SetInterval
from ..util.exceptions import TgLinkException
from ..util.links_utils import extract_urls, is_telegram_link
from ..util.status_utils import get_readable_message
from .flood import flood_seconds

if TYPE_CHECKING:
    # For ``chat_of``'s signature only; nothing here builds either one.
    from pyrogram import Client
    from pyrogram.types import Chat, Message

STATUS_RESEND_INTERVAL = 8
"""Seconds before the status message may be re-sent to the bottom of the chat.

Inside the window it is edited in place instead. ``/status`` passes
``force=True`` because a user asking for it expects a fresh message.
"""


def chat_of(message: Message) -> Chat:
    """The chat *message* was sent in.

    pyrogram leaves ``Message.chat`` optional -- the empty placeholder it
    answers with for a message it could not fetch has no chat -- but a command
    the bot is handling and a message the bot just sent both came from one. The
    eight ``message.chat.id`` reads around the bot are not the place to argue
    about that, so the assumption is stated here.
    """
    return message.chat  # pyrefly: ignore[bad-return]


def message_media(message: Message):
    """The media *message* carries, or None when it carries none.

    One chain in one order: a document wins over a photo, a photo over a video,
    and so on down to an animation. A message can legitimately hold more than
    one of these, so the order is the answer to "which file is this message
    about" -- and it has to be the same answer everywhere. The callers that pick
    a file to send and the callers that decide whether a message *has* a file
    are looking at the same messages, and two chains that disagreed would mean a
    message counted as having media that then failed to produce any.
    """
    return (
        message.document
        or message.photo
        or message.video
        or message.audio
        or message.voice
        or message.video_note
        or message.sticker
        or message.animation
        or None
    )


def file_link_line(index: int, link: str, name: str) -> str:
    """One numbered, clickable result line for an uploaded file.

    Both halves are escaped, and neither is optional. A name comes off disk, so
    a file called ``<b>x</b>.mkv`` used to arrive as markup; a link to a bucket
    carries ``&`` between its query parameters, which is the same mistake
    spelled differently. Telegram's parser accepts a bare ``&`` often enough
    that only some names and some links broke.

    Escaping is a no-op for the telegram message links this was first written
    for, so the message a leech has always sent is unchanged -- and the only
    length it adds is in the safe direction, where a chunk splits earlier.
    """
    escaped_link = html.escape(link, quote=True)
    return f"{index}. <a href='{escaped_link}'>{html.escape(name)}</a>\n"


async def send_message(message, text, buttons=None, block=True):
    try:
        return await message.reply(
            text=text,
            disable_notification=True,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(flood_seconds(f) * 1.2)
        return await send_message(message, text, buttons, block)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def edit_message(message, text, buttons=None, block=True):
    try:
        return await message.edit(
            text=text,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(flood_seconds(f) * 1.2)
        return await edit_message(message, text, buttons, block)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_notice(
    message: Message, text: str, *, notify: bool = True
) -> Message | str:
    """Send a fresh message to the chat *message* belongs to.

    ``send_message`` replies and stays silent, which is right for status lines
    and for the anchor a batch edits. A final summary is neither: an edit
    notifies nobody, so the summary has to arrive as a new message, and the
    user has to be told about it. The thread id is carried over so a summary
    lands in the same forum topic as the message it replaces.

    Returns the sent message, or the error string -- the same shape
    ``send_message`` uses, so a caller can tell the two apart with ``isinstance``.
    """
    chat_id = chat_of(message).id
    if chat_id is None:
        # ``chat_of`` documents the opposite, but the type still says optional;
        # refusing here keeps the assumption from becoming a TypeError inside
        # pyrogram's argument validation.
        LOGGER.error("Cannot send a notice: the message has no chat")
        return "No chat to send to"
    try:
        return await TgClient.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=getattr(message, "message_thread_id", None),
            disable_notification=not notify,
        )
    except (FloodWait, FloodPremiumWait) as f:
        LOGGER.warning(str(f))
        await sleep(flood_seconds(f) * 1.2)
        return await send_notice(message, text, notify=notify)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_file(message, file, caption=""):
    try:
        return await message.reply_document(
            document=file, caption=caption, disable_notification=True
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(flood_seconds(f) * 1.2)
        return await send_file(message, file, caption)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_rss(text, chat_id, thread_id):
    try:
        return await TgClient.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            disable_notification=True,
        )
    except (FloodWait, FloodPremiumWait) as f:
        LOGGER.warning(str(f))
        await sleep(flood_seconds(f) * 1.2)
        return await send_rss(text, chat_id, thread_id)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def delete_message(message):
    try:
        await message.delete()
    except Exception as e:
        LOGGER.error(str(e))


async def auto_delete_message(cmd_message=None, bot_message=None):
    await sleep(60)
    if cmd_message is not None:
        await delete_message(cmd_message)
    if bot_message is not None:
        await delete_message(bot_message)


async def delete_status():
    async with task_dict_lock:
        for key, data in list(status_dict.items()):
            try:
                await delete_message(data["message"])
                del status_dict[key]
            except Exception as e:
                LOGGER.error(str(e))


MAX_RANGE_MESSAGES = 1000
"""Most messages one range may name."""

RANGE_FETCH_CHUNK = 100
"""Message ids per ``get_messages`` request.

Telegram documents 200; 100 is the comfortable half of that and keeps a failed
request small enough to retry.
"""

_HTTPS_PREFIX = (
    r"(?P<prefix>https://t\.me/(?:(?P<super>c)/)?"
    r"(?P<chat>[^/\s?#]+)/(?P<topic>(?:\d+/)*))"
)
_HTTPS_SINGLE = re_compile(rf"^{_HTTPS_PREFIX}(?P<id>\d+)(?:[?#]\S*)?$")
_HTTPS_RANGE = re_compile(rf"^{_HTTPS_PREFIX}(?P<first>\d+)-(?P<last>\d+)$")
_TG_PREFIX = r"(?P<prefix>tg://openmessage\?user_id=(?P<chat>-?\d+)&message_id=)"
_TG_SINGLE = re_compile(rf"^{_TG_PREFIX}(?P<id>\d+)(?:&[^#\s]*)?$")
_TG_RANGE = re_compile(rf"^{_TG_PREFIX}(?P<first>\d+)-(?P<last>\d+)$")

_RANGE_SEPARATORS = ("-https://t.me/", "-tg://openmessage?")


@dataclass(frozen=True)
class TgLink:
    """One telegram address, resolved into the pieces a range needs.

    ``prefix`` is everything before the message id, which is what lets one
    address become one link per id without guessing the chat, the ``c/`` form
    or a forum topic again.
    """

    chat: int | str
    first_id: int
    last_id: int
    scheme: str
    prefix: str
    is_range: bool

    @property
    def ids(self) -> range:
        return range(self.first_id, self.last_id + 1)

    @property
    def count(self) -> int:
        return self.last_id - self.first_id + 1

    def link_for(self, message_id: int) -> str:
        return f"{self.prefix}{message_id}"


def _address_of(text: str) -> str:
    """The address at the front of *text*, without surrounding punctuation.

    ``/leech`` hands this function either a bare link or a link followed by the
    mode keyword; the thumbnail and ffmpeg callers append their own text. Only
    the first token is ever the address.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.split(maxsplit=1)[0].rstrip(".,;:!?\"')]}")


def _chat_id(raw: str, *, tg: bool) -> int | str:
    """The peer id a link names, in the form pyrogram expects.

    The ``c/`` form carries the channel id without telegram's ``-100`` marker;
    a username is not a number at all and goes back as a string.
    """
    if tg:
        return int(raw)
    if raw.isdigit():
        return int(f"-100{raw}")
    return raw


def _parse_single_tg_link(address: str) -> TgLink | None:
    if match := _HTTPS_SINGLE.match(address):
        message_id = int(match["id"])
        if message_id < 1:
            return None
        return TgLink(
            chat=_chat_id(match["chat"], tg=False),
            first_id=message_id,
            last_id=message_id,
            scheme="https",
            prefix=match["prefix"],
            is_range=False,
        )
    if match := _TG_SINGLE.match(address):
        message_id = int(match["id"])
        if message_id < 1:
            return None
        return TgLink(
            chat=int(match["chat"]),
            first_id=message_id,
            last_id=message_id,
            scheme="tg",
            prefix=match["prefix"],
            is_range=False,
        )
    return None


def _parse_range_address(address: str) -> TgLink | None:
    if match := _TG_RANGE.match(address):
        scheme, tg = "tg", True
    elif match := _HTTPS_RANGE.match(address):
        scheme, tg = "https", False
    else:
        return None
    first_id, last_id = sorted((int(match["first"]), int(match["last"])))
    if first_id < 1:
        return None
    return TgLink(
        chat=_chat_id(match["chat"], tg=tg),
        first_id=first_id,
        last_id=last_id,
        scheme=scheme,
        prefix=match["prefix"],
        is_range=True,
    )


def _split_two_links(address: str) -> tuple[str, str] | None:
    for separator in _RANGE_SEPARATORS:
        index = address.find(separator)
        if index > 0:
            return address[:index], address[index + 1 :]
    return None


def _merge_two_links(pair: tuple[str, str]) -> TgLink:
    left = _parse_single_tg_link(pair[0])
    right = _parse_single_tg_link(pair[1])
    if left is None or right is None:
        raise TgLinkException("Wrong link format!")
    if left.scheme != right.scheme or left.chat != right.chat:
        raise TgLinkException("The two links in a range must point to the same chat.")
    if left.prefix != right.prefix:
        raise TgLinkException("The two links in a range must point to the same topic.")
    first_id, last_id = sorted((left.first_id, right.first_id))
    return TgLink(
        chat=left.chat,
        first_id=first_id,
        last_id=last_id,
        scheme=left.scheme,
        prefix=left.prefix,
        is_range=True,
    )


def parse_tg_link(address: str) -> TgLink:
    """Resolve a single link, a short range, or two full links joined by ``-``.

    Both ends of a range are normalized to ``first..last``, so a range written
    backwards names the same messages as one written forwards.
    """
    pair = _split_two_links(address)
    parsed = (
        _merge_two_links(pair)
        if pair
        else _parse_range_address(address) or _parse_single_tg_link(address)
    )
    if parsed is None:
        raise TgLinkException("Wrong link format!")
    if parsed.count > MAX_RANGE_MESSAGES:
        raise TgLinkException(
            f"Message range is too large: {parsed.count} "
            f"(max {MAX_RANGE_MESSAGES})."
        )
    return parsed


def split_tg_command_text(text: str) -> tuple[str, str | None]:
    """Split a command's telegram input into ``(address, mode keyword)``.

    Only a positional token can be the keyword: ``arg_parser`` never folds a
    flag into the link, so a ``-`` prefix here cannot be intended as one.
    """
    parts = text.split()
    if len(parts) > 1 and is_telegram_link(parts[0]) and not parts[1].startswith("-"):
        return parts[0], " ".join(parts[1:])
    return text, None


async def get_tg_link_message(
    link: str, user_id: int | None = None
) -> tuple[Message | list[str], str]:
    """Resolve a telegram address into what a leech task can act on.

    A single message is returned as the message itself; a range becomes one
    link per id, which the caller dispatches as a bulk.
    """
    parsed = parse_tg_link(_address_of(link))
    if parsed.is_range:
        session = await _probe_session(parsed, user_id)
        return [parsed.link_for(message_id) for message_id in parsed.ids], session
    return await _fetch_single(parsed, user_id)


async def get_tg_link_batch(
    text: str, user_id: int | None = None
) -> tuple[Message | list[str], str]:
    """Resolve the telegram input of a leech command.

    With only an address this is :func:`get_tg_link_message`. A trailing word
    switches to links mode: the range is read, and every URL in its messages is
    returned in place of the message links, so the caller dispatches them as an
    ordinary bulk and never touches the range's own media.
    """
    address, keyword = split_tg_command_text(text)
    if keyword is None:
        return await get_tg_link_message(address, user_id)
    links, session = await _scan_range_urls(parse_tg_link(address), user_id)
    return links, session


async def _fetch_single(
    parsed: TgLink, user_id: int | None
) -> tuple[Message, str]:
    """Read one message: the bot first, the requester's session second."""
    user_client = await get_user_client(user_id) or TgClient.user
    if parsed.scheme == "tg":
        if not user_client:
            raise TgLinkException("USER_SESSION_STRING required for this private link!")
        return await _fetch_with_user(user_client, parsed, parsed.first_id)
    try:
        message = await TgClient.bot.get_messages(
            chat_id=parsed.chat, message_ids=parsed.first_id
        )
        if message is not None and not message.empty:
            return message, "bot"
    except Exception as e:
        if not user_client:
            raise e
    if not user_client:
        raise TgLinkException("Private: Can't get this message!")
    return await _fetch_with_user(user_client, parsed, parsed.first_id)


async def _fetch_with_user(
    client: Client, parsed: TgLink, message_id: int
) -> tuple[Message, str]:
    try:
        message = await client.get_messages(
            chat_id=parsed.chat, message_ids=message_id
        )
    except Exception as e:
        raise TgLinkException(
            f"You don't have access to this chat!. ERROR: {e}"
        ) from e
    if message is not None and not message.empty:
        return message, "user"
    raise TgLinkException("Private: Can't get this message!")


async def _probe_session(parsed: TgLink, user_id: int | None) -> str:
    """Choose the reader for a range without fetching the whole range.

    The first id is the natural probe, but a deleted first message must not
    condemn a range the rest of which is readable, hence the second probe at
    the other end.
    """
    user_client = await get_user_client(user_id) or TgClient.user
    if parsed.scheme == "tg":
        if not user_client:
            raise TgLinkException("USER_SESSION_STRING required for this private link!")
        await _probe_user_range(user_client, parsed)
        return "user"
    try:
        for message_id in dict.fromkeys((parsed.first_id, parsed.last_id)):
            message = await TgClient.bot.get_messages(
                chat_id=parsed.chat, message_ids=message_id
            )
            if message is not None and not message.empty:
                return "bot"
    except Exception as e:
        if not user_client:
            raise e
    if user_client:
        await _probe_user_range(user_client, parsed)
        return "user"
    raise TgLinkException("Private: Can't get this message!")


async def _probe_user_range(client: Client, parsed: TgLink) -> None:
    for message_id in dict.fromkeys((parsed.first_id, parsed.last_id)):
        try:
            message = await client.get_messages(
                chat_id=parsed.chat, message_ids=message_id
            )
        except Exception as e:
            raise TgLinkException(
                f"You don't have access to this chat!. ERROR: {e}"
            ) from e
        if message is not None and not message.empty:
            return
    raise TgLinkException("Private: Can't get this message!")


async def _scan_range_urls(
    parsed: TgLink, user_id: int | None
) -> tuple[list[str], str]:
    """Every URL in the messages of *parsed*, in message order.

    Nothing here downloads: the messages are read for their text and captions,
    and the media they carry is left alone.
    """
    messages, session = await _range_messages(parsed, user_id)
    if not messages:
        raise TgLinkException("Can't read the messages in this range!")
    urls: list[str] = []
    for message in sorted(messages, key=lambda item: item.id):
        urls.extend(_message_urls(message))
    unique = list(dict.fromkeys(urls))
    if not unique:
        raise TgLinkException("No links found in the selected messages.")
    return unique, session


async def _range_messages(
    parsed: TgLink, user_id: int | None
) -> tuple[list[Message], str]:
    """Read a range with the bot, falling back to the requester's session."""
    user_client = await get_user_client(user_id) or TgClient.user
    if parsed.scheme == "tg":
        if not user_client:
            raise TgLinkException("USER_SESSION_STRING required for this private link!")
        messages, _ = await _fetch_range(user_client, parsed)
        return messages, "user"
    try:
        messages, missing = await _fetch_range(TgClient.bot, parsed)
    except Exception as e:
        if not user_client:
            raise TgLinkException(
                f"You don't have access to this chat!. ERROR: {e}"
            ) from e
        messages, _ = await _fetch_range(user_client, parsed)
        return messages, "user"
    if missing and user_client:
        # A bot in a channel sees only what was posted while it was a member;
        # the requester's session can read the rest.
        extra, _ = await _fetch_range(user_client, parsed, missing)
        if extra:
            messages.extend(extra)
            return messages, "user"
    return messages, "bot"


async def _fetch_range(
    client: Client, parsed: TgLink, ids: Sequence[int] | None = None
) -> tuple[list[Message], list[int]]:
    """Read the named ids, returning ``(messages, missing ids)``.

    ``missing`` is what the caller may retry with another client: a deleted
    message and a post the bot cannot see look alike here, and only the retry
    can tell them apart.
    """
    wanted = list(parsed.ids) if ids is None else list(ids)
    messages: list[Message] = []
    missing: list[int] = []
    for index in range(0, len(wanted), RANGE_FETCH_CHUNK):
        chunk = wanted[index : index + RANGE_FETCH_CHUNK]
        answer = await client.get_messages(chat_id=parsed.chat, message_ids=chunk)
        returned = {
            message.id: message
            for message in answer or ()
            if message is not None and not message.empty
        }
        messages.extend(returned.values())
        missing.extend(message_id for message_id in chunk if message_id not in returned)
    return messages, missing


def _message_urls(message: Message) -> list[str]:
    """Every URL a message carries, not only the ones visible in its text."""
    urls = extract_urls(getattr(message, "text", None))
    urls.extend(extract_urls(getattr(message, "caption", None)))
    for attr in ("entities", "caption_entities"):
        for entity in getattr(message, attr, None) or ():
            if url := getattr(entity, "url", None):
                urls.append(url)
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or ():
        for button in row:
            if url := getattr(button, "url", None):
                urls.append(url)
    return urls


async def temp_download(msg):
    path = f"{DOWNLOAD_DIR}temp"
    return await msg.download(file_name=f"{path}/")


def cancel_status_interval(sid):
    """Stop the ticker that refreshes the status message of *sid*."""
    if obj := intervals["status"].get(sid):
        obj.cancel()
        del intervals["status"][sid]


def _drop_status(sid):
    """Forget the status message of *sid* and stop refreshing it.

    Every path that gives up on a status message -- nothing left to report,
    Telegram refusing the edit -- has to do both, and dropping the entry while
    leaving the ticker behind means it keeps firing against a message that is no
    longer tracked.
    """
    del status_dict[sid]
    cancel_status_interval(sid)


async def _render_status(sid, is_user):
    """The status text and buttons for *sid*, on the page it was left at."""
    data = status_dict[sid]
    return await get_readable_message(
        sid, is_user, data["page_no"], data["status"], data["page_step"]
    )


async def _send_status(msg, sid, text, buttons):
    """Send a status message as a reply to *msg*, or None if Telegram refused.

    ``.text`` is kept on the returned message because the update path compares
    against it to decide whether an edit is worth a request.
    """
    message = await send_message(msg, text, buttons, block=False)
    if isinstance(message, str):
        LOGGER.error(f"Status with id: {sid} haven't been sent. Error: {message}")
        return None
    message.text = text
    return message


async def update_status_message(sid, force=False):
    if intervals["stopAll"]:
        return
    async with task_dict_lock:
        if not status_dict.get(sid):
            cancel_status_interval(sid)
            return
        if not force and time() - status_dict[sid]["time"] < 3:
            return
        status_dict[sid]["time"] = time()
        text, buttons = await _render_status(sid, status_dict[sid]["is_user"])
        if text is None:
            _drop_status(sid)
            return
        if text != status_dict[sid]["message"].text:
            message = await edit_message(
                status_dict[sid]["message"], text, buttons, block=False
            )
            if isinstance(message, str):
                if message.startswith("Telegram says: [40"):
                    _drop_status(sid)
                else:
                    LOGGER.error(
                        f"Status with id: {sid} haven't been updated. Error: {message}"
                    )
                return
            status_dict[sid]["message"].text = text
            status_dict[sid]["time"] = time()


async def send_status_message(msg, user_id=0, force=False):
    if intervals["stopAll"]:
        return
    sid = user_id or msg.chat.id
    is_user = bool(user_id)
    throttled = False
    async with task_dict_lock:
        if sid in status_dict:
            # every task asks for the status message when it starts, and a bulk
            # starts a hundred of them within a few seconds. Re-sending here
            # costs a send plus a delete each time, which is what earns the
            # FloodWait that then stalls the batch, so outside the window the
            # existing message is edited in place instead of being replaced.
            if (
                not force
                and time() - status_dict[sid].get("sent_at", 0) < STATUS_RESEND_INTERVAL
            ):
                throttled = True
            else:
                text, buttons = await _render_status(sid, is_user)
                if text is None:
                    _drop_status(sid)
                    return
                old_message = status_dict[sid]["message"]
                message = await _send_status(msg, sid, text, buttons)
                if message is None:
                    return
                await delete_message(old_message)
                status_dict[sid].update(
                    {"message": message, "time": time(), "sent_at": time()}
                )
        else:
            text, buttons = await get_readable_message(sid, is_user)
            if text is None:
                return
            message = await _send_status(msg, sid, text, buttons)
            if message is None:
                return
            status_dict[sid] = {
                "message": message,
                "time": time(),
                "sent_at": time(),
                "page_no": 1,
                "page_step": 1,
                "status": "All",
                "is_user": is_user,
            }
        if not intervals["status"].get(sid) and not is_user:
            intervals["status"][sid] = SetInterval(
                Config.STATUS_UPDATE_INTERVAL, update_status_message, sid
            )
    if throttled:
        # locks are not reentrant, so the edit happens after the release
        await update_status_message(sid)
