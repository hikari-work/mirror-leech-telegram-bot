"""Harness diferensial: jalur tautan telegram sebelum vs sesudah Fase 12.

Versi HEAD `message_utils.py` dimuat langsung dari git di samping versi working
tree, lalu keduanya dijalankan lewat skenario yang sama. Yang dibandingkan:
panggilan ke klien telegram (metode, chat, id, urutan), hasil (bentuk kembali
atau eksepsi), dan sesi yang dipilih. Referensi ``git show`` dipakai agar
harness tidak bergantung pada berkas sementara di pohon kerja.

Fase 12 mengubah perilaku secara sengaja pada rentang, jadi perbandingan
diferensial hanya mencakup tautan tunggal. Rentang diuji terhadap ekspektasi
yang ditulis eksplisit di bawah -- bentuknya tidak mungkin dibandingkan dengan
HEAD karena HEAD memang salah pada dua bentuk rentang yang sekarang didukung.

Jalankan dari akar repo: ``python tools/archive/_phase12_diff.py``
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET = "bot/helper/telegram/message_utils.py"
CAGE = "_phase12_cage"
# Nama harus setinggi aslinya: ``from ... import LOGGER`` berarti naik tiga
# tingkat dari paket ``bot.helper.telegram``, jadi kandangnya pun butuh
# ``kandang.bot.helper.telegram``.
TOP = f"{CAGE}.bot"
PKG = f"{TOP}.helper.telegram"
NEW_NAME = f"{PKG}.message_utils"
OLD_NAME = f"{PKG}._old_message_utils"


# --------------------------------------------------------------------------
# kandang: modul tiruan yang dibutuhkan kedua versi saat diimpor
# --------------------------------------------------------------------------


class FloodWait(Exception):
    def __init__(self, value=1):
        super().__init__("flood")
        self.value = value


class FloodPremiumWait(FloodWait):
    pass


class TgLinkException(Exception):
    pass


class Bag:
    """Tempat menyimpan klien yang sedang dipakai satu skenario."""

    user_client = None


def _module(name: str, package: bool = False, **attrs) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    if "." in name:
        parent, _, leaf = name.rpartition(".")
        setattr(sys.modules[parent], leaf, module)
    return module


def _build_cage() -> None:
    _module(CAGE, package=True)
    _module(TOP, package=True)
    _module(f"{TOP}.core", package=True)
    _module(f"{TOP}.helper", package=True)
    _module(f"{TOP}.helper.util", package=True)
    _module(PKG, package=True)

    pyrogram = _module("pyrogram", package=True)
    errors = _module("pyrogram.errors")
    errors.FloodWait = FloodWait
    errors.FloodPremiumWait = FloodPremiumWait
    pyrogram.errors = errors

    _module(
        f"{TOP}.core.config_manager",
        Config=SimpleNamespace(STATUS_UPDATE_INTERVAL=5),
    )
    _module(
        f"{TOP}.core.telegram_manager",
        TgClient=SimpleNamespace(bot=None, user=None),
        get_user_client=_get_user_client,
    )
    _module(f"{TOP}.helper.util.bot_utils", SetInterval=object)
    _module(f"{TOP}.helper.util.exceptions", TgLinkException=TgLinkException)
    _module(
        f"{TOP}.helper.util.status_utils",
        get_readable_message=_never_called,
    )
    _module(f"{PKG}.flood", flood_seconds=lambda f: getattr(f, "value", 0))

    # links_utils asli dimuat apa adanya: extract_urls adalah bagian dari
    # perilaku baru, dan harness tidak boleh menguji salinan kedua yang bisa
    # menyimpang dari yang dipakai produksi.
    _load(
        f"{TOP}.helper.util.links_utils",
        ROOT / "bot/helper/util/links_utils.py",
    )

    sys.modules[TOP].LOGGER = SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    sys.modules[TOP].DOWNLOAD_DIR = "/tmp/"
    sys.modules[TOP].status_dict = {}
    sys.modules[TOP].intervals = {"status": {}, "stopAll": False}
    sys.modules[TOP].task_dict_lock = asyncio.Lock()


async def _get_user_client(user_id):
    return Bag.user_client


async def _never_called(*args, **kwargs):
    raise AssertionError("get_readable_message must not be reached here")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib_util.spec_from_file_location(name, path)
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_from_git(name: str) -> ModuleType:
    source = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = ModuleType(name)
    module.__file__ = f"<git HEAD:{TARGET}>"
    module.__package__ = PKG
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


# --------------------------------------------------------------------------
# klien dan pesan tiruan
# --------------------------------------------------------------------------


class Message:
    def __init__(self, message_id: int, empty: bool = False):
        self.id = message_id
        self.empty = empty
        self.text = None
        self.caption = None


class Behavior:
    """Jawaban satu klien per id, termasuk yang berupa eksepsi."""

    def __init__(self, answer):
        self.answer = answer

    def __call__(self, chat_id, message_ids):
        if isinstance(message_ids, list):
            return [self.answer(i) for i in message_ids]
        return self.answer(message_ids)


def ok_at(*ids: int) -> Behavior:
    wanted = set(ids)
    return Behavior(lambda i: Message(i) if i in wanted else None)


def empty_at(*ids: int) -> Behavior:
    wanted = set(ids)
    return Behavior(lambda i: Message(i, empty=True) if i in wanted else None)


def raises(exc: Exception) -> Behavior:
    def answer(_message_id):
        raise exc

    return Behavior(answer)


def make_client(name: str, behavior: Behavior, calls: list) -> SimpleNamespace:
    async def get_messages(chat_id, message_ids):
        calls.append({"client": name, "chat_id": chat_id, "message_ids": message_ids})
        return behavior(chat_id, message_ids)

    return SimpleNamespace(get_messages=get_messages)


def describe(result, error: Exception | None) -> dict:
    if error is not None:
        return {"kind": "error", "type": type(error).__name__, "text": str(error)}
    value, session = result
    if isinstance(value, list):
        return {"kind": "links", "links": list(value), "session": session}
    return {
        "kind": "message",
        "id": value.id,
        "empty": value.empty,
        "session": session,
    }


async def run_scenario(module, scenario: dict) -> dict:
    calls: list = []
    telegram_manager = sys.modules[f"{TOP}.core.telegram_manager"]
    telegram_manager.TgClient.bot = make_client("bot", scenario["bot"], calls)
    telegram_manager.TgClient.user = None
    Bag.user_client = (
        make_client("user", scenario["user"], calls) if scenario.get("user") else None
    )
    try:
        result = await module.get_tg_link_message(scenario["link"], user_id=7)
        error = None
    except Exception as e:  # noqa: BLE001 - hasilnya memang dibandingkan
        result, error = None, e
    return {"calls": calls, "outcome": describe(result, error)}


def describe_parse(module, address: str) -> dict:
    try:
        parsed = module.parse_tg_link(address)
    except Exception as e:  # noqa: BLE001
        return {"kind": "error", "type": type(e).__name__, "text": str(e)}
    return {
        "kind": "link",
        "chat": parsed.chat,
        "scheme": parsed.scheme,
        "prefix": parsed.prefix,
        "ids": list(parsed.ids),
        "is_range": parsed.is_range,
        "count": parsed.count,
    }


# --------------------------------------------------------------------------
# skenario: tautan tunggal, dibandingkan HEAD vs working tree
# --------------------------------------------------------------------------


SINGLE_LINKS = [
    {
        "name": "username publik, bot bisa membaca",
        "link": "https://t.me/mychannel/321",
        "bot": ok_at(321),
    },
    {
        "name": "channel c/, bot bisa membaca",
        "link": "https://t.me/c/1234567890/55",
        "bot": ok_at(55),
    },
    {
        "name": "channel tanpa c/, bot bisa membaca",
        "link": "https://t.me/1234567890/55",
        "bot": ok_at(55),
    },
    {
        "name": "tautan topik memakai prefix yang sama",
        "link": "https://t.me/c/1234/77/55",
        "bot": ok_at(55),
    },
    {
        "name": "album ?single membaca id yang sama",
        "link": "https://t.me/c/1234/55?single",
        "bot": ok_at(55),
    },
    {
        "name": "teks setelah tautan tidak mengubah id",
        "link": "https://t.me/c/1234/55 tonton ini",
        "bot": ok_at(55),
    },
    {
        "name": "bot mengembalikan placeholder kosong, sesi user dipakai",
        "link": "https://t.me/c/1234/55",
        "bot": empty_at(55),
        "user": ok_at(55),
    },
    {
        "name": "bot mengembalikan None, sesi user dipakai",
        "link": "https://t.me/c/1234/55",
        "bot": Behavior(lambda _i: None),
        "user": ok_at(55),
    },
    {
        "name": "bot melempar error, sesi user dipakai",
        "link": "https://t.me/c/1234/55",
        "bot": raises(RuntimeError("bot buta")),
        "user": ok_at(55),
    },
    {
        "name": "bot kosong tanpa sesi user",
        "link": "https://t.me/c/1234/55",
        "bot": empty_at(55),
    },
    {
        "name": "bot melempar error tanpa sesi user",
        "link": "https://t.me/c/1234/55",
        "bot": raises(RuntimeError("bot buta")),
    },
    {
        "name": "bot bisa membaca tanpa sesi user",
        "link": "https://t.me/c/1234/55",
        "bot": ok_at(55),
    },
    {
        "name": "tautan privat membaca lewat sesi user",
        "link": "tg://openmessage?user_id=-1001234&message_id=55",
        "bot": ok_at(55),
        "user": ok_at(55),
    },
    {
        "name": "tautan privat tanpa sesi user",
        "link": "tg://openmessage?user_id=-1001234&message_id=55",
        "bot": ok_at(55),
    },
    {
        "name": "tautan privat dengan pesan kosong",
        "link": "tg://openmessage?user_id=-1001234&message_id=55",
        "bot": ok_at(55),
        "user": empty_at(55),
    },
    {
        "name": "tautan privat yang tidak bisa dibaca",
        "link": "tg://openmessage?user_id=-1001234&message_id=55",
        "bot": ok_at(55),
        "user": raises(RuntimeError("bukan anggota")),
    },
]

DIVERGENT_LINKS = [
    {
        "name": "bukan tautan telegram",
        "link": "https://example.invalid/55",
        "bot": ok_at(55),
        "expect": {
            "calls": [],
            "outcome": {
                "kind": "error",
                "type": "TgLinkException",
                "text": "Wrong link format!",
            },
        },
    },
]

DIVERGENCES = """
Perbedaan yang disengaja, jadi tidak ikut dibandingkan dengan HEAD:

