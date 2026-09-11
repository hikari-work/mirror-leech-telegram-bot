"""`/stats` must not ask psutil for a sampled CPU reading.

``psutil.cpu_percent(interval=1)`` does not measure "the CPU right now" -- it
*sleeps* for the interval and measures over it. The call is synchronous, so
those two seconds (the function asks twice) are two seconds in which the single
event loop serves nobody: every other user's download progress, upload progress
and status refresh stops dead. ``cpu_percent()`` with no interval answers
immediately with the average since the previous call, which is what a status
line wants anyway -- and is already what the ``/status`` page asks for.

So the property to pin is narrow and checkable: no call in the ``/stats``
handler passes an ``interval``.

The handler is collected in a subprocess for the same reason
``test_handlers_table`` does it. Importing the real ``bot`` package installs
uvloop's policy, builds an event loop, opens ``log.txt`` and starts a scheduler;
none of that belongs in the pytest process, where other tests are stubbing
``bot.*`` entries out of ``sys.modules``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MARKER = "<<<CPU-CALLS-JSON>>>"

# Read-only fakes for every psutil probe the handler makes, plus the reply. The
# spy is the point: it records how each cpu_percent call was made.
_PROBE = f"""
import asyncio
import json
import psutil

from bot.modules import stats

calls = []


def spy(*args, **kwargs):
    calls.append({{"args": len(args), "kwargs": sorted(kwargs)}})
    return [1.0, 2.0] if kwargs.get("percpu") else 1.0


stats.cpu_percent = spy
stats.disk_usage = lambda _: (1, 2, 3, 4.0)
stats.swap_memory = lambda: type("S", (), {{"total": 1, "percent": 1.0}})()
stats.virtual_memory = lambda: type("V", (), {{"percent": 1.0, "total": 1,
                                              "available": 1, "used": 1}})()
stats.cpu_count = lambda logical=True: 1
stats.net_io_counters = lambda: type("N", (), {{"bytes_sent": 1,
                                                "bytes_recv": 1}})()
stats.boot_time = lambda: 0.0


async def reply(*a, **k):
    return None


stats.send_message = reply

asyncio.run(stats.bot_stats.__wrapped__(None, object()))
print("{_MARKER}" + json.dumps(calls))
"""


def _cpu_calls() -> list[dict]:
    """Every ``cpu_percent`` call ``/stats`` made, as ``{args, kwargs}``."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith(_MARKER)
    )
    return json.loads(line.removeprefix(_MARKER))


def test_stats_never_asks_psutil_to_sample_the_cpu():
    """An ``interval`` is a sleep, and the sleep is on the event loop."""
    calls = _cpu_calls()

    # both readings still happen -- this is about how, not whether
    assert len(calls) == 2
    assert [call for call in calls if "interval" in call["kwargs"]] == []
    # the per-core reading is still per-core
    assert calls[0]["kwargs"] == ["percpu"]
