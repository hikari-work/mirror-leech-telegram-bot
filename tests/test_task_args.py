"""Tests for ``task_args`` — the centralised arg-parsing helpers.

These tests load ``arg_parser`` and the ``task_args`` module from source
without importing the full bot stack, using the same exec-from-source
pattern as ``test_arg_parser_flags.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BOT_UTILS_PATH = _ROOT / "bot" / "helper" / "ext_utils" / "bot_utils.py"
_TASK_ARGS_PATH = _ROOT / "bot" / "helper" / "ext_utils" / "task_args.py"


def _extract_arg_parser():
    """Extract just the ``arg_parser`` function from ``bot_utils.py``."""
    src = _BOT_UTILS_PATH.read_text(encoding="utf-8")
    snippet_start = src.find("def arg_parser(")
    snippet_end = src.find("\ndef ", snippet_start + 1)
    if snippet_end == -1:
        snippet_end = len(src)
    snippet = src[snippet_start:snippet_end]
    ns: dict[str, object] = {}
    exec(compile(snippet, str(_BOT_UTILS_PATH), "exec"), ns)  # noqa: S102
    return ns["arg_parser"]


@pytest.fixture
def task_args():
    """Return a namespace containing ``parse_leech_args``, ``parse_ytdlp_args``,
    ``LeechArgs``, ``YtdlpArgs`` loaded without the bot package tree."""
    arg_parser_fn = _extract_arg_parser()

    src = _TASK_ARGS_PATH.read_text(encoding="utf-8")

    # Create a proper module so dataclasses can look up __module__
    mod = ModuleType("_test_task_args_ns")
    sys.modules[mod.__name__] = mod

    ns = mod.__dict__
    ns["__builtins__"] = __builtins__

    # We need dataclass support
    import dataclasses
    ns["dataclass"] = dataclasses.dataclass
    ns["field"] = dataclasses.field

    # Replace the import of arg_parser with a direct assignment
    # Strip the ``from .bot_utils import arg_parser`` line and inject manually
    lines = src.split("\n")
    filtered = []
    for line in lines:
        if "from __future__" in line:
            filtered.append(line)
        elif "from .bot_utils import" in line:
            continue  # skip — we inject arg_parser manually
        elif line.startswith("from ."):
            continue  # skip other relative imports
        elif line.startswith("from ") or line.startswith("import "):
            filtered.append(line)
        else:
            filtered.append(line)

    clean_src = "\n".join(filtered)
    ns["arg_parser"] = arg_parser_fn

    exec(compile(clean_src, str(_TASK_ARGS_PATH), "exec"), ns)  # noqa: S102

    try:
        yield ns
    finally:
        sys.modules.pop(mod.__name__, None)


# ── parse_leech_args ────────────────────────────────────────────────

class TestParseLeechArgs:
    def test_basic_link(self, task_args):
        la = task_args["parse_leech_args"](["http://example.com/file.zip"])
        assert la.link == "http://example.com/file.zip"
        assert la.seed is False
        assert la.is_bulk is False

    def test_bool_flags(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-ad", "-z", "-e", "-s"])
        assert la.is_alldebrid is True
        assert la.compress is True
        assert la.extract is True
        assert la.select is True

    def test_seed_ratio_time(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-d", "1.5:60"])
        assert la.seed is True
        assert la.ratio == "1.5"
        assert la.seed_time == "60"

    def test_seed_ratio_only(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-d", "2.0"])
        assert la.seed is True
        assert la.ratio == "2.0"
        assert la.seed_time is None

    def test_seed_bool_flag(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-d"])
        assert la.seed is True
        assert la.ratio is None
        assert la.seed_time is None

    def test_bulk_range(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-b", "2:5"])
        assert la.is_bulk is True
        assert la.bulk_start == "2"
        assert la.bulk_end == "5"

    def test_bulk_start_only(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-b", "3:"])
        assert la.is_bulk is True
        assert la.bulk_start == "3"
        assert la.bulk_end == 0

    def test_bulk_bool_flag(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-b"])
        assert la.is_bulk is True
        assert la.bulk_start == 0
        assert la.bulk_end == 0

    def test_multi_count(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-i", "5"])
        assert la.multi == 5

    def test_multi_invalid(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-i", "abc"])
        assert la.multi == 0

    def test_folder_name(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-m", "my_folder"])
        assert la.folder_name == "/my_folder"

    def test_folder_name_empty(self, task_args):
        la = task_args["parse_leech_args"](["http://x"])
        assert la.folder_name == ""

    def test_name_flag(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-n", "custom_name"])
        assert la.name == "custom_name"

    def test_headers(self, task_args):
        la = task_args["parse_leech_args"](
            ["http://x", "-h", "Referer: http://y|Cookie: z=1"]
        )
        assert la.headers == ["Referer: http://y", "Cookie: z=1"]

    def test_no_headers(self, task_args):
        la = task_args["parse_leech_args"](["http://x"])
        assert la.headers == []

    def test_auth_flags(self, task_args):
        la = task_args["parse_leech_args"](
            ["http://x", "-au", "user", "-ap", "pass"]
        )
        assert la.ussr == "user"
        assert la.pssw == "pass"

    def test_torbox_flag(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-tb"])
        assert la.is_torbox is True

    def test_doc_med_flags(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-doc"])
        assert la.as_doc is True
        assert la.as_med is False

    def test_ffmpeg_cmds(self, task_args):
        la = task_args["parse_leech_args"](["http://x", "-ff", "some_cmd"])
        assert "some_cmd" in la.ffmpeg_cmds

    def test_defaults_isolated(self, task_args):
        """Ensure successive calls don't leak state from mutable defaults."""
        parse = task_args["parse_leech_args"]
        la1 = parse(["http://a", "-ad"])
        la2 = parse(["http://b"])
        assert la1.is_alldebrid is True
        assert la2.is_alldebrid is False
        assert la2.link == "http://b"


# ── parse_ytdlp_args ───────────────────────────────────────────────

class TestParseYtdlpArgs:
    def test_basic_link(self, task_args):
        ya = task_args["parse_ytdlp_args"](
            ["https://youtube.com/watch?v=123"]
        )
        assert ya.link == "https://youtube.com/watch?v=123"
        assert ya.is_bulk is False

    def test_bool_flags(self, task_args):
        ya = task_args["parse_ytdlp_args"](["http://x", "-s", "-z", "-hl"])
        assert ya.select is True
        assert ya.compress is True
        assert ya.hybrid_leech is True

    def test_bulk_range(self, task_args):
        ya = task_args["parse_ytdlp_args"](["http://x", "-b", "1:10"])
        assert ya.is_bulk is True
        assert ya.bulk_start == "1"
        assert ya.bulk_end == "10"

    def test_multi(self, task_args):
        ya = task_args["parse_ytdlp_args"](["http://x", "-i", "3"])
        assert ya.multi == 3

    def test_folder_name(self, task_args):
        ya = task_args["parse_ytdlp_args"](["http://x", "-m", "videos"])
        assert ya.folder_name == "/videos"

    def test_no_seed_fields(self, task_args):
        """YtdlpArgs should not have seed/ratio/seed_time."""
        ya = task_args["parse_ytdlp_args"](["http://x"])
        assert not hasattr(ya, "seed")
        assert not hasattr(ya, "ratio")

    def test_defaults_isolated(self, task_args):
        parse = task_args["parse_ytdlp_args"]
        ya1 = parse(["http://a", "-s"])
        ya2 = parse(["http://b"])
        assert ya1.select is True
        assert ya2.select is False
