"""Running one child process and reading its progress.

``FFMpeg`` and ``SevenZ`` drive a command line the same way: refuse to start if
the task is already cancelled, leave the handle on the listener so
``cancel_task`` can kill it, drain stdout through a tool-specific progress
reader, then wait for the exit code. Five copies of that in ``media_utils.py``
and two in ``files_utils.py`` had already drifted apart -- some tested ``-9``
before ``0`` and some after, one caught bare ``Exception`` where the rest caught
the two errors a ``bytes.decode()`` can actually raise -- so it lives here once.
"""

from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE


class SubprocRunner:
    """Runs one command against ``self._listener`` and reports how it ended."""

    async def _read_progress(self):
        """Drain the child's stdout into this tool's counters.

        Runs while the process does, so it is also what notices a cancel: both
        readers stop as soon as ``listener.is_cancelled`` is set.
        """
        raise NotImplementedError

    async def _run_cmd(self, cmd):
        return await run_subproc(self._listener, cmd, self._read_progress)


async def run_subproc(listener, cmd, read_progress=None, stdout=PIPE):
    """Run *cmd* to completion, leaving the handle on *listener* to be killed.

    Returns ``(code, stderr)`` with *stderr* already decoded, or ``(None, "")``
    when the task was cancelled -- by the user, or by the OOM kill that arrives
    as ``-9`` and that every caller treats as a cancel too.
    """
    if listener.is_cancelled:
        return None, ""
    listener.subproc = await create_subprocess_exec(*cmd, stdout=stdout, stderr=PIPE)
    if read_progress is not None:
        await read_progress()
    _, stderr = await listener.subproc.communicate()
    code = listener.subproc.returncode
    if listener.is_cancelled:
        return None, ""
    if code == -9:
        listener.is_cancelled = True
        return None, ""
    if code == 0:
        return 0, ""
    try:
        stderr = stderr.decode().strip()
    except (UnicodeDecodeError, AttributeError):
        stderr = "Unable to decode the error!"
    return code, stderr
