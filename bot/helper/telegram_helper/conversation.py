"""Waiting for the next message a user sends.

Five places ask a question in the chat and then have to catch the answer:
``/login`` and ``/bypass`` read the reply themselves, while the bot, user and rss
menus hand it to a handler and poll a module-level dict until that handler says
it is done. Both shapes come down to the same three steps -- register a message
handler in group ``-1`` restricted to the one user in the one chat, wait with a
timeout, then remove it again -- and the five copies had already drifted over
which message types count as an answer and over whether a missing sender is
tolerated.
"""

from asyncio import sleep, wait_for
from time import time

from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from ... import bot_loop
from .message_utils import delete_message


def _message_filter(user_id, chat_id, media=("text",)):
    """A pyrogram predicate for a message from *user_id* in *chat_id*.

    *media* names the ``Message`` attributes that count as an answer, so a prompt
    asking for a thumbnail is not satisfied by a caption.
    """

    async def event_filter(_, __, event):
        user = event.from_user or event.sender_chat
        return bool(
            user
            and user.id == user_id
            and event.chat.id == chat_id
            and any(getattr(event, attr, None) for attr in media)
        )

    return event_filter


async def wait_for_reply(client, message, user_id, timeout=60):
    """The text of the next message *user_id* sends in this chat, or None.

    The reply is deleted as soon as it is read: what the callers ask for is a
    phone number, a login code or a page list, none of which should stay in the
    chat. A timeout answers None instead of raising, because each caller turns it
    into a message of its own.
    """
    future = bot_loop.create_future()

    async def catcher(_, event):
        if not future.done():
            future.set_result(event)

    handler = client.add_handler(
        MessageHandler(
            catcher, filters=create(_message_filter(user_id, message.chat.id))
        ),
        group=-1,
    )
    try:
        reply = await wait_for(future, timeout)
    except TimeoutError:
        return None
    finally:
        client.remove_handler(*handler)

    text = reply.text.strip()
    await delete_message(reply)
    return text


async def wait_for_message(
    client,
    query,
    pfunc,
    handler_dict,
    key,
    media=("text",),
    on_timeout=None,
    timeout=60,
):
    """Route the next message of the user who pressed *query* to *pfunc*.

    The menus hand the answer to *pfunc* rather than reading it here, so the wait
    ends when that handler clears ``handler_dict[key]`` -- or when the next
    button press does, which is how a menu cancels a prompt it has replaced.
    *on_timeout* is what to fall back to when neither happens in time, usually
    redrawing the menu the prompt came from.
    """
    handler_dict[key] = True
    start_time = time()

    handler = client.add_handler(
        MessageHandler(
            pfunc,
            filters=create(
                _message_filter(query.from_user.id, query.message.chat.id, media)
            ),
        ),
        group=-1,
    )
    while handler_dict[key]:
        await sleep(0.5)
        if time() - start_time > timeout:
            handler_dict[key] = False
            if on_timeout is not None:
                await on_timeout()
    client.remove_handler(*handler)
