"""Smoke test: every module under ``bot/`` must import cleanly.

Fase 1 landed ``download_utils/link_resolver.py`` with ``from ... import
LOGGER``. That file sits three packages below ``bot``, so ``...`` resolves to
``bot.helper``, not ``bot`` — the module was un-importable from the moment it
was committed. Nothing noticed, because no test imports ``bot.modules`` and
every unit test stubs its dependencies into ``sys.modules`` before importing
its target. Meanwhile the real bot could not start at all: ``bot/__main__.py``
ends with ``from .core.handlers import add_handlers``, and ``handlers.py``
imports ``bot.modules``, which imports ``leech.py``, which imports
``link_resolver``.

A wrong relative-import depth is invisible to stub-based tests by construction,
so it is guarded the only way that works: import everything for real.

The sweep runs in a subprocess. ``bot/__init__.py`` is side-effectful on import
(``uvloop.install()``, builds an event loop, opens ``log.txt``, starts an
APScheduler), and none of that belongs in the pytest process where other tests
are busy stubbing ``bot.*`` entries out of ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# `bot.__main__` is deliberately excluded: it calls `Config.load()` at import
# time, which raises "BOT_TOKEN variable is missing!" without a real config.
_SWEEP = """
import importlib
import pkgutil

import bot

failures = []
for module in pkgutil.walk_packages(bot.__path__, prefix="bot."):
    if module.name == "bot.__main__":
        continue
    try:
        importlib.import_module(module.name)
    except Exception as exc:
        failures.append(f"{module.name}: {type(exc).__name__}: {exc}")

for line in failures:
    print("IMPORT-FAILURE " + line)
"""


def test_every_bot_module_imports(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _SWEEP],
        cwd=tmp_path,  # keep the log.txt that `bot/__init__.py` opens out of the repo
        env={"PYTHONPATH": str(_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    failures = [
        line.removeprefix("IMPORT-FAILURE ")
        for line in proc.stdout.splitlines()
        if line.startswith("IMPORT-FAILURE ")
    ]
    assert proc.returncode == 0, f"sweep crashed:\n{proc.stdout}\n{proc.stderr}"
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def test_handlers_module_is_importable():
    """`add_handlers` is the bot's last import before `run_forever()`.

    Called out separately from the sweep so a regression here names the file
    that actually stops the bot from starting.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "from bot.core.handlers import add_handlers"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
