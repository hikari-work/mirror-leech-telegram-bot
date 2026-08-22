from aiofiles import open as aiopen
from aiofiles.os import remove


def filter_links(links_list, bulk_start, bulk_end):
    """Select ``bulk_start``..``bulk_end`` of the list, 1-based and inclusive.

    The range is what a person counting lines in the replied-to message means:
    ``-b 3401:3500`` is the 3401st link through the 3500th, so 100 links. Handing
    the two numbers straight to a slice instead -- which is what this did -- made
    both ends off by one in the same direction: the window started at the 3402nd
    link and held 99 of them, and nothing in the batch counter hinted at why.

    ``0`` (the parser's "not given") still means "no bound on this side".
    """
    start = bulk_start - 1 if bulk_start > 0 else None
    end = bulk_end if bulk_end > 0 else None
    return links_list[start:end]


def get_links_from_message(text):
    links_list = text.split("\n")
    return [item.strip() for item in links_list if len(item) != 0]


async def get_links_from_file(message):
    links_list = []
    text_file_dir = await message.download()
    async with aiopen(text_file_dir, "r+") as f:
        lines = await f.readlines()
        links_list.extend(line.strip() for line in lines if len(line) != 0)
    await remove(text_file_dir)
    return links_list


async def extract_bulk_links(message, bulk_start, bulk_end):
    bulk_start = int(bulk_start)
    bulk_end = int(bulk_end)
    links_list = []
    if reply_to := message.reply_to_message:
        if (file_ := reply_to.document) and (file_.mime_type == "text/plain"):
            links_list = await get_links_from_file(reply_to)
        elif text := reply_to.text:
            links_list = get_links_from_message(text)
    return filter_links(links_list, bulk_start, bulk_end) if links_list else links_list