- rentang apa pun (`5-7`, `7-5`, dua tautan penuh): HEAD salah, Fase 12 memperbaiki;
- id 0: HEAD meneruskannya ke telegram, sekarang ditolak sebelum ada request;
- `5-` (tanda hubung tanpa angka): HEAD melempar ValueError, sekarang
  TgLinkException("Wrong link format!");
- alamat yang bukan telegram tanpa sesi user: HEAD menjawab
  "USER_SESSION_STRING required..." karena memeriksa sesi sebelum bentuk
  tautan; sekarang "Wrong link format!", yang memang penyebabnya.
"""


# --------------------------------------------------------------------------
# skenario: rentang, diuji terhadap ekspektasi eksplisit
# --------------------------------------------------------------------------

CHAT = -1001234
PREFIX = "https://t.me/c/1234/"

PARSE_EXPECTATIONS = [
    (
        "rentang pendek",
        "https://t.me/c/1234/5-7",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "rentang pendek ditulis terbalik",
        "https://t.me/c/1234/7-5",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "dua tautan penuh",
        f"{PREFIX}5-{PREFIX}7",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "dua tautan penuh ditulis terbalik",
        f"{PREFIX}7-{PREFIX}5",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "topik dipertahankan",
        "https://t.me/c/1234/77/5-https://t.me/c/1234/77/7",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": "https://t.me/c/1234/77/",
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "rentang username",
        "https://t.me/mychannel/5-7",
        {
            "kind": "link",
            "chat": "mychannel",
            "scheme": "https",
            "prefix": "https://t.me/mychannel/",
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "rentang privat",
        "tg://openmessage?user_id=-1001234&message_id=5-7",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "tg",
            "prefix": "tg://openmessage?user_id=-1001234&message_id=",
            "ids": [5, 6, 7],
            "is_range": True,
            "count": 3,
        },
    ),
    (
        "rentang satu pesan",
        "https://t.me/c/1234/5-5",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": [5],
            "is_range": True,
            "count": 1,
        },
    ),
    (
        "batas atas seribu pesan",
        "https://t.me/c/1234/1-1000",
        {
            "kind": "link",
            "chat": CHAT,
            "scheme": "https",
            "prefix": PREFIX,
            "ids": list(range(1, 1001)),
            "is_range": True,
            "count": 1000,
        },
    ),
    (
        "lebih dari seribu pesan",
        "https://t.me/c/1234/1-1001",
        {
            "kind": "error",
            "type": "TgLinkException",
            "text": "Message range is too large: 1001 (max 1000).",
        },
    ),
    (
        "dua chat berbeda",
        f"{PREFIX}5-https://t.me/c/999/7",
        {
            "kind": "error",
            "type": "TgLinkException",
            "text": "The two links in a range must point to the same chat.",
        },
    ),
    (
        "dua topik berbeda",
        "https://t.me/c/1234/77/5-https://t.me/c/1234/88/7",
        {
            "kind": "error",
            "type": "TgLinkException",
            "text": "The two links in a range must point to the same topic.",
        },
    ),
    (
        "id nol",
        "https://t.me/c/1234/0",
        {
            "kind": "error",
            "type": "TgLinkException",
            "text": "Wrong link format!",
        },
    ),
]

RANGE_LINKS = [
    {
        "name": "rentang dengan bot yang bisa membaca",
        "link": "https://t.me/c/1234/5-7",
        "bot": ok_at(5, 6, 7),
        "expect": {
            "calls": [
                {"client": "bot", "chat_id": CHAT, "message_ids": 5},
            ],
            "outcome": {
                "kind": "links",
                "links": [f"{PREFIX}5", f"{PREFIX}6", f"{PREFIX}7"],
                "session": "bot",
            },
        },
    },
    {
        "name": "pesan pertama terhapus, ujung lain yang diprobe",
        "link": "https://t.me/c/1234/5-7",
        "bot": Behavior(
            lambda i: Message(5, empty=True) if i == 5 else Message(7)
        ),
        "expect": {
            "calls": [
                {"client": "bot", "chat_id": CHAT, "message_ids": 5},
                {"client": "bot", "chat_id": CHAT, "message_ids": 7},
            ],
            "outcome": {
                "kind": "links",
                "links": [f"{PREFIX}5", f"{PREFIX}6", f"{PREFIX}7"],
                "session": "bot",
            },
        },
    },
    {
        "name": "bot buta pada kedua ujung, sesi user diprobe",
        "link": "https://t.me/c/1234/5-7",
        "bot": Behavior(lambda _i: None),
        "user": ok_at(5, 6, 7),
        "expect": {
            "calls": [
                {"client": "bot", "chat_id": CHAT, "message_ids": 5},
                {"client": "bot", "chat_id": CHAT, "message_ids": 7},
                {"client": "user", "chat_id": CHAT, "message_ids": 5},
            ],
            "outcome": {
                "kind": "links",
                "links": [f"{PREFIX}5", f"{PREFIX}6", f"{PREFIX}7"],
                "session": "user",
            },
        },
    },
    {
        "name": "bot buta tanpa sesi user",
        "link": "https://t.me/c/1234/5-7",
        "bot": Behavior(lambda _i: None),
        "expect": {
            "calls": [
                {"client": "bot", "chat_id": CHAT, "message_ids": 5},
                {"client": "bot", "chat_id": CHAT, "message_ids": 7},
            ],
            "outcome": {
                "kind": "error",
                "type": "TgLinkException",
                "text": "Private: Can't get this message!",
            },
        },
    },
    {
        "name": "rentang privat tanpa sesi user",
        "link": "tg://openmessage?user_id=-1001234&message_id=5-7",
        "bot": ok_at(5, 6, 7),
        "expect": {
            "calls": [],
            "outcome": {
                "kind": "error",
                "type": "TgLinkException",
                "text": "USER_SESSION_STRING required for this private link!",
            },
        },
    },
    {
        "name": "rentang privat dengan sesi user",
        "link": "tg://openmessage?user_id=-1001234&message_id=5-7",
        "bot": ok_at(5, 6, 7),
        "user": ok_at(5, 6, 7),
        "expect": {
            "calls": [
                {
                    "client": "user",
                    "chat_id": CHAT,
                    "message_ids": 5,
                },
            ],
            "outcome": {
                "kind": "links",
                "links": [
                    "tg://openmessage?user_id=-1001234&message_id=5",
                    "tg://openmessage?user_id=-1001234&message_id=6",
                    "tg://openmessage?user_id=-1001234&message_id=7",
                ],
                "session": "user",
            },
        },
    },
]


# --------------------------------------------------------------------------
# pembanding
# --------------------------------------------------------------------------


def _diff(label: str, old: dict, new: dict) -> list[str]:
    if old == new:
        return []
    return [f"{label}: HEAD={old!r} baru={new!r}"]


def _diff_expected(label: str, expected: dict, actual: dict) -> list[str]:
    if expected == actual:
        return []
    return [f"{label}: ekspektasi={expected!r} aktual={actual!r}"]


async def _compare_single(old, new) -> list[str]:
    problems = []
    for scenario in SINGLE_LINKS:
        before = await run_scenario(old, scenario)
        after = await run_scenario(new, scenario)
        problems += _diff(scenario["name"], before, after)
    return problems


async def _compare_divergent(new) -> list[str]:
    problems = []
    for scenario in DIVERGENT_LINKS:
        actual = await run_scenario(new, scenario)
        problems += _diff_expected(scenario["name"], scenario["expect"], actual)
    return problems


def _compare_parse(new) -> list[str]:
    problems = []
    for name, address, expected in PARSE_EXPECTATIONS:
        actual = describe_parse(new, address)
        problems += _diff_expected(name, expected, actual)
    return problems


async def _compare_ranges(new) -> list[str]:
    problems = []
    for scenario in RANGE_LINKS:
        actual = await run_scenario(new, scenario)
        problems += _diff_expected(scenario["name"], scenario["expect"], actual)
    return problems


async def _run() -> list[str]:
    _build_cage()
    new = _load(NEW_NAME, ROOT / TARGET)
    old = _load_from_git(OLD_NAME)
    return (
        await _compare_single(old, new)
        + await _compare_divergent(new)
        + _compare_parse(new)
        + await _compare_ranges(new)
    )


def main() -> int:
    problems = asyncio.run(_run())
    total = (
        len(SINGLE_LINKS)
        + len(DIVERGENT_LINKS)
        + len(PARSE_EXPECTATIONS)
        + len(RANGE_LINKS)
    )
    if problems:
        print(f"{len(problems)} dari {total} skenario berbeda:")
        for problem in problems:
            print(f"  ! {problem}")
        print(DIVERGENCES)
        return 1
    print(f"{total} skenario identik/ sesuai ekspektasi")
    print(DIVERGENCES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
