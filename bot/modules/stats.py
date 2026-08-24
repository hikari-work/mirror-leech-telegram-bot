from time import time
from re import search as research
from asyncio import gather
from aiofiles.os import path as aiopath
from psutil import (
    disk_usage,
    cpu_percent,
    swap_memory,
    cpu_count,
    virtual_memory,
    net_io_counters,
    boot_time,
)

from .. import bot_start_time
from ..helper.ext_utils.status_utils import get_readable_file_size, get_readable_time
from ..helper.ext_utils.bot_utils import cmd_exec, new_task
from ..helper.telegram_helper.message_utils import send_message

commands: dict[str, tuple[list[str], str]] = {
    "aria2": (["aria2c", "--version"], r"aria2 version ([\d.]+)"),
    "qBittorrent": (["qbittorrent-nox", "--version"], r"qBittorrent v([\d.]+)"),
    "python": (["python3", "--version"], r"Python ([\d.]+)"),
    "yt-dlp": (["yt-dlp", "--version"], r"([\d.]+)"),
    "ffmpeg": (["ffmpeg", "-version"], r"ffmpeg version ([\d.]+(-\w+)?).*"),
    "7z": (["7z", "i"], r"7-Zip ([\d.]+)"),
}
"""How to ask each tool for its version: the argv to run and what to read out."""

versions: dict[str, str] = {}
"""What each of them answered, filled once at startup by ``get_packages_version``.

Its own dict rather than the answers overwriting ``commands`` in place, which
left that name meaning two different things depending on when you read it.
"""


@new_task
async def bot_stats(_, message):
    """Answer /stats.

    The version rows read through ``.get``: ``new_task`` schedules
    ``get_packages_version`` rather than awaiting it, so startup finishes and the
    handlers register while the probes are still running. A /stats sent in that
    window used to raise ``KeyError: 'commit'`` out of the f-string below.
    """
    total, used, free, disk = disk_usage("/")
    swap = swap_memory()
    memory = virtual_memory()
    per_cpu = cpu_percent(interval=1, percpu=True)
    per_cpu_str = " | ".join([f"CPU{i+1}: {round(p)}%" for i, p in enumerate(per_cpu)])
    stats = f"""
<b>Commit Date:</b> {versions.get("commit", "-")}

<b>Bot Uptime:</b> {get_readable_time(time() - bot_start_time)}
<b>OS Uptime:</b> {get_readable_time(time() - boot_time())}

<b>Total Disk Space:</b> {get_readable_file_size(total)}
<b>Used:</b> {get_readable_file_size(used)} | <b>Free:</b> {get_readable_file_size(free)}

<b>Upload:</b> {get_readable_file_size(net_io_counters().bytes_sent)}
<b>Download:</b> {get_readable_file_size(net_io_counters().bytes_recv)}

<b>CPU:</b> {cpu_percent(interval=1)}%
<b>CPU Cores:</b>
{per_cpu_str}

<b>RAM:</b> {memory.percent}%
<b>DISK:</b> {disk}%

<b>Physical Cores:</b> {cpu_count(logical=False)}
<b>Total Cores:</b> {cpu_count()}
<b>SWAP:</b> {get_readable_file_size(swap.total)} | <b>Used:</b> {swap.percent}%

<b>Memory Total:</b> {get_readable_file_size(memory.total)}
<b>Memory Free:</b> {get_readable_file_size(memory.available)}
<b>Memory Used:</b> {get_readable_file_size(memory.used)}

<b>python:</b> {versions.get("python", "-")}
<b>aria2:</b> {versions.get("aria2", "-")}
<b>qBittorrent:</b> {versions.get("qBittorrent", "-")}
<b>yt-dlp:</b> {versions.get("yt-dlp", "-")}
<b>ffmpeg:</b> {versions.get("ffmpeg", "-")}
<b>7z:</b> {versions.get("7z", "-")}
"""
    await send_message(message, stats)


async def get_version_async(command, regex):
    try:
        out, err, code = await cmd_exec(command)
        if code != 0:
            return f"Error: {err}"
        match = research(regex, out)
        return match.group(1) if match else "Version not found"
    except Exception as e:
        return f"Exception: {str(e)}"


@new_task
async def get_packages_version():
    tasks = [get_version_async(command, regex) for command, regex in commands.values()]
    results = await gather(*tasks)
    for tool, version in zip(commands.keys(), results):
        versions[tool] = version
    if await aiopath.exists(".git"):
        last_commit = (
            await cmd_exec(
                "git log -1 --date=short --pretty=format:'%cd <b>From</b> %cr'", True
            )
        )[0]
    else:
        last_commit = "No UPSTREAM_REPO"
    versions["commit"] = last_commit
