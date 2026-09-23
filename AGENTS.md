# AGENTS.md — cara mengubah code di repo ini

Satu aturan mengikat sisanya: **tiap commit lulus tiga gerbang, membawa test
yang sudah dibuktikan bisa gagal, dan code yang disentuhnya beranotasi tipe.**
Sisa dokumen ini perinciannya.

Angka yang dikutip di bawah adalah snapshot 2026-08-24 (`c4f47c5`). Ukur ulang,
jangan percaya angkanya — yang mengikat adalah perintah dan targetnya.

---

## 1. Interpreter — salah pilih, code yang sehat terlihat rusak

Tiga tool, tiga lokasi berbeda. Ini bukan detail kosmetik: `pytest` yang salah
melaporkan **6 collection error** `ModuleNotFoundError: No module named
'pyrogram'`, dan itu gampang dibaca sebagai "code-nya rusak" padahal cuma
interpreternya keliru.

| Tool | Jalankan dengan | Kenapa |
|---|---|---|
| pytest | `.venv/bin/python -m pytest` | Dependensi ada di `.venv` (`kurigram` → modul `pyrogram`). `pytest` di PATH (`~/.local/bin`) memakai site-packages sistem yang tidak punya itu. |
| ruff | `ruff check .` | Hanya ada di PATH (`~/.local/bin/ruff`), **tidak** di `.venv`. |
| pyrefly | `.venv/bin/pyrefly check` | Hanya ada di `.venv`, **tidak** di PATH. |

`.venv` dibuat tanpa system-site-packages, jadi `.venv/bin/python` adalah satu-satunya
interpreter yang melihat requirements lengkap.

**Kalau Anda agent yang jalan di bawah hook RTK:** hook itu memangkas output
ruff (43 baris mentah → 28 baris, baris terakhir terpotong `b...`). Untuk
angka dan daftar temuan yang utuh — yang jadi dasar diff di §2 — lewati
filternya:

```bash
rtk proxy 'ruff check . --output-format concise'
```

---

## 2. Tiga gerbang

Jalankan ketiganya sebelum commit. Bukan salah satu.

```bash
.venv/bin/python -m pytest -q                                # 1. test
ruff check .                                                 # 2. lint
.venv/bin/pyrefly check --baseline pyrefly-baseline.json      # 3. tipe
```

| Gerbang | Target | Snapshot 2026-08-24 |
|---|---|---|
| pytest | 0 failed | 872 passed, 0 failed (~24 s) |
| ruff | 0 temuan | **41 temuan** — lihat §5 |
| pyrefly | 0 errors | 0 errors (36 suppressed) |

**Soal 41 temuan ruff itu.** Semuanya utang yang masuk *setelah* refactor
Fase 10 selesai, dan tidak satu pun tercatat di ledger `per-file-ignores`
(§5) — jadi bukan utang yang disahkan, tapi regresi yang lolos. Sebarannya:
`bot/core/handlers.py` (18× E501), `hosts/vidara.py` (5), `pornhub_scraper.py`
(6), plus I001 tersebar dan 3 di `tests/`.

Konsekuensinya untuk pekerjaan Anda: **jangan pakai angka 41 sebagai izin.**
Bandingkan sebelum dan sesudah supaya temuan Anda tidak menyamar sebagai
temuan yang sudah ada:

```bash
ruff check . --output-format concise | sort > /tmp/before.txt   # sebelum menyentuh apa pun
# ... kerjakan perubahannya ...
ruff check . --output-format concise | sort > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt                            # harus kosong, atau hanya berisi baris yang hilang
```

File yang Anda sentuh idealnya keluar dari daftar itu sekalian. Kalau tidak,
minimal jangan menambah barisnya.

---

## 3. Type hints

`pyrefly` adalah pemeriksa tipenya; konfigurasi di `[tool.pyrefly]`
(`pyproject.toml`), cakupan `bot` + `web`.

**Aturannya:** tiap fungsi/method yang Anda tambah — atau yang signature-nya
Anda ubah — dapat anotasi parameter dan anotasi return.

Yang perlu diketahui: **ruff tidak akan menangkap hint yang hilang.** Aturan
`ANN` (flake8-annotations) sengaja tidak diaktifkan, karena code lama punya
2255 temuan ANN dan menyalakannya akan menenggelamkan yang baru. Artinya
kelengkapan anotasi ditegakkan oleh review, bukan oleh linter. Periksa diff
Anda sendiri — dan hanya baris yang Anda sentuh:

