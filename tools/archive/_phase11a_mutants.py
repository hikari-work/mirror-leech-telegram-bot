"""Mutation check for the Fase 11a differential harness.

A harness that reports "identical" tells you nothing until you know it can tell
the difference. This breaks the extracted pacer -- and the uploader's calls into
it -- one small change at a time, and demands that
`tools/_phase11a_diff.py` fail on every single one.

A mutation that survives is not a pass. It is either a hole in the harness or a
line that does not matter, and which of the two it is has to be established, not
assumed.

Run from the repo root: `python tools/_phase11a_mutants.py`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACER = ROOT / "bot/helper/mirror_leech_utils/upload_utils/flood_pacer.py"
UPLOADER = ROOT / "bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py"

# (name, file, text to find, replacement). Each is one plausible way to get the
# pacing subtly wrong while leaving code that still imports and runs.
MUTANTS = [
    # --- the policy constants ---
    ("cap raised", PACER, "_MAX_PACE = 4.0", "_MAX_PACE = 8.0"),
    ("calm shortened", PACER, "_CALM_FILES = 5", "_CALM_FILES = 4"),
    ("slack dropped", PACER, "FLOOD_SLACK = 1.3", "FLOOD_SLACK = 1.0"),
    # --- how the gap opens ---
    (
        "first gap halved",
        PACER,
        "min(self._MAX_PACE, self._pace * 2 or 0.5)",
        "min(self._MAX_PACE, self._pace * 2 or 0.25)",
    ),
    (
        "cap becomes a floor",
        PACER,
        "min(self._MAX_PACE, self._pace * 2 or 0.5)",
        "max(self._MAX_PACE, self._pace * 2 or 0.5)",
    ),
    (
        "gap grows by adding",
        PACER,
        "self._pace * 2 or 0.5",
        "self._pace + 0.5 or 0.5",
    ),
    (
        "flood forgets to reset the calm run",
        PACER,
        """        self._pace = min(self._MAX_PACE, self._pace * 2 or 0.5)
        self._calm = 0""",
        "        self._pace = min(self._MAX_PACE, self._pace * 2 or 0.5)",
    ),
    # --- how the gap closes ---
    (
        "decay is off by one",
        PACER,
        "if self._calm >= self._CALM_FILES:",
        "if self._calm > self._CALM_FILES:",
    ),
    (
        "gap never reaches zero",
        PACER,
        "self._pace = self._pace / 2 if self._pace > 0.5 else 0.0",
        "self._pace = self._pace / 2",
    ),
    (
        "gap snaps shut instead of decaying",
        PACER,
        "self._pace = self._pace / 2 if self._pace > 0.5 else 0.0",
        "self._pace = 0.0",
    ),
    (
        "calm run is not restarted after a decay",
        PACER,
        """            self._calm = 0
            self._pace = self._pace / 2""",
        "            self._pace = self._pace / 2",
    ),
    (
        "no-gap shortcut removed",
        PACER,
        """        if not self._pace:
            return
        await sleep(self._pace)""",
        "        await sleep(self._pace)",
    ),
    (
        "calm is counted even with no gap",
        PACER,
        """        if not self._pace:
            return
        await sleep(self._pace)
        self._calm += 1""",
        """        self._calm += 1
        if not self._pace:
            return
        await sleep(self._pace)""",
    ),
    # --- waiting out a flood ---
    (
        "cancel is only checked once",
        PACER,
        """        while True:
            if self._is_cancelled():
                return None""",
        """        if self._is_cancelled():
            return None
        while True:""",
    ),
    (
        "cancel is never checked",
        PACER,
        """            if self._is_cancelled():
                return None
            try:""",
        "            try:",
    ),
    (
        "a guarded flood does not widen the gap",
        PACER,
        """                self.note_flood()
                await sleep(f.value * FLOOD_SLACK)""",
        "                await sleep(f.value * FLOOD_SLACK)",
    ),
    (
        "a guarded flood is not waited out",
        PACER,
        "                await sleep(f.value * FLOOD_SLACK)",
        "                pass",
    ),
    (
        "premium floods are not caught",
        PACER,
        "except (FloodWait, FloodPremiumWait) as f:",
        "except FloodWait as f:",
    ),
    (
        "the rate limit is not logged",
        PACER,
        '                LOGGER.warning('
        'f"Rate limited on {name}: waiting {f.value}s. {f}")',
        "                pass",
    ),
    (
        "retry gives up after the first flood",
        PACER,
        """        while True:
            if self._is_cancelled():""",
        """        for _ in range(1):
            if self._is_cancelled():""",
    ),
    # --- the uploader's side of the seam ---
    (
        "the per-file sender stops sharing the widened gap",
        UPLOADER,
        """                    self._pacer.note_flood()
                    await sleep(f.value * FLOOD_SLACK)""",
        "                    await sleep(f.value * FLOOD_SLACK)",
    ),
    (
        "the per-file sender waits without the margin",
        UPLOADER,
        "                    await sleep(f.value * FLOOD_SLACK)",
        "                    await sleep(f.value)",
    ),
    (
        "the per-file sender retries in place",
        UPLOADER,
        """                    await sleep(f.value * FLOOD_SLACK)
                    raise""",
        "                    await sleep(f.value * FLOOD_SLACK)",
    ),
    (
        "the per-file sender misses premium floods",
        UPLOADER,
        """                except (FloodWait, FloodPremiumWait) as f:
                    LOGGER.warning(str(f))""",
        """                except FloodWait as f:
                    LOGGER.warning(str(f))""",
    ),
    (
        "the pacer is built with a fixed answer",
        UPLOADER,
        "FloodPacer(lambda: self._listener.is_cancelled)",
        "FloodPacer(lambda: False)",
    ),
    (
        "one guarded call site loses its guard",
        UPLOADER,
        """        return await self._pacer.guard(
            self._group_client.get_messages,
            chat_id=chat_id,
            message_ids=message_id,
        )""",
        """        return await self._group_client.get_messages(
            chat_id=chat_id,
            message_ids=message_id,
        )""",
    ),
    # --- the call-site check's own subject ---
    (
        "the gap between files is never waited",
        UPLOADER,
        "await self._pacer.pace()",
        "pass",
    ),
]


def _run_harness():
    proc = subprocess.run(
        [sys.executable, "tools/_phase11a_diff.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def main():
    originals = {path: path.read_text() for path in (PACER, UPLOADER)}
    survivors = []
    broken = []
    try:
        for name, path, find, replace in MUTANTS:
            src = originals[path]
            if src.count(find) != 1:
                broken.append((name, f"anchor matched {src.count(find)} times"))
                continue
            path.write_text(src.replace(find, replace))
            code, out = _run_harness()
            path.write_text(src)
            if code == 0:
                survivors.append((name, out.strip().splitlines()[-1]))
                print(f"  SURVIVED  {name}")
            else:
                print(f"  caught    {name}")
    finally:
        for path, src in originals.items():
            path.write_text(src)

    total = len(MUTANTS)
    print(f"\n{total - len(survivors) - len(broken)}/{total} mutations caught")
    for name, why in broken:
        print(f"  ! anchor stale: {name} ({why})")
    for name, last in survivors:
        print(f"  ! survived: {name} -- harness said: {last}")
    return 1 if survivors or broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
