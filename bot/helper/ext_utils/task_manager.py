from asyncio import Event

from ... import (
    queued_dl,
    queued_up,
    non_queued_up,
    non_queued_dl,
    upload_chat_of,
    queue_dict_lock,
)
from ...core.config_manager import Config


def upload_chat_id(listener):
    """Chat that an upload counts against for the per-chat upload limit."""
    return str(listener.up_dest or listener.message.chat.id)


def _chat_up_count(chat_id):
    return sum(1 for mid in non_queued_up if upload_chat_of.get(mid) == chat_id)


async def check_running_tasks(listener, state="dl"):
    all_limit = Config.QUEUE_ALL
    state_limit = Config.QUEUE_DOWNLOAD if state == "dl" else Config.QUEUE_UPLOAD
    event = None
    is_over_limit = False
    chat_id = upload_chat_id(listener) if state == "up" else None
    async with queue_dict_lock:
        if state == "up" and listener.mid in non_queued_dl:
            non_queued_dl.remove(listener.mid)
        if (
            (all_limit or state_limit)
            and not listener.force_run
            and not (listener.force_upload and state == "up")
            and not (listener.force_download and state == "dl")
        ):
            dl_count = len(non_queued_dl)
            up_count = len(non_queued_up)
            # Upload limit is enforced per destination chat, not globally.
            t_count = dl_count if state == "dl" else _chat_up_count(chat_id)
            is_over_limit = (
                all_limit
                and dl_count + up_count >= all_limit
                and (not state_limit or t_count >= state_limit)
            ) or (state_limit and t_count >= state_limit)
            if is_over_limit:
                event = Event()
                if state == "dl":
                    queued_dl[listener.mid] = event
                else:
                    queued_up[listener.mid] = event
                    upload_chat_of[listener.mid] = chat_id
        if not is_over_limit:
            if state == "up":
                upload_chat_of[listener.mid] = chat_id
                non_queued_up.add(listener.mid)
            else:
                non_queued_dl.add(listener.mid)

    return is_over_limit, event


async def start_dl_from_queued(mid: int):
    queued_dl[mid].set()
    del queued_dl[mid]
    non_queued_dl.add(mid)


async def start_up_from_queued(mid: int):
    queued_up[mid].set()
    del queued_up[mid]
    non_queued_up.add(mid)


async def start_from_queued():
    all_limit = Config.QUEUE_ALL
    up_limit = Config.QUEUE_UPLOAD
    dl_limit = Config.QUEUE_DOWNLOAD
    async with queue_dict_lock:
        # Uploads: limit is per destination chat, not global.
        if queued_up:
            for mid in list(queued_up.keys()):
                if all_limit and len(non_queued_dl) + len(non_queued_up) >= all_limit:
                    break
                chat_id = upload_chat_of.get(mid)
                if up_limit and _chat_up_count(chat_id) >= up_limit:
                    continue
                await start_up_from_queued(mid)

        # Downloads: unchanged global limit behaviour.
        if queued_dl:
            for mid in list(queued_dl.keys()):
                if all_limit and len(non_queued_dl) + len(non_queued_up) >= all_limit:
                    break
                if dl_limit and len(non_queued_dl) >= dl_limit:
                    break
                await start_dl_from_queued(mid)