```bash
ruff check <file-yang-diubah> --select ANN
```

Gaya rumah, sesuai code yang sudah ada:

- PEP 604 untuk union: `FloodWait | FloodPremiumWait`, `str | None`. Bukan `Optional[...]`.
- `collections.abc` untuk `Callable`/`Iterable`, bukan `typing`.
- `from __future__ import annotations` ditambahkan **kalau butuh** forward
  reference atau ingin menjaga anotasi tetap murah — 23 dari 145 file di
  `bot`/`web` memakainya, jadi bukan kewajiban.
- Kalau banyak call site membaca atribut `self` yang di-set di file lain,
  tuliskan kontraknya sekali di satu class annotation-only yang inert.
  Presedennya `bot/helper/task/_host.py` (41 atribut, tanpa `__init__`,
  tanpa nilai — komposisinya tidak mengubah apa pun saat runtime).
- Sebab yang menyuapi banyak temuan sekaligus di-root-cause, bukan dianotasi
  satu-satu di tiap call site. Presedennya `flood_seconds()` di
  `bot/helper/telegram/flood.py`: satu fungsi menggantikan lima
  `sleep(f.value * slack)` yang berhitung di atas tipe yang mungkin `str`.

**Yang tidak boleh:**

- `# type: ignore` untuk melewati gerbang. Cari akarnya. Kalau temuannya
  memang bug pihak ketiga, baru baseline-kan, dengan komentar yang menjelaskan
  kenapa — 5 entri yang ada semuanya satu bug pyrogram (`thumb: Union[str,
  BinaryIO] = None` di signature-nya sendiri).
- `pyrefly check --update-baseline` untuk membungkam temuan baru. Perintah itu
  hanya untuk saat utang benar-benar dibereskan (entri hilang) atau sengaja
  diterima setelah dipikirkan.

Baseline dipilih alih-alih mematikan kode-error per file justru karena bedanya:
mematikan `bad-assignment` untuk satu modul juga menelan `bad-assignment`
*berikutnya* di modul itu; baseline mencatat temuan satu per satu, jadi yang
baru tetap dilaporkan meski jenis dan file-nya sudah pernah muncul.

---

## 4. Test

**Tiap perubahan perilaku membawa test, dan test itu sudah dibuktikan gagal
tanpa perubahannya.** Pembuktian itu ditulis di body commit — bukan diklaim,
tapi dilaporkan. Test yang tidak pernah dilihat gagal tidak menjamin apa pun.

Letak dan bentuk:

- `tests/test_<subjek>.py`. `asyncio_mode = auto`, jadi `async def test_...`
  tidak perlu marker. Marker yang dikenal cuma `slow`, dan `--strict-markers`
  aktif — marker yang salah tulis akan error, bukan diam.
- Mengimpor modul `bot` menarik seluruh rantainya (lxml, cloudscraper,
  pyrogram), dan `bot/__init__.py` punya efek samping saat import
  (`uvloop.install()`, bikin event loop, buka `log.txt`). Untuk menguji satu
  modul daun, **stub package-nya** alih-alih mengimpor rantai itu — preseden:
  fixture `bunkr` dan `vidara` di `tests/conftest.py` memuat satu file host
  lewat `spec_from_file_location` dengan `_common`/`registry` yang dipalsukan.
- `DOWNLOAD_DIR = "/app/downloads/"` hardcoded; test yang menyentuh path perlu
  monkeypatch.

**Untuk refactor yang menjanjikan perilaku tidak berubah**, dua test saja tidak
cukup — pola yang dipakai repo ini ada dua lapis:

1. **Differential harness** di `tools/archive/` — memuat modul versi pra-refactor
   langsung dari git di samping versi working tree, menjalankan keduanya lewat
   skenario yang sama, lalu membandingkan **panggilan** (method mana, argumen
   apa, urutan apa), **log**, **state**, dan **hasil** (nilai kembali atau
   exception). Preseden: `tools/archive/_phase11b_diff.py`, 103 skenario.
