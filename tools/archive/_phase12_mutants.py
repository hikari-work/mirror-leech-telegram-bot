"""Cek mutasi untuk harness diferensial Fase 12.

Harness yang mengatakan "identik" belum membuktikan apa-apa sampai ia terbukti
bisa melihat perbedaan. Skrip ini merusak `message_utils.py` satu perubahan
kecil sekaligus dan menuntut `_phase12_diff.py` gagal pada setiap mutasi.

Mutasi yang lolos bukan berarti lulus: itu lubang di harness, atau baris yang
memang tidak berpengaruh -- dan yang mana harus ditentukan, bukan diasumsikan.

Berkas sumber ditulis ulang sementara lalu dikembalikan di blok ``finally``.
Jalankan dari akar repo: ``python tools/archive/_phase12_mutants.py``
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = ROOT / "bot/helper/telegram/message_utils.py"
HARNESS = ROOT / "tools/archive/_phase12_diff.py"

# (nama, teks yang dicari, pengganti). Tiap entri harus cocok tepat satu kali;
# kalau tidak, mutasinya sudah basi dan skrip menolak menghitungnya.
MUTANTS = [
    (
        "batas rentang tidak diurutkan",
        'first_id, last_id = sorted((int(match["first"]), int(match["last"])))',
        'first_id, last_id = int(match["first"]), int(match["last"])',
    ),
    (
        "penanda -100 hilang",
        'return int(f"-100{raw}")',
        "return int(raw)",
    ),
    (
        "validasi chat yang sama dilonggarkan",
        "if left.scheme != right.scheme or left.chat != right.chat:",
        "if left.scheme != right.scheme and left.chat != right.chat:",
    ),
    (
        "tanda hubung ikut masuk ke id",
        'int(match["last"])))',
        'int(match["last"] + "-")))',
    ),
    (
        "placeholder kosong dianggap terbaca",
        'if message is not None and not message.empty:\n'
        '            return message, "bot"',
        'if message is not None:\n            return message, "bot"',
    ),
    (
        "batas seribu pesan dihapus",
        "if parsed.count > MAX_RANGE_MESSAGES:",
        "if parsed.count > MAX_RANGE_MESSAGES and False:",
    ),
    (
        "probe hanya di ujung pertama",
        "for message_id in dict.fromkeys((parsed.first_id, parsed.last_id)):\n"
        "            message = await TgClient.bot.get_messages(",
        "for message_id in (parsed.first_id,):\n"
        "            message = await TgClient.bot.get_messages(",
    ),
]


def _run_harness() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HARNESS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def main() -> int:
    original = SOURCE.read_text()
    survivors = []
    broken = []
    try:
        for name, find, replace in MUTANTS:
            if original.count(find) != 1:
                broken.append((name, f"anchor cocok {original.count(find)} kali"))
                print(f"  ! basi      {name}")
                continue
            SOURCE.write_text(original.replace(find, replace))
            code, out = _run_harness()
            SOURCE.write_text(original)
            if code == 0:
                survivors.append((name, out.strip().splitlines()[-1]))
                print(f"  LOLOS     {name}")
            else:
                print(f"  tertangkap {name}")
    finally:
        SOURCE.write_text(original)

    total = len(MUTANTS)
    caught = total - len(survivors) - len(broken)
    print(f"\n{caught}/{total} mutasi tertangkap")
    for name, why in broken:
        print(f"  ! anchor basi: {name} ({why})")
    for name, last in survivors:
        print(f"  ! lolos: {name} -- harness bilang: {last}")
    return 1 if survivors or broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
