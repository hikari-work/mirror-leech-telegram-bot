"""Leech the media out of a chat that has no link to give.

``/leech`` reaches telegram through one of two doors: a link, or a reply to a
message the bot can read. A private chat -- a DM with a person, a DM with
another bot -- has neither. Telegram writes no link for one, so there is nothing
to paste, and the bot is not a party to it, so there is nothing to reply to. The
only address that names such a chat is ``tg://openmessage?user_id=X&message_id=Y``,
which the user would have to assemble by hand, message id included.

This command is the missing door: name the chat and how far back to look, and it
turns that into the links the rest of the bot already knows how to take apart.

    /pleech @somebot 10        the last 10 messages of that DM, all of them
                              that carry media

What it produces is an ordinary bulk: one ``Leech`` task per media message,
sharing a batch anchor, merged into one folder, options and copy presets and
destination all behaving as they do for ``-b``. Nothing downstream of the
dispatch is aware that the links were built rather than typed -- which is the
point. The alternative, teaching ``TelegramDownloadHelper`` about a second kind
of source, would be a second copy of the download, upload and recovery paths,
and the two would drift.

A chat the bot can read itself needs no user session at all; one it cannot is
read with the requester's own, which is what ``/login`` is for.
"""

from html import escape

from .. import LOGGER, bot_loop
from ..core.telegram_manager import TgClient, get_user_client, own_account
from ..helper.telegram.bot_commands import BotCommands
from ..helper.telegram.message_utils import message_media, send_message
from .leech import Leech

MAX_MESSAGES = 100
"""Most messages one command may ask to scan.

Telegram answers a history request with at most 100 messages, so a larger count
cannot be honoured and silently returning 100 would be a lie about what was
looked at. Refusing says so instead.
"""

CHANNEL_PREFIX = 1000000000000
"""Smallest magnitude a channel or supergroup id can have.

Telegram builds those as ``-100`` followed by the channel's own id, so the
magnitude alone separates them from the shorter negative ids of ordinary
groups. The link form depends on it: only a channel can be addressed as
``https://t.me/c/<stripped id>/<message>``, and a plain group -- whose id is
already the peer id -- has to go through the ``tg://`` form like a DM.
"""

USAGE = f"""<b>Leech from a private chat that has no link</b>

<code>/pleech chat count</code>
The last <b>count</b> messages of <b>chat</b> are scanned and every one that
carries a file is leeched, as a single batch.

<code>chat</code>: <code>@username</code>, a numeric chat id, or <code>me</code>
for your Saved Messages.
<code>count</code>: how many messages back to look, 1 to {MAX_MESSAGES}.

Options go after them, exactly as they do for /leech:
<code>/pleech @somebot 10 -c mypreset</code>

A chat the bot cannot read is read with your own session, so log in with
/login first. Everything else at the source stays untouched: files sent
straight to the bot are still leeched by replying /leech to them."""


class SourceError(Exception):
    """The chat cannot be read, and the message is meant for the user.

    Separate from the ``ValueError`` :func:`parse_spec` raises because the two
    failures want different answers: a mistyped command deserves the usage text,
    while a chat telegram will not open deserves the reason and nothing else.
    """


def parse_spec(tokens):
    """Split the tokens after the command into ``(chat, count, options)``.

    Raises ``ValueError`` with something worth reading when they do not fit, so
    the caller can hand the reason and the usage text to the user together.
    """
    if len(tokens) < 2:
        raise ValueError("Chat and message count are both required.")
    chat, raw_count = tokens[0], tokens[1]
    try:
        count = int(raw_count)
    except ValueError:
        raise ValueError(
            f"<code>{escape(raw_count)}</code> is not a message count."
        ) from None
    if not 1 <= count <= MAX_MESSAGES:
        raise ValueError(
            f"Message count must be between 1 and {MAX_MESSAGES}, not {count}."
        )
    return chat, count, " ".join(tokens[2:])