2. **Mutation check** — harness yang melaporkan "identical" belum berarti apa
   pun sampai terbukti bisa membedakan. Script mutasi merusak code baru satu
   perubahan kecil sekaligus dan menuntut harness-nya gagal di setiap mutasi.
   Preseden: `tools/archive/_phase11b_mutants.py`, 62 mutasi, 62 tertangkap.
   **Mutan yang lolos bukan lulus** — itu entah lubang di harness, entah baris
   yang tidak berpengaruh, dan mana dari keduanya harus dipastikan, bukan
   diasumsikan.

`tests/` dan `tools/` di luar cakupan pyrefly (164 temuan, akan menenggelamkan
temuan dari code yang ship), tapi **tetap** di dalam cakupan ruff.

---

## 5. Ledger lint — menyusut, tidak pernah bertambah

`[tool.ruff.lint.per-file-ignores]` di `pyproject.toml` adalah daftar utang
yang sudah ada sebelum refactor, dicabut per fase. Aturannya satu arah: **baris
dihapus saat file dibereskan; menambah baris baru adalah regresi.** Kalau Anda
menyentuh file yang punya entri, bereskan temuan yang tersisa dan hapus
barisnya.

Memastikan satu direktori benar-benar tidak punya utang — bukan sekadar
utangnya sedang disembunyikan:

```bash
ruff check bot/helper/upload/ --config 'lint.per-file-ignores = {}'
```

`upload/` sekarang bersih dengan perintah itu. Kalau nanti ada yang
menambahkan barisnya kembali, itu regresi, bukan penyesuaian.

Ambang yang berlaku: `line-length = 88`, `target-version = "py314"`,
`max-complexity = 10`. `.venv` dan `qBittorrent` dikecualikan.

Untuk menilai kapan sebuah baris ledger sudah boleh dicabut (dan untuk melihat
apakah perubahan Anda menambah kompleksitas):

```bash
.venv/bin/python tools/complexity_report.py --summary
.venv/bin/python tools/complexity_report.py --diff tools/baseline.txt
```

CX-nya dihitung dengan pendekatan yang sama dengan C901 ruff, jadi angkanya
sebanding. Catat bahwa target §5 `REFACTOR_PLAN.md` belum semuanya tercapai
(`cx_max` masih 69, target ≤ 15) — jangan tambah, dan kurangi kalau lewat.

---

## 6. Alur satu perubahan

1. **Baca dulu.** `REFACTOR_PLAN.md` mencatat kenapa banyak hal berbentuk
   seperti sekarang, termasuk asimetri yang disengaja. Melawan salah satunya
   tanpa tahu itu disengaja biasanya jadi bug.
2. **Rekam baseline ruff** (§2) sebelum menyentuh apa pun.
3. **Satu perubahan logis per commit.** Ekstraksi dan perbaikan perilaku tidak
   digabung — kalau refactor mengubah perilaku, perubahan perilakunya jadi
   commit `fix:`/`feat:` tersendiri (preseden: empat `eval()` di Fase 7).
4. **Tulis testnya, lihat gagal, baru perbaiki.**
5. **Anotasi apa pun yang Anda tulis** (§3).
6. **Jalankan tiga gerbang** (§2). Tidak ada yang di-skip karena "cuma ubah
   satu baris".
7. **Cabut baris ledger** kalau file yang disentuh sudah bersih (§5).
8. **Commit** (§7).

Selesai berarti: 0 test gagal, tidak ada temuan ruff baru, pyrefly 0 errors,
code baru beranotasi, dan test barunya sudah terbukti bisa gagal. Kalau ada
bagian yang tidak bisa dituntaskan, katakan bagian mana dan kenapa — jangan
diamkan.

---

## 7. Commit

Ringkasan: `<type>: <imperatif huruf kecil>` — `feat`, `fix`, `refactor`,
`perf`, `docs`. Body menjelaskan **kenapa**, bukan mengulang diff: masalah apa
yang ada sebelumnya, apa yang berubah secara perilaku, dan hasil verifikasinya
(jumlah test, skenario harness, mutasi tertangkap, hasil di lingkungan nyata
kalau ada). Akhiri dengan:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Bahasa.** Ini bukan preferensi, ini pola yang sudah konsisten di repo:

| Di mana | Bahasa |
|---|---|
| Commit message | Inggris |
| Docstring & komentar di `bot/`, `web/`, `tests/` | Inggris |
| Dokumen proses (`AGENTS.md`, `REFACTOR_PLAN.md`), komentar `pyproject.toml`, docstring `tools/` | Indonesia |

Commit dan push hanya kalau diminta.
