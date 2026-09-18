"""Asking yt-dlp where a file finally landed.

A streamed (``-su``) file is uploaded the moment it is complete, so the
downloader has to hand over a path that exists *now* -- and with yt-dlp that is
not the path the download was started with. It picks the container, muxes
fragments into it, and moves the result out of its temporary name last of all.

The answer only exists inside yt-dlp, and the postprocessing hook is where it
can be caught. That payload is ``{'status', 'postprocessor', 'info_dict'}`` (no
``when`` key, as the download hooks have) and the info dict is a *shallow* copy
taken before the postprocessor ran -- so its ``filepath`` is still the path
before the move, while the move itself is recorded as
``__files_to_move[old] = new`` in a dict the copy shares with the live one. The
move that ends a video's postprocessing is therefore the only hook call worth
reading, and ``MoveFiles`` is the postprocessor that makes it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# The postprocessor whose run is the last thing between a finished mux and a
# file on disk, whichever downloader asked for it.
_MOVE = "MoveFiles"


def final_paths_hook(emit: Callable[[str], None]) -> Callable[[dict[str, Any]], None]:
    """A ``postprocessor_hooks`` entry that hands over each finished file's path.

    The hooks of a playlist are called once per video, in order, and this is
    called for every postprocessor and every status, so it answers only to the
    move and reports each file once. A file the move left where it already was
    is reported at the path it was downloaded to, which is where it is.

    *emit* is what the caller can do with a path from inside yt-dlp's download
    thread: a list's ``append`` when the downloader collects the paths and reads
    them once its blocking call has returned, or a bridge onto the event loop
    when each file has to be handed over as it lands.
    """
    seen: set[str] = set()

    def hook(payload: dict[str, Any]) -> None:
        if payload.get("status") != "finished":
            return
        if payload.get("postprocessor") != _MOVE:
            return
        info = payload.get("info_dict") or {}
        path = (info.get("__files_to_move") or {}).get(info.get("filepath"))
        path = path or info.get("filepath")
        if path and path not in seen:
            seen.add(path)
            emit(path)

    return hook
