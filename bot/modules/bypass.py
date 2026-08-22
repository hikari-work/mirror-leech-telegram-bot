from html import escape
from re import sub as re_sub
from urllib.parse import urlparse

from aiofiles import open as aiopen
from aiofiles.os import remove, path as aiopath

from ..helper.ext_utils.bot_utils import new_task, sync_to_async
from ..helper.ext_utils.exceptions import DirectDownloadLinkException
from ..helper.mirror_leech_utils.download_utils.bypass_dispatcher import (
    bypass_scrape,
    is_scrape_target,
)
from ..helper.mirror_leech_utils.download_utils.url_shortener_bypass import (
    bypass_shortener,
    is_url_shortener,
)
from ..helper.telegram_helper.conversation import wait_for_reply
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
    return await wait_for_reply(client, message, user_id, _PAGE_TIMEOUT)


async def _resolve_shortlink(status, link):
    """Resolve ``link`` when it is a shortlink; otherwise hand it back untouched.

    Returns (link, done). ``done`` means the user has already been answered and
    there is nothing left to scrape: either the bypass failed, or it succeeded
    and no thread scraper handles the target, in which case the target URL is
    the whole answer.
    """
    if not is_url_shortener(urlparse(link).hostname or ""):
        return link, False

    try:
        target = await sync_to_async(bypass_shortener, link)
    except DirectDownloadLinkException as e:
        await edit_message(status, str(e))
        return link, True

    if not is_scrape_target(target):
        await edit_message(status, f"<b>Bypassed</b>\n<code>{escape(target)}</code>")
        return target, True
    return target, False


async def _deliver_links(message, status, title, all_links, page_list, keyword):
    """Send the scraped links as a .txt and drop the status message."""
    path = f"{_slug(title)}_links.txt"
    async with aiopen(path, "w") as f:
        await f.write("\n".join(all_links))

    caption = f"<b>{title}</b>\nPages: {page_list} | Links: {len(all_links)}"
    if keyword:
        caption += f" | Filter: <code>{keyword}</code>"
    await send_file(message, path, caption=caption)
    await delete_message(status)
    if await aiopath.exists(path):
        await remove(path)


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
            "Send a shortlink or thread URL with the command, or reply to a message "
            "with the URL.\n"
            "<code>/bypass &lt;url&gt; [filter]</code>\n\n"
            "A shortlink is answered with its target URL; a thread URL asks which "
            "pages to scrape. <code>filter</code> only applies to threads.",
        )
        return

    user_id = message.from_user.id if message.from_user else 0

    status = await send_message(message, "Bypassing link...")

    link, done = await _resolve_shortlink(status, link)
    if done:
        return

    probe_page = "1"  # only need pages_total; page 1 is cheapest and always valid
    await edit_message(status, "Fetching thread info...")
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
        "<code>0</code> = semua, <code>1-10</code> = range, "
        "<code>1,5,7</code> = pilihan",
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

    await _deliver_links(message, status, title, all_links, page_list, keyword)
