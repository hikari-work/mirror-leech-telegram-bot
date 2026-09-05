"""The two shutil calls the bot makes, moved off the event loop.

This replaces the aioshutil dependency, which wrapped every function in shutil
the same way -- ``run_in_executor(None, partial(fn, *args, **kwargs))`` -- while
only ``rmtree`` and ``move`` were ever imported from it. ``to_thread`` hands work
to that same default executor, so the thread hop is unchanged; what goes away is
a package whose entire surface came along to reach two names.

Kept as its own module rather than folded into ``files_utils`` so that
``core.startup`` can reach it without pulling in that module's chain (magic,
bot_utils, TorrentManager) for a single ``rmtree``.
"""

from asyncio import to_thread
from shutil import move as _move
from shutil import rmtree as _rmtree

__all__ = ["move", "rmtree"]


async def rmtree(path, ignore_errors: bool = False) -> None:
    """Recursively delete *path*, off the event loop.

    Only ``ignore_errors`` is threaded through because it is the only option any
    caller passes; ``onexc`` and friends can be added when something wants them.
    """
    await to_thread(_rmtree, path, ignore_errors=ignore_errors)


async def move(src, dst):
    """Move *src* onto *dst*, off the event loop, answering the new path.

    shutil falls back to a copy-then-delete when the two sit on different
    filesystems, which is why this cannot simply be ``os.rename``: downloads and
    their upload staging directory are routinely separate mounts.
    """
    return await to_thread(_move, src, dst)