def link_for(chat_id, message_id, *, bot_readable, username):
    """The link that points at one message of a chat.

    Which of the three forms comes out is not cosmetic, because they are not
    read alike: ``get_tg_link_message`` sends a ``tg://`` link to the user
    session and nothing else, while on an ``https://t.me`` one it asks the bot
    first and only falls back. So a chat the bot can read gets an https link --
    that is what lets ``/pleech`` work on the bot's own PM, or on a public
    channel, without the requester having logged in at all.
    """
    if bot_readable and username:
        return f"https://t.me/{username}/{message_id}"
    if bot_readable and chat_id <= -CHANNEL_PREFIX:
        # the ``-100`` is telegram's own marker, and the ``c/`` form leaves it
        # off; get_tg_link_message puts it back
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return f"tg://openmessage?user_id={chat_id}&message_id={message_id}"


async def _history(client, chat_id, count):
    """The last *count* messages of a chat, oldest first.

    ``get_chat_history`` is an async iterator that yields newest first; the
    reversal is what makes a batch upload in the order the files were posted.
    """
    messages = [msg async for msg in client.get_chat_history(chat_id, limit=count)]
    messages.reverse()
    return messages


async def resolve_source(listener, chat_spec, count):
    """Read the last *count* messages of *chat_spec*.

    Returns ``(chat_id, username, bot_readable, messages)``, or raises
    :class:`SourceError` with something to tell the user.

    The bot is asked first and the requester's own session second, which is the
    order that keeps a logged-out user working on every chat the bot is party
    to. The username comes back because it is the only handle that survives into
    a link the bot can read without a session.
    """
    if chat_spec.lower() == "me":
        client = await get_user_client(listener.user_id) or TgClient.user
        if client is None:
            raise SourceError(
                "Saved Messages is only reachable through your own session. "
                "Log in with /login first."
            )
        me = own_account(client)
        return me.id, None, False, await _history(client, me.id, count)

    try:
        chat = await TgClient.bot.get_chat(chat_spec)
        messages = await _history(TgClient.bot, chat.id, count)
        return chat.id, chat.username, True, messages
    except Exception as e:
        # Not an error yet: a DM with somebody else is exactly the case the bot
        # was never going to be able to read, and it is the case this command
        # exists for. Only if the user's own session fails too does this become
        # something to report.
        LOGGER.info(f"/pleech: the bot cannot read {chat_spec}: {e}")

    client = await get_user_client(listener.user_id) or TgClient.user
    if client is None:
        raise SourceError(
            f"The bot cannot read <b>{escape(chat_spec)}</b>, and you have no "
            "session of your own to read it with. Log in with /login, or use a "
            "chat the bot is already in."
        )
    try:
        chat = await client.get_chat(chat_spec)
        return chat.id, chat.username, False, await _history(client, chat.id, count)
    except Exception as e:
        raise SourceError(f"Could not read <b>{escape(chat_spec)}</b>: {e}") from e


class PrivateLeech(Leech):
    """``/pleech``: look the media up first, then run the ordinary bulk.

    A ``Leech`` rather than a ``CommandTask`` for one reason: ``_handle_bulk``
    is where a batch of telegram links becomes a batch of tasks, and it already
    does everything this needs -- the anchor, the ``-m`` that merges the batch
    into one folder, the propagation of the options, the book-keeping that lets
    the batch be cancelled. Nothing here downloads, so none of the leech
    argument handling is reached.
    """

    async def new_event(self):
        tokens = [t for t in self.cmd_text.split("\n")[0].split(" ") if t]

        try:
            chat_spec, count, self.options = parse_spec(tokens[1:])
        except ValueError as e:
            await send_message(self.message, f"{e}\n\n{USAGE}")
            return

        try:
            chat_id, username, bot_readable, messages = await resolve_source(
                self, chat_spec, count
            )
        except SourceError as e:
            await send_message(self.message, str(e))
            return
        except Exception as e:
            LOGGER.error(f"/pleech failed to read {chat_spec}: {e}")
            await send_message(self.message, f"ERROR: {e}")
            return

        message_ids = [msg.id for msg in messages if message_media(msg) is not None]
        if not message_ids:
            await send_message(
                self.message,
                f"No media in the last {count} messages of <b>{escape(chat_spec)}</b>.",
            )
            return

        links = [
            link_for(chat_id, message_id, bot_readable=bot_readable, username=username)
            for message_id in message_ids
        ]
        LOGGER.info(
            f"/pleech {chat_spec}: starting {len(links)} task(s) from the last "
            f"{count} messages"
        )
        await self._handle_bulk(links, self.options, BotCommands.LeechCommand[0])


async def private_leech(client, message):
    bot_loop.create_task(PrivateLeech(client, message).new_event())
