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

COMMON_ARG_DEFAULTS: dict[str, object] = {
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
    "-n": "",
    "-m": "",
    "-t": "",
    "-c": "",
    "-ca": "",
    "-cv": "",
    "-ns": "",
    "-tl": "",
    "-ff": set(),
}
"""The flags ``/leech`` and ``/ytdl`` both take.

Kept in one dict because the two commands only differ by a handful of flags, and
the copies drifted: an option added to one was silently ignored by the other.
"""

LEECH_ARG_DEFAULTS: dict[str, object] = {
    **COMMON_ARG_DEFAULTS,
    "-d": False,
    "-j": False,
    "-e": False,
    "-ad": False,
    "-tb": False,
    "-su": False,
    "-au": "",
    "-ap": "",
    "-h": [],
}

YTDLP_ARG_DEFAULTS: dict[str, object] = {
    **COMMON_ARG_DEFAULTS,
    "-opt": {},
}

# Raw flag -> attribute, for everything both commands parse the same way.
COMMON_ARG_FIELDS: dict[str, str] = {
    "-doc": "as_doc",
    "-med": "as_med",
    "-s": "select",
    "-z": "compress",
    "-sv": "sample_video",
    "-ss": "screen_shots",
    "-f": "force_run",
    "-fd": "force_download",
    "-fu": "force_upload",
    "-hl": "hybrid_leech",
    "-bt": "bot_trans",
    "-ut": "user_trans",
    "-sp": "split_size",
    "link": "link",
    "-n": "name",
    "-t": "thumb",
    "-c": "copy_preset",
    "-ca": "convert_audio",
    "-cv": "convert_video",
    "-ns": "name_sub",
    "-tl": "thumbnail_layout",
    "-ff": "ffmpeg_cmds",
}


# ── dataclasses ─────────────────────────────────────────────────────

@dataclass
class CommonArgs:
    """What a ``/leech`` and a ``/ytdl`` command have in common.

    The two commands share every field here; only the seeding, extraction and
    debrid flags are leech's alone and only ``-opt`` is yt-dlp's.
    """

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
    copy_preset: str = ""
    convert_audio: str = ""
    convert_video: str = ""
    name_sub: str = ""
    thumbnail_layout: str = ""

    # set
    ffmpeg_cmds: set = field(default_factory=set)

    # bulk detail (parsed from ``-b start:end``)
    bulk_start: int | str = 0
    bulk_end: int | str = 0


@dataclass
class LeechArgs(CommonArgs):
    """Parsed arguments for a ``/leech`` or ``/qbleech`` command."""

    # bool flags
    seed: bool = False
    join: bool = False
    extract: bool = False
    is_alldebrid: bool = False
    is_torbox: bool = False
    stream_upload: bool = False

    # str – auth
    ussr: str = ""
    pssw: str = ""

    # list
    headers: list[str] = field(default_factory=list)

    # seed detail (parsed from ``-d ratio:seed_time``)
    ratio: str | None = None
    seed_time: str | None = None


@dataclass
class YtdlpArgs(CommonArgs):
    """Parsed arguments for a ``/ytdl`` command."""

    # dict – yt-dlp options
    opt: dict = field(default_factory=dict)


# ── parsing functions ───────────────────────────────────────────────

def parse_leech_args(input_list: list[str]) -> LeechArgs:
    """Parse *input_list* (message tokens minus the command) into a
    :class:`LeechArgs`.

    The heavy lifting is delegated to the existing :func:`arg_parser`;
    this function owns the post-parse normalisation that used to be
    scattered across ``Leech.new_event``.
    """
    raw = _parse_raw(LEECH_ARG_DEFAULTS, input_list)

    la = LeechArgs()
    _apply_common(la, raw)

    # leech-only flags
    la.join = raw["-j"]
    la.extract = raw["-e"]
    la.is_alldebrid = raw["-ad"]
    la.is_torbox = raw["-tb"]
    la.stream_upload = raw["-su"]
    la.ussr = raw["-au"]
    la.pssw = raw["-ap"]

    # headers
    h = raw["-h"]
    if h:
        la.headers = h.split("|") if isinstance(h, str) else list(h)

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

    return la


def parse_ytdlp_args(input_list: list[str]) -> YtdlpArgs:
    """Parse *input_list* into a :class:`YtdlpArgs`."""
    raw = _parse_raw(YTDLP_ARG_DEFAULTS, input_list)

    ya = YtdlpArgs()
    _apply_common(ya, raw)

    # -opt (yt-dlp options) – raw dict or string
    ya.opt = raw["-opt"] if isinstance(raw["-opt"], dict) else {}

    return ya


# ── internal helpers ────────────────────────────────────────────────

def _parse_raw(defaults: dict[str, object], input_list: list[str]) -> dict[str, object]:
    """Run :func:`arg_parser` over a fresh copy of *defaults*."""
    raw: dict[str, object] = {k: _copy_default(v) for k, v in defaults.items()}
    arg_parser(input_list, raw)
    return raw


def _apply_common(args: CommonArgs, raw: dict[str, object]) -> None:
    """Copy everything both commands parse alike from *raw* onto *args*."""
    for key, attr in COMMON_ARG_FIELDS.items():
        setattr(args, attr, raw[key])

    # folder_name
    m = raw["-m"]
    args.folder_name = f"/{m}".rstrip("/") if len(m) > 0 else ""

    # -i (multi) – tolerant parse
    try:
        args.multi = int(raw["-i"])
    except (ValueError, TypeError):
        args.multi = 0

    # -b: a bare flag, or the ``start:end`` slice of the replied-to list. The
    # empty halves fall back to 0 because ``extract_bulk_links`` puts both
    # through ``int()``, and ``-b :5`` used to reach it as None from here.
    bulk_raw = raw["-b"]
    if not isinstance(bulk_raw, bool):
        dargs = bulk_raw.split(":")
        args.bulk_start = dargs[0] or 0
        if len(dargs) == 2:
            args.bulk_end = dargs[1] or 0
        args.is_bulk = True
    else:
        args.is_bulk = bulk_raw


def _copy_default(value: object) -> object:
    """Return a shallow copy of mutable defaults so each parse starts fresh."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, set):
        return set(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def parse_folder_name(input_list: list[str], *, ytdlp: bool = False) -> str:
    """Return the ``-m`` folder name the way the full parsers derive it.

    The bulk dispatcher has to know the same-dir folder before it starts any
    task, but ``-m`` values may span several tokens and are delimited by the
    surrounding flags, so re-implementing the scan would drift from what the
    children compute. Runs the real :func:`arg_parser` over the same defaults
    instead, and keeps the ``f"/{m}".rstrip("/")`` normalisation in one place.
    """
    defaults = YTDLP_ARG_DEFAULTS if ytdlp else LEECH_ARG_DEFAULTS
    raw = _parse_raw(defaults, input_list)
    m = raw["-m"]
    return f"/{m}".rstrip("/") if len(m) > 0 else ""


def strip_link_tokens(input_list: list[str], *, ytdlp: bool = False) -> list[str]:
    """Return *input_list* without the leading positional tokens.

    :func:`arg_parser` folds everything before the first recognised flag into
    ``link``, so those tokens are the address the user typed. A bulk dispatcher
    replaces that address with one link per task, and if the original were left
    in the option string every child would parse "<its link> <original>" as a
    single address.
    """
    defaults = YTDLP_ARG_DEFAULTS if ytdlp else LEECH_ARG_DEFAULTS
    for index, token in enumerate(input_list):
        if token in defaults:
            return input_list[index:]
    return []

