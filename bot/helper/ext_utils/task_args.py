"""Argument dataclasses and parsing helpers for leech / yt-dlp commands.

Centralises the duplicated arg-default dicts that lived in
``bot.modules.leech`` and ``bot.modules.ytdlp``, and moves the
post-parse logic for ``-d`` (seed ratio:time), ``-b`` (bulk range)
and ``-i`` (multi count) into one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bot_utils import arg_parser

# ── shared arg-default dicts ────────────────────────────────────────

LEECH_ARG_DEFAULTS: dict[str, object] = {
    "-doc": False,
    "-med": False,
    "-d": False,
    "-j": False,
    "-s": False,
    "-b": False,
    "-e": False,
    "-z": False,
    "-sv": False,
    "-ss": False,
    "-f": False,
    "-fd": False,
    "-fu": False,
    "-hl": False,
    "-bt": False,
    "-ut": False,
    "-ad": False,
    "-tb": False,
    "-su": False,
    "-i": 0,
    "-sp": 0,
    "link": "",
    "-n": "",
    "-m": "",
    "-au": "",
    "-ap": "",
    "-h": [],
    "-t": "",
    "-ca": "",
    "-cv": "",
    "-ns": "",
    "-tl": "",
    "-ff": set(),
}

YTDLP_ARG_DEFAULTS: dict[str, object] = {
    "-doc": False,
    "-med": False,
    "-s": False,
    "-b": False,
    "-z": False,
    "-sv": False,
    "-ss": False,
    "-f": False,
    "-fd": False,
    "-fu": False,
    "-hl": False,
    "-bt": False,
    "-ut": False,
    "-i": 0,
    "-sp": 0,
    "link": "",
    "-m": "",
    "-opt": {},
    "-n": "",
    "-t": "",
    "-ca": "",
    "-cv": "",
    "-ns": "",
    "-tl": "",
    "-ff": set(),
}


# ── dataclasses ─────────────────────────────────────────────────────

@dataclass
class LeechArgs:
    """Parsed arguments for a ``/leech`` or ``/qbleech`` command."""

    # bool flags
    as_doc: bool = False
    as_med: bool = False
    seed: bool = False
    join: bool = False
    select: bool = False
    is_bulk: bool = False
    extract: bool = False
    compress: bool = False
    sample_video: bool = False
    screen_shots: bool = False
    force_run: bool = False
    force_download: bool = False
    force_upload: bool = False
    hybrid_leech: bool = False
    bot_trans: bool = False
    user_trans: bool = False
    is_alldebrid: bool = False
    is_torbox: bool = False
    stream_upload: bool = False

    # int
    multi: int = 0
    split_size: int = 0

    # str
    link: str = ""
    name: str = ""
    folder_name: str = ""
    thumb: str = ""
    convert_audio: str = ""
    convert_video: str = ""
    name_sub: str = ""
    thumbnail_layout: str = ""

    # str – auth
    ussr: str = ""
    pssw: str = ""

    # list / set
    headers: list[str] = field(default_factory=list)
    ffmpeg_cmds: set = field(default_factory=set)

    # seed detail (parsed from ``-d ratio:seed_time``)
    ratio: str | None = None
    seed_time: str | None = None

    # bulk detail (parsed from ``-b start:end``)
    bulk_start: int | str = 0
    bulk_end: int | str = 0


@dataclass
class YtdlpArgs:
    """Parsed arguments for a ``/ytdl`` command."""

    # bool flags
    as_doc: bool = False
    as_med: bool = False
    select: bool = False
    is_bulk: bool = False
    compress: bool = False
    sample_video: bool = False
    screen_shots: bool = False
    force_run: bool = False
    force_download: bool = False
    force_upload: bool = False
    hybrid_leech: bool = False
    bot_trans: bool = False
    user_trans: bool = False

    # int
    multi: int = 0
    split_size: int = 0

    # str
    link: str = ""
    name: str = ""
    folder_name: str = ""
    thumb: str = ""
    convert_audio: str = ""
    convert_video: str = ""
    name_sub: str = ""
    thumbnail_layout: str = ""

    # dict – yt-dlp options
    opt: dict = field(default_factory=dict)

    # set
    ffmpeg_cmds: set = field(default_factory=set)

    # bulk detail
    bulk_start: int | str = 0
    bulk_end: int | str = 0


# ── parsing functions ───────────────────────────────────────────────

def parse_leech_args(input_list: list[str]) -> LeechArgs:
    """Parse *input_list* (message tokens minus the command) into a
    :class:`LeechArgs`.

    The heavy lifting is delegated to the existing :func:`arg_parser`;
    this function owns the post-parse normalisation that used to be
    scattered across ``Leech.new_event``.
    """
    raw: dict[str, object] = {
        k: _copy_default(v) for k, v in LEECH_ARG_DEFAULTS.items()
    }
    arg_parser(input_list, raw)

    la = LeechArgs()

    # direct bool flags
    la.as_doc = raw["-doc"]
    la.as_med = raw["-med"]
    la.join = raw["-j"]
    la.select = raw["-s"]
    la.extract = raw["-e"]
    la.compress = raw["-z"]
    la.sample_video = raw["-sv"]
    la.screen_shots = raw["-ss"]
    la.force_run = raw["-f"]
    la.force_download = raw["-fd"]
    la.force_upload = raw["-fu"]
    la.hybrid_leech = raw["-hl"]
    la.bot_trans = raw["-bt"]
    la.user_trans = raw["-ut"]
    la.is_alldebrid = raw["-ad"]
    la.is_torbox = raw["-tb"]
    la.stream_upload = raw["-su"]

    # str / list / set
    la.link = raw["link"]
    la.name = raw["-n"]
    la.thumb = raw["-t"]
    la.convert_audio = raw["-ca"]
    la.convert_video = raw["-cv"]
    la.name_sub = raw["-ns"]
    la.thumbnail_layout = raw["-tl"]
    la.ussr = raw["-au"]
    la.pssw = raw["-ap"]
    la.ffmpeg_cmds = raw["-ff"]

    # headers
    h = raw["-h"]
    if h:
        la.headers = h.split("|") if isinstance(h, str) else list(h)

    # folder_name
    m = raw["-m"]
    la.folder_name = f"/{m}".rstrip("/") if len(m) > 0 else ""

    # split_size
    la.split_size = raw["-sp"]

    # -i (multi) – tolerant parse
    try:
        la.multi = int(raw["-i"])
    except (ValueError, TypeError):
        la.multi = 0

    # -d (seed ratio:time)
    seed_raw = raw["-d"]
    if not isinstance(seed_raw, bool):
        dargs = seed_raw.split(":")
        la.ratio = dargs[0] or None
        if len(dargs) == 2:
            la.seed_time = dargs[1] or None
        la.seed = True
    else:
        la.seed = seed_raw

    # -b (bulk start:end)
    bulk_raw = raw["-b"]
    if not isinstance(bulk_raw, bool):
        dargs = bulk_raw.split(":")
        la.bulk_start = dargs[0] or 0
        if len(dargs) == 2:
            la.bulk_end = dargs[1] or 0
        la.is_bulk = True
    else:
        la.is_bulk = bulk_raw

    return la


def parse_ytdlp_args(input_list: list[str]) -> YtdlpArgs:
    """Parse *input_list* into a :class:`YtdlpArgs`."""
    raw: dict[str, object] = {
        k: _copy_default(v) for k, v in YTDLP_ARG_DEFAULTS.items()
    }
    arg_parser(input_list, raw)

    ya = YtdlpArgs()

    # bool flags
    ya.as_doc = raw["-doc"]
    ya.as_med = raw["-med"]
    ya.select = raw["-s"]
    ya.compress = raw["-z"]
    ya.sample_video = raw["-sv"]
    ya.screen_shots = raw["-ss"]
    ya.force_run = raw["-f"]
    ya.force_download = raw["-fd"]
    ya.force_upload = raw["-fu"]
    ya.hybrid_leech = raw["-hl"]
    ya.bot_trans = raw["-bt"]
    ya.user_trans = raw["-ut"]

    # str / set
    ya.link = raw["link"]
    ya.name = raw["-n"]
    ya.thumb = raw["-t"]
    ya.convert_audio = raw["-ca"]
    ya.convert_video = raw["-cv"]
    ya.name_sub = raw["-ns"]
    ya.thumbnail_layout = raw["-tl"]
    ya.ffmpeg_cmds = raw["-ff"]

    # folder_name
    m = raw["-m"]
    ya.folder_name = f"/{m}".rstrip("/") if len(m) > 0 else ""

    # split_size
    ya.split_size = raw["-sp"]

    # -i (multi)
    try:
        ya.multi = int(raw["-i"])
    except (ValueError, TypeError):
        ya.multi = 0

    # -opt (yt-dlp options) – raw dict or string
    ya.opt = raw["-opt"] if isinstance(raw["-opt"], dict) else {}

    # -b (bulk)
    bulk_raw = raw["-b"]
    if not isinstance(bulk_raw, bool):
        dargs = bulk_raw.split(":")
        ya.bulk_start = dargs[0] or None
        if len(dargs) == 2:
            ya.bulk_end = dargs[1] or None
        ya.is_bulk = True
    else:
        ya.is_bulk = bulk_raw

    return ya


# ── internal helpers ────────────────────────────────────────────────

def _copy_default(value: object) -> object:
    """Return a shallow copy of mutable defaults so each parse starts fresh."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, set):
        return set(value)
    if isinstance(value, dict):
        return dict(value)
    return value
