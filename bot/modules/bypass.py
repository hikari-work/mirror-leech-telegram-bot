from asyncio import wait_for, TimeoutError as AsyncTimeoutError
from re import sub as re_sub, search as re_search
from urllib.parse import urlparse

from aiofiles import open as aiopen
from aiofiles.os import remove, path as aiopath
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from .. import bot_loop
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.exceptions import DirectDownloadLinkException
from ..helper.mirror_leech_utils.download_utils.bypass_dispatcher import bypass_scrape
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
)

_PAGE_TIMEOUT = 120


def _slug(title):
    s = re_sub(r"[^a-zA-Z0-9_-]+", "", title.replace(" ", "_"))[:60]
    return s or "bypass"


def _extract_page_from_url(url):
    """Extract page number from URL path like /page-2205. Returns int or 1."""
    m = re_search(r"/page-(\d+)", urlparse(url).path)
    return int(m.group(1)) if m else 1


def _format_page_list(text, total_pages):
    """Convert user input into a pageList string for the backend.

    Accepts: '0' (all), '3' (single), '1-5' (range),
    '1,3,7' (comma-separated), or combos like '1-3,7,9-10'.
    Returns the spec string as-is for the backend, except '0' which
    expands to '1-<total_pages>'.
    """
    text = text.strip()
    if text == "0":
        return f"1-{total_pages}"
    return text


async def _ask_reply(client, message, user_id):
    """Wait for user's next text message in the same chat. Returns text or None."""
    future = bot_loop.create_future()

    async def event_filter(_, __, event):
        user = event.from_user
        return bool(
            user
            and user.id == user_id
            and event.chat.id == message.chat.id
            and event.text
        )

    async def catcher(_, event):
        if not future.done():
            future.set_result(event)

    handler = client.add_handler(
        MessageHandler(catcher, filters=create(event_filter)), group=-1
    )
    try:
        reply = await wait_for(future, _PAGE_TIMEOUT)
    except AsyncTimeoutError:
        reply = None
    finally:
        client.remove_handler(*handler)

    if reply is None:
        return None
    text = reply.text.strip()
    await delete_message(reply)
    return text


@new_task
async def bypass_scrape_cmd(client, message):
    args = message.text.split(maxsplit=2)
    link = args[1] if len(args) > 1 else ""
    keyword = args[2].strip() if len(args) > 2 else ""
    if not link and (reply_to := message.reply_to_message):
        link = reply_to.text.split(maxsplit=1)[0].strip()

    if not link:
        await send_message(
            message,
            "Send a thread URL with the command or reply to a message with the URL.\n"
            "<code>/bypass &lt;url&gt; [filter]</code>",
        )
        return

    user_id = message.from_user.id if message.from_user else 0

    probe_page = str(_extract_page_from_url(link))
    status = await send_message(message, "Fetching thread info...")
    try:
        title, _, total_pages = await bypass_scrape(link, probe_page, keyword)
    except DirectDownloadLinkException as e:
        await edit_message(status, str(e))
        return

    await edit_message(
        status,
        f"<b>{title}</b>\n"
        f"Total pages: {total_pages}\n\n"
        "Kirim halaman yang ingin di-scrape:\n"
        "<code>0</code> = semua, <code>1-10</code> = range, <code>1,5,7</code> = pilihan",
    )

    page_text = await _ask_reply(client, message, user_id)
    if page_text is None:
        await edit_message(status, "Timeout. Tidak ada halaman yang dipilih.")
        return

    page_list = _format_page_list(page_text, total_pages)
    if not page_list:
        await edit_message(status, "Halaman tidak valid.")
        return

    await edit_message(status, f"Scraping pages {page_list}...")
    try:
        title, all_links, _ = await bypass_scrape(link, page_list, keyword)
    except DirectDownloadLinkException as e:
        await edit_message(status, f"Error: {e}")
        return

    if not all_links:
        await edit_message(status, "Tidak ada link ditemukan.")
        return

    page_label = page_list

    path = f"{_slug(title)}_links.txt"
    async with aiopen(path, "w") as f:
        await f.write("\n".join(all_links))

    caption = (
        f"<b>{title}</b>\n"
        f"Pages: {page_label} | Links: {len(all_links)}"
    )
    if keyword:
        caption += f" | Filter: <code>{keyword}</code>"
    await send_file(message, path, caption=caption)
    await delete_message(status)
    if await aiopath.exists(path):
        await remove(path)
