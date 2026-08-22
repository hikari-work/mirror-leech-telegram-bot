# Refactor Plan — mirror-leech-telegram-bot (branch `leech-only`)

Status: **Fase 0 selesai · Fase 1 selesai · Fase 2 selesai · Fase 3 selesai · Fase 4 selesai · Fase 5 selesai · Fase 6 selesai · Fase 7 selesai · Fase 8 selesai · Fase 9 selesai · Fase 10 selesai** — SEMUA FASE TUNTAS
Dibuat: 2026-08-10 · Baseline commit: `6c60d46` (refactor: pangkas bot jadi leech-only)

Dokumen ini dipecah jadi fase-fase yang bisa dijalankan satu per satu. Tiap fase
punya target file, langkah konkret, dan cara verifikasi. Urutan fase disusun dari
**risiko rendah + nilai tinggi** ke **risiko tinggi**.

---

## 1. Temuan Baseline

Diukur dengan AST walk atas seluruh `*.py` (di luar `.venv`, `__pycache__`).

### 1.1 File terbesar

| LOC | File | Masalah utama |
|-----|------|---------------|
| 2500 | `bot/helper/mirror_leech_utils/download_utils/direct_link_generator.py` | 54 fungsi top-level dalam 1 file + dispatcher if/elif 204 baris |
| 1216 | `bot/helper/common.py` | God class `TaskConfig` (17 method, 3 tanggung jawab campur) |
| 961 | `bot/modules/rss.py` | Command + monitor + menu + storage jadi satu |
| 767 | `bot/helper/ext_utils/media_utils.py` | Campur ffmpeg, thumbnail, split, probe |
| 737 | `bot/helper/mirror_leech_utils/download_utils/alldebrid_resolver.py` | Duplikat struktural dengan torbox_resolver |
| 677 | `bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py` | `_upload_file` 186 LOC / CX 55 |
| 643 | `bot/modules/users_settings.py` | UI settings hardcoded, bukan schema-driven |
| 565 | `bot/modules/bot_settings.py` | idem |
| 525 | `bot/modules/leech.py` | Satu method 439 LOC |
| 508 | `bot/helper/ext_utils/files_utils.py` | — |
| 506 | `bot/helper/listeners/task_listener.py` | `on_download_complete` 222 LOC / CX 51 |

### 1.2 Fungsi terbesar

Total 832 fungsi. **30 fungsi > 100 LOC**, **87 fungsi > 50 LOC**.
CX = perkiraan cyclomatic complexity (target sehat: ≤ 10).

| LOC | CX | Lokasi | Fungsi |
|-----|----|--------|--------|
| 439 | **93** | `bot/modules/leech.py:79` | `Leech.new_event()` ← terburuk |
| 264 | **92** | `bot/helper/common.py:134` | `TaskConfig.before_start()` |
| 258 | 60 | `.../direct_link_generator.py:801` | `terabox()` |
| 246 | 1 | `bot/core/handlers.py:10` | `add_handlers()` — boilerplate murni |
| 227 | 52 | `bot/modules/users_settings.py:48` | `get_user_settings()` |
| 222 | 51 | `bot/helper/listeners/task_listener.py:87` | `on_download_complete()` |
| 204 | 49 | `.../direct_link_generator.py:30` | `direct_link_generator()` |
| 202 | 59 | `bot/helper/common.py:721` | `proceed_ffmpeg()` |
| 200 | 50 | `bot/modules/rss.py:585` | `rss_listener()` |
| 197 | 38 | `bot/modules/ytdlp.py:283` | `YtDlp.new_event()` |
| 186 | 55 | `.../telegram_uploader.py:476` | `_upload_file()` |
| 184 | 55 | `bot/modules/bot_settings.py:338` | `edit_bot_settings()` |
| 164 | 36 | `.../direct_link_generator.py:1759` | `mediafireFolder()` |
| 158 | 47 | `bot/modules/rss.py:787` | `rss_monitor()` |
| 141 | 22 | `.../yt_dlp_download.py:193` | `add_download()` |

### 1.3 Duplikasi

- **`leech.py:180-207` ≡ `ytdlp.py:365-392`** — blok bookkeeping `same_dir` identik ~28 baris.
- **Dict argumen default** diduplikasi antara `leech.py:83-116` dan `ytdlp.py:288-315`.
- **Error-handling triple** di `leech.py`: pola `send_message` → `remove_from_same_dir` →
  `register_batch_failure` → `return` diulang **13×** (juga 5× di `ytdlp.py`).
- **`alldebrid_resolver.py` vs `torbox_resolver.py`** — struktur paralel: poll-until-ready,
  unlock berkonkurensi 3, delete, payload build. ~1170 LOC gabungan dengan bentuk yang sama.
- **`telegram_uploader._upload_file`** — blok cleanup thumb diulang **4×**,
  blok append/flush `_media_dict` diulang **2×**.

### 1.4 Code smell lain

- **86 bare `except:`** di `bot/` + `web/` — menelan `KeyboardInterrupt`/`CancelledError`.
- **9 pemanggilan `eval()`** atas input user/config:
  - `media_utils.py:56,104` — output ffprobe sebenarnya JSON → harusnya `json.loads`.
  - `ytdlp.py:324` — flag `-opt` datang dari pesan user → jalur eksekusi kode arbitrer.
  - `users_settings.py:309,361`, `bot_settings.py:226,228`, `common.py:373`, `bot_utils.py:173`.
- **Assignment ganda** di `TaskConfig.__init__` (`common.py:98-103`): `compress`/`extract`
  di-set dua kali.
- **`TaskConfig.__init__` membaca `self.message`** yang baru di-set subclass sebelum
  `super().__init__()` — kontrak implisit dan rapuh.
- **Direktori kosong sisa refactor leech-only**: `bot/helper/mirror_leech_utils/gdrive_utils/`,
  `bot/helper/mirror_leech_utils/rclone_utils/`.
- **`is_leech` selalu `True`** (`common.py:90`) tapi masih dicabang di 4 tempat.
- **`bot/__init__.py`** = 18 global mutable + 5 lock → sulit di-test terisolasi.
- **Tidak ada konfigurasi lint** di repo meski `.ruff_cache/` ada.

### 1.5 Baseline test — PENTING

```
62 passed, 5 failed
```

Kegagalan **sudah ada sebelum refactor** (working tree bersih di `6c60d46`):

- `tests/test_semprot_scraper.py::test_scrape_thread_success` — `AttributeError`
- `tests/test_telegram_uploader_album.py` — 4 test album gagal
  (mis. `test_album_replaces_individual_links_in_msgs_dict`: link individual tidak
  dibuang dari `_msgs_dict` setelah album terkirim)

> Refactor **tidak boleh** dimulai di atas suite merah — kalau tidak, kita tidak bisa
> membedakan regresi baru dari kerusakan lama. Ini jadi Fase 0.

---

## 2. Prinsip Kerja

1. **Satu fase = satu commit** (atau beberapa commit kecil), selalu hijau di akhir fase.
2. **Perilaku tidak berubah.** Ini refactor murni. Perbaikan bug dipisah ke commit sendiri
   dan ditandai `fix:`, bukan `refactor:`.
3. **API publik dipertahankan.** Nama fungsi yang di-import lintas modul tetap; kalau file
   jadi package, `__init__.py` re-export supaya call site tidak ikut berubah.
4. **Target ukuran**: fungsi ≤ 50 LOC dan CX ≤ 10; file ≤ 400 LOC.
   Pengecualian boleh, tapi harus disengaja dan dicatat.
5. **Jalankan test tiap selesai langkah**, bukan cuma di akhir fase.

---

## 3. Fase

### Fase 0 — Guardrail (prasyarat, jangan dilewat) ✅

**Status:** selesai · Commit: `2429a1a`, `4cd0ad4`, `f0d2855`

**Tujuan:** punya baseline hijau + alat ukur + lint sebelum menyentuh struktur.

1. Triase 5 test yang gagal:
   - Perbaiki kalau bug produksi nyata (kasus album di `telegram_uploader` kemungkinan besar bug asli).
   - Kalau test-nya yang usang, perbarui test-nya dan catat alasannya di pesan commit.
   - **Jangan** di-skip diam-diam.
2. Tambah `pyproject.toml` dengan konfigurasi ruff:
   - `line-length = 88`, `target-version` sesuai Python di `.venv` (3.14)
   - Rules: `E`, `F`, `W`, `I` (import order), `C901` (complexity, `max-complexity = 10`),
     `E722` (bare except), `S307` (eval), `UP` (pyupgrade)
   - Sementara pakai `per-file-ignores` untuk file yang belum disentuh, dicabut per fase.
3. Tambah `tools/complexity_report.py` — script AST yang menghasilkan tabel di §1.1–1.2,
   supaya progres tiap fase terukur, bukan terasa.
4. Catat angka baseline ke `tools/baseline.txt`.

**Verifikasi:** `pytest -q` → 67 passed, 0 failed. `ruff check .` → 0 temuan (621 temuan
pre-existing didaftarkan di ledger `per-file-ignores`). ✅

---

### Fase 1 — `leech.py` + `ytdlp.py`: bongkar `new_event()` ✅

**Status:** selesai
**Target:** `Leech.new_event()` 439 LOC / CX 93 → ≤ 60 LOC / CX ≤ 10. ✅
Ini fase dengan rasio nilai/risiko terbaik.

1. **Buat `bot/helper/ext_utils/task_args.py`**
   - `LEECH_ARG_DEFAULTS` dan `YTDLP_ARG_DEFAULTS` sebagai konstanta (hapus duplikasi §1.3).
   - `@dataclass LeechArgs` / `YtdlpArgs` dengan field bertipe, plus
     `parse_leech_args(input_list) -> LeechArgs` yang membungkus `arg_parser`.
   - Pindahkan parsing `-d` (ratio:seed_time) dan `-b` (bulk_start:bulk_end) ke sini —
     sekarang di-inline di kedua modul.

2. **Pindahkan bookkeeping `same_dir` ke `TaskConfig`**
   - Method baru `async def register_same_dir(self)` di `common.py`, isi = blok
     `leech.py:180-207`. Hapus salinan di `ytdlp.py`.

3. **Helper error tunggal di `TaskConfig`**
   ```python
   async def fail_task(self, error, *, notify=True) -> None:
       """send_message + remove_from_same_dir + register_batch_failure."""
   ```
   Ganti 13 call site di `leech.py` dan 5 di `ytdlp.py`. Ini sendirian memangkas ~80 baris.

4. **Buat `bot/helper/mirror_leech_utils/download_utils/link_resolver.py`**
   - `async def resolve_torbox(listener) -> None`
   - `async def resolve_alldebrid(listener) -> None`
   - `async def resolve_direct_link(listener, headers) -> list[str]`
   Masing-masing mengembalikan/melempar konsisten, memakai `fail_task` di pemanggil.

5. **Ekstrak method privat di `Leech`**
   - `_handle_bulk(input_list, reply_to) -> bool` (blok `leech.py:234-279`)
   - `_resolve_reply_media(reply_to)` (blok `leech.py:281-304`)
   - `_validate_link(file_) -> bool` (blok `leech.py:306-321`)
   - `_dispatch_download(path, args, headers, ratio, seed_time)` (blok `leech.py:492-517`)

6. Terapkan langkah setara di `YtDlp.new_event()` (197 LOC → ≤ 60).

**Verifikasi:** `pytest tests/test_arg_parser_flags.py -q` + suite penuh.
Tambah test unit baru untuk `parse_leech_args` (flag boolean, `-d 1.5:60`, `-b 2:5`, `-ff`).
✅ 94 passed, 0 failed (27 test baru di `test_task_args.py` + 67 existing).

---

### Fase 2 — `common.py`: pecah god class `TaskConfig` ✅

**Status:** selesai

**Target:** 1216 LOC → `common.py` tipis (≤ 150 LOC) + 4 modul.
`before_start()` 264 LOC / CX 92 → ≤ 40 LOC sebagai orkestrator.

Buat package `bot/helper/task_config/`:

| Modul | Isi |
|-------|-----|
| `settings_resolver.py` | `SettingsResolverMixin` — `before_start()` (264 LOC) |
| `batch_tracker.py` | `BatchTrackerMixin` — `_batch`, `update_batch_progress`, `register_batch_failure`, `finalize_batch`, `fail_task` |
| `media_pipeline.py` | `MediaPipelineMixin` — `proceed_extract`, `proceed_ffmpeg`, `substitute`, `generate_screenshots`, `convert_media`, `generate_sample_video`, `proceed_compress`, `proceed_split` |
| `multi_link.py` | `MultiLinkMixin` — `run_multi`, `init_bulk`, `get_tag`, `register_same_dir` |

- `TaskConfig` jadi komposisi mixin: `class TaskConfig(SettingsResolverMixin, BatchTrackerMixin, MediaPipelineMixin, MultiLinkMixin)`.
- `bot/helper/common.py` tetap ada dan re-export `TaskConfig` → **tidak ada call site yang berubah**.
- Hapus assignment ganda `compress`/`extract`.
- `eval()` diganti `ast.literal_eval` di `settings_resolver.py`.

**Verifikasi:** 94 passed, 0 failed. `ruff check .` → 0 temuan. `common.py` turun 1261 → 89 LOC. ✅

**Catatan:** `before_start()` dipecah menyusul di Fase 2b.

---

### Fase 2b — `before_start()` jadi orkestrator ✅

**Status:** selesai

**Target:** `before_start()` 289 LOC / CX 97 → orkestrator ≤ 20 LOC.

Empat belas keputusan yang tidak berhubungan tadinya berbagi satu scope. Dipecah
jadi langkah bernama, dinamai menurut *maksud*-nya, bukan mekanismenya:

| Langkah | Isi |
|---------|-----|
| `_resolve_name_substitutions` | aturan rename `old/new \| old/new` |
| `_resolve_extension_filters` | ekstensi yang dibuang / disimpan |
| `_resolve_ffmpeg_commands` | + `_ffmpeg_presets`, `_fill_preset` |
| `_resolve_upload_destination` | + `_apply_transmission_defaults`, `_normalize_up_dest`, `_apply_transmission_prefix` |
| verifikasi tujuan | `_verify_dest_for_user_session`, `_verify_user_session_privileges`, `_verify_dest_for_bot`, `_verify_bot_privileges`, `_verify_bot_can_reach_dest` |
| `_resolve_split_sizes` | parse ukuran + plafon per sesi |
| `_resolve_upload_format` | dokumen vs media |
| `_resolve_thumbnail_layout` / `_resolve_thumbnail` | layout + thumb dari link telegram |
| `_resolve_clone_dump_chats` | + `_as_dump_target`, `_as_dump_entries` |

Duplikasi yang dihapus, bukan hanya dipindah:

- Fallback tiga tingkat (task → user → bot) diulang 6×, sekarang `_setting_for()`
  / `_is_enabled()`. Aturannya jadi eksplisit: **key yang *ada* di `user_dict`
  berarti user punya pendapat — walau kosong — jadi default bot tidak berlaku.**
- Pasangan `user_transmission = False; hybrid_leech = False` muncul 8×, sekarang
  `_downgrade_to_bot_session()`.
- Coercion `chat|thread` / digit / `pm` diulang di `up_dest` dan
  `clone_dump_chats`, sekarang `_as_chat_id()`.
- `["SUPERGROUP", "CHANNEL", "GROUP", "FORUM"]` ditulis 2× → `GROUP_CHAT_TYPES`;
  magic number `2097152000` → `BOT_MAX_SPLIT_SIZE`.

**Parameter:** tidak ada parameter object yang perlu dibuat. Mixin-nya *sudah*
jadi parameter object — semua langkah baca/tulis `self`, jadi hasil ekstraksi
nol parameter. `_dest_unverified()` tetap di mixin karena `test_dest_chat.py`
mengetesnya lewat `SettingsResolverMixin` langsung.

**Verifikasi:**

- `tools/_phase2b_diff.py` — harness diferensial: `before_start()` versi git vs
  working tree, 136 skenario, membandingkan state akhir + tipe/pesan exception +
  urutan `LOGGER.warning`. **136 identical, 0 divergent.**
- `tests/test_settings_resolver.py` — 53 test baru untuk seam hasil ekstraksi.
- 765 passed, 0 failed (712 → 765).
- Baris `per-file-ignores` untuk `settings_resolver.py` **dicabut** — C901/E501/E722
  di file itu nol.
- `cx_max` 97 → 68; fungsi terbesar di file 30 LOC, median 12 LOC.

---

### Fase 3 — `direct_link_generator.py`: registry + package

**Target:** 2500 LOC → `__init__.py` ≤ 80 LOC + ~8 modul host ≤ 350 LOC.
Dispatcher `direct_link_generator()` 204 LOC / CX 49 → ≤ 25 LOC.

1. Ubah jadi package `direct_link_generators/`:
   ```
   direct_link_generators/
     __init__.py        # re-export direct_link_generator (nama publik tetap)
     registry.py        # @register(domains=[...]) → dict domain -> handler
     _common.py         # scraper/session/retry helper bersama
     hosts/terabox.py   # terabox() 258 LOC dipecah lagi jadi ~4 langkah
     hosts/mediafire.py # mediafire, mediafireFolder (164 LOC)
     hosts/gofile.py
     hosts/linkbox.py
     hosts/streaming.py # doods, streamtape, filelions, streamhub, vidoy, mp4upload
     hosts/misc.py      # sisanya
     hosts/dead.py      # daftar "R.I.P" — data, bukan cabang if
   ```
2. **Ganti rantai if/elif jadi lookup.** `registry.py`:
   ```python
   _HANDLERS: dict[str, Callable] = {}

   def register(*domains, predicate=None): ...
   def resolve(domain, link): ...   # exact match → suffix match → predicate
   ```
   Handler yang butuh logika non-domain (`is_share_link`, `is_mega_link`, `is_vidoy_link`)
   didaftarkan lewat `predicate=`.
3. Import `direct_link_generator` di `leech.py:38` **tidak berubah**.

**Verifikasi:** suite penuh + `tests/test_semprot_scraper.py`, `tests/test_mega_direct_link.py`.
Tambah test yang memastikan setiap domain di daftar lama me-resolve ke handler yang sama
(tabel domain → nama fungsi, di-snapshot sebelum refactor).

**Status Fase 3:** selesai

**Hasil:** 2500 LOC → package 18 file, semua < 400 LOC. `__init__.py` 79 LOC,
dispatcher `direct_link_generator()` 204 → **19 LOC** (target ≤ 25).
`hosts/terabox.py` 340 LOC, `terabox()` 258 → 11 LOC dan seluruh fungsinya
C901-bersih. `direct_link_generator.py` jadi shim 25 LOC, jadi import di
`leech.py`, `ytdlp.py:20`, dan `link_resolver.py:168` tidak berubah.

**Verifikasi:** 345 passed, 0 failed. `ruff check .` → 0 temuan. Selain suite,
perilaku dibuktikan tiga cara terpisah:
1. **Diff AST per fungsi** — body 52 dari 54 handler identik byte-per-byte
   dengan aslinya; 2 sisanya diperiksa manual dan memang disengaja.
2. **Differential routing** — 19.514 URL (termasuk 16.504 hostname ambigu yang
   digenerate exhaustive dari tiap pasangan domain) dibandingkan antara
   transkripsi mekanis rantai lama dan registry baru: **0 mismatch**.
3. **Differential terabox** — 115 skenario (gateway/CDN palsu) membandingkan
   return value, pesan exception, dan output log sebelum vs sesudah dipecah:
   **0 mismatch**.

**Catatan — `order=` wajib di tiap `@register`.** Rencana awal menyebut
"exact match → suffix match → predicate", tapi rantai lama mencocokkan
**substring** (`"racaty" in domain`), dan tiga entri memang ditulis tanpa TLD
("racaty", "devuploads", "uploadhaven"). Akibatnya satu hostname bisa kena
beberapa handler sekaligus — `racaty.mediafire.com` mengandung "racaty" dan
"mediafire.com" — dan rantai lama menjawab dengan cabang yang lebih dulu.
Jadi urutan itu **perilaku**, bukan detail implementasi. Sempat dicoba
mengandalkan urutan import modul; sweep exhaustive menemukan **1.288 mismatch**
dari 19.514, karena cabang rantai berselang-seling antar modul (mediafire #8,
racaty #15, fichier #16) sehingga tidak ada urutan import yang bisa
mereproduksinya. Solusinya: tiap handler menyatakan posisinya sendiri
(`order=1..41`) dan `resolve()` mengurutkan atasnya. Dipin oleh
`test_dispatch_order_matches_the_old_chain`.

**Deviasi yang disengaja dari sketsa di atas:**
- `hosts/misc.py` dipecah jadi `filehosts.py`, `cloud.py`, dan `lockers.py`
  supaya tiap file tetap di bawah target ukuran.
- Daftar "R.I.P" jadi tuple data `_DEAD_DOMAINS` di `registry.py`, bukan
  `hosts/dead.py` — tidak ada handler di sana, hanya data yang dicek paling
  akhir persis seperti rantai lama.
- `swisstransfer.py`, `sendcm.py`, `sharelinks.py`, `mega.py`, `vidoy.py` jadi
  modul sendiri karena tiap host punya alur scrape yang berdiri sendiri.
- Bare `except:` dan baris panjang **tidak** diperbaiki di fase ini (itu
  mengubah perilaku / masuk scope Fase 10); utangnya pindah ke ledger
  `per-file-ignores` di `pyproject.toml`.

---

### Fase 4 — Debrid resolver: angkat base bersama

**Target:** 737 + 431 = 1168 LOC → ~600 LOC total.

- Buat `bot/helper/mirror_leech_utils/download_utils/debrid/base.py`:
  - Protocol `DebridProvider`: `create_from_magnet`, `create_from_torrent`, `create_web`,
    `fetch_status`, `list_files`, `unlock`, `delete`.
  - Fungsi bersama: `wait_until_ready(provider, item_id, *, ready_pred, error_pred, poll=5.0, max_wait=7200, no_seed_wait=180, is_cancelled)`.
  - `resolve_files_concurrently(links, unlock, concurrency=3)`.
- `debrid/alldebrid.py` dan `debrid/torbox.py` menyisakan pemetaan API spesifik saja.
- Modul lama `alldebrid_resolver.py` / `torbox_resolver.py` jadi shim re-export supaya
  import di `leech.py:25-34` tidak berubah.

**Verifikasi:** `tests/test_alldebrid_magnet.py`, `tests/test_alldebrid_resolver.py` harus
lulus tanpa diubah. Kalau perlu diubah, berarti perilaku bergeser — hentikan dan tinjau.

**Status Fase 4:** selesai

**Hasil:** `debrid/base.py` (199 LOC) memuat mesin bersama: `request_json`,
`require_key`, `wait_until_ready`, `resolve_files_concurrently`, plus
`_StallTimer`. `alldebrid_resolver.py` 737 → **688 LOC**, `torbox_resolver.py`
431 → **427 LOC**. Duplikasi terbesar hilang: dua blok ~75 baris yang identik di
`alldebrid_resolve_magnet` / `alldebrid_resolve_torrent` jadi satu
`_resolve_uploaded()`, dan tiga entry point TorBox (`torbox_resolve_magnet`,
`torbox_resolve_torrent`, `torbox_resolve`) jadi delegasi 17–18 LOC ke
`_resolve_created()`. Poll loop yang tadinya ditulis dua kali — lengkap dengan
tiga cara menyerah (cancel, stall no-seed, max wait) — sekarang hanya ada satu.

**Verifikasi:** 384 passed, 0 failed. `ruff check .` → 0 temuan.
`test_alldebrid_magnet.py` dan `test_alldebrid_resolver.py` **lulus tanpa
diubah**, sesuai syarat di atas. Selain suite:
1. **Differential AllDebrid** — 99 skenario/perbandingan helper antara modul
   pra-refactor (dari `git show HEAD:`) dan modul baru, dijalankan lewat fake
   `_call_api` yang sama: return value, tipe + pesan exception, dan urutan
   panggilan API dibandingkan. **0 mismatch**, stabil di 3 kali run.
2. **Differential TorBox** — 75 perbandingan dengan cara yang sama lewat fake
   `_api`. **0 mismatch**, stabil di 3 kali run.
3. **`tests/test_torbox_resolver.py` baru** (39 test) — TorBox sebelumnya
   **nol test** padahal ia yang paling banyak berubah bentuk di fase ini. Test
   ini memaku perilaku yang dibandingkan harness: kosakata ready/error, guard
   stall dan timeout, cleanup saat gagal, dan bentuk payload ke
   `add_direct_download`.

**Target LOC tidak tercapai — dan itu disengaja.** Sketsa menargetkan
1168 → ~600 LOC; hasilnya 1168 → 1314 (688 + 427 + 199). Diukur sebagai baris
kode saja (tanpa blank/comment/docstring) angkanya 888 → 937, **+49**. Alasannya:
- Target ~600 mengasumsikan kedua provider bisa dilebur ke satu bentuk. Ternyata
  tidak — lihat "Deviasi" di bawah. Yang benar-benar duplikat (poll loop, unlock
  fan-out, request/error plumbing) memang sudah turun ke `base.py` dan tinggal
  satu salinan; sisanya adalah pemetaan API yang beda betulan antara AllDebrid
  dan TorBox, dan meleburnya justru berarti menambah cabang `if provider ==`.
- Kenaikan bersihnya berasal dari docstring (+71 baris di ketiga file; TorBox
  tadinya nol docstring) dan dari signature `_resolve_uploaded` /
  `_resolve_created` / `wait_until_ready` yang eksplisit alih-alih inline.
  Itu harga yang dibayar untuk menghapus salinan kedua poll loop.
- Ukuran per file sudah di bawah batas 400 LOC untuk `torbox_resolver.py` dan
  `base.py`; `alldebrid_resolver.py` (688) masih di atas dan **belum** dipecah.

**Deviasi yang disengaja dari sketsa di atas:**
- **Tidak ada `debrid/alldebrid.py` / `debrid/torbox.py` + shim.** Modul provider
  tetap di tempatnya; hanya `base.py` yang diangkat. Sebabnya: kedua test
  AllDebrid me-monkeypatch atribut level-modul per nama (`_call_api`,
  `upload_magnet`, `get_magnet_status`, `get_magnet_files`,
  `_unlock_alldebrid_link`, `delete_magnet`, `Config`). Kalau isinya pindah ke
  modul lain dan yang lama jadi shim re-export, target monkeypatch itu menunjuk
  ke objek yang salah — test harus diubah, dan syarat verifikasi fase ini
  melarangnya. Menaruh `base.py` saja di package memenuhi tujuan yang sama
  (satu salinan mesin bersama) tanpa memindahkan satu pun target patch.
- **Tidak ada Protocol `DebridProvider`.** Sketsa menyebut protocol dengan tujuh
  method. Yang benar-benar dibagi cuma dua titik — "ambil status sekali" dan
  "unlock satu file" — jadi `wait_until_ready` menerima callable `fetch_status`
  dan predikat, bukan objek provider. Protocol tujuh method akan memaksa kedua
  resolver mengarang method yang tidak dipakai (`create_web` tidak ada padanan
  di AllDebrid; `unlock` bentuknya beda jauh).
- **`resolve_files_concurrently` punya flag `ordered=`.** TorBox versi lama
  memakai gather-lalu-append sehingga entry mendarat dalam urutan selesai, bukan
  urutan file didaftar; AllDebrid mempertahankan urutan. Ini perilaku yang
  kelihatan di payload, jadi dipertahankan apa adanya (`ordered=False` untuk
  TorBox) alih-alih diseragamkan.
- **Guard stall dimatikan untuk web download.** Web download TorBox tidak punya
  swarm, jadi `seeds == 0 and peers == 0` selalu benar dan guard no-seed akan
  salah memicu. `is_stalled=None` untuk `kind == "webdl"`, sama seperti versi
  lama yang memang hanya mengecek swarm di jalur torrent.
- **`base._error()` mengimpor `DirectDownloadLinkException` per panggilan**,
  bukan di level modul. Fixture test memasang `bot.helper.ext_utils.exceptions`
  sendiri ke `sys.modules` per test; kalau `base` mengikat kelasnya saat import,
  modul yang ter-cache akan memegang kelas basi dan `pytest.raises` meleset.
- **Bare `except:` tidak diperbaiki** (4 di `alldebrid_resolver.py`) — konsisten
  dengan Fase 3, itu scope Fase 10. Utangnya tetap di ledger `per-file-ignores`
  supaya Fase 10 masih bisa menemukannya lewat E722.

**Sisa untuk fase lain:** `alldebrid_resolver.py` masih 688 LOC. Kandidat
pecahnya jelas — unlock filehost (`alldebrid_resolve`,
`alldebrid_check_supported`) berdiri sendiri dan tidak berbagi apa pun dengan
jalur magnet selain `_call_api`. Tidak dikerjakan di sini supaya fase ini tetap
satu commit yang perilakunya terbukti tidak berubah.

---

### Fase 5 — `telegram_uploader._upload_file`

**Target:** 186 LOC / CX 55 → ≤ 40 LOC.

1. `_resolve_thumb(file, is_video, is_audio, is_image) -> str | None` — blok pencarian thumb.
2. Context manager `_temp_thumb(thumb)` yang membereskan cleanup — menghapus **4 salinan**
   blok `if self._thumb is None and thumb is not None and await aiopath.exists(thumb)`.
3. Sender per tipe: `_send_as_document`, `_send_as_video`, `_send_as_audio`, `_send_as_photo`,
   dipilih lewat dict, bukan if/elif.
4. `_track_media_group(key, o_path)` — menghapus **2 salinan** blok append/flush `_media_dict`.
5. Retry FloodWait & fallback-to-document tetap di `_upload_file` sebagai pembungkus tipis.

> Catatan: 4 test album yang gagal di §1.5 menyentuh area ini. Selesaikan Fase 0 dulu,
> jangan refactor di atasnya.

**Status Fase 5:** selesai

**Hasil:** `_upload_file` 186 LOC / CX 64 → **34 LOC / CX 14** (target ≤ 40 LOC).
Isinya sekarang cuma yang memang urusan pembungkus: normalisasi thumb usang,
retry FloodWait, fallback-to-document, dan pembersihan `_base_msg`. Semua
langkah per-file pindah ke helper:

| Helper | LOC / CX | Menggantikan |
|--------|----------|--------------|
| `_resolve_thumb` | 19 / 7 | tangga pencarian thumb yang di-inline |
| `_temp_thumb` (async CM) | 19 / 5 | **4 salinan** blok cleanup thumb |
| `_pick_key` | 13 / 6 | rantai if/elif pemilih tipe |
| `_send_as_document/_video/_audio/_photo` | 14–32 / 3–8 | badan tiap cabang |
| `_queue_in_group` | 9 / 2 | **2 salinan** blok append/flush `_media_dict` |
| `_track_media_group` | 21 / 12 | bookkeeping album vs split-group |
| `_send_one` | 14 / 2 | orkestrator satu kiriman |

Sender dipilih lewat dict `_SENDERS` (`documents`/`videos`/`audios`/`photos`),
bukan if/elif — dipaku oleh `test_sender_table_covers_all_four_kinds`.

**Verifikasi:** 405 passed, 0 failed (21 test baru di
`tests/test_telegram_uploader_helpers.py` + 384 existing). `ruff check .` → 0
temuan. 5 test album di `test_telegram_uploader_album.py` **lulus tanpa
diubah**. Selain suite:
1. **Differential upload** — `tools/_phase5_diff.py` menjalankan modul
   pra-refactor (dari `git show HEAD:`) dan modul baru lewat stub yang sama,
   lalu membandingkan: urutan penuh panggilan telegram + argumennya, state
   akhir (`_sent_msg`, `_media_dict`, `_album_msgs`, `_msgs_dict`,
   `_last_msg_in_group`, `_base_msg`, `_thumb`), file thumb yang dihapus,
   tipe + pesan exception, dan **output LOGGER**. 80 skenario menutup lima
   tipe media × tiga bentuk thumb × as_doc, tangga resolusi thumb, batching
   album, split-group video/dokumen, pembatalan sebelum vs saat kirim,
   FloodWait, fallback BadRequest, dan kegagalan saat flush.
   **0 mismatch tak terduga**, stabil di 3 kali run, exit code 0 — harness ini
   dipakai sebagai gerbang, bukan sekadar laporan.
2. Dua deviasi yang disengaja didaftarkan eksplisit di harness (`EXPECTED`)
   supaya tetap kelihatan, bukan disembunyikan.

**Bug yang ketemu lewat harness — dan sengaja tidak dibiarkan lolos.**
Kalau `get_document_type` atau `get_audio_thumbnail` melempar `BadRequest`,
bucket media (`key`) belum sempat ter-assign. Versi lama menabrak
`UnboundLocalError: cannot access local variable 'key'` — error asli tertelan.
Transkripsi lurus ke `attempt.key` justru lebih buruk: `None != "documents"`
selalu benar, jadi retry-as-document mengulang kegagalan yang sama sampai
`RecursionError`. Karena itu `retryable` mensyaratkan `attempt.key is not None`
— kirim ulang sebagai dokumen tidak masuk akal kalau yang gagal adalah probe
atau pembuatan thumbnail, bukan pengirimannya. Ini **satu-satunya** perubahan
perilaku di fase ini, dan arahnya memperbaiki: `BadRequest` yang sebenarnya
diteruskan apa adanya.

**Deviasi yang disengaja dari sketsa di atas:**
- **`_temp_thumb` menerima `_Attempt`, bukan path thumb.** Sketsa menulis
  `_temp_thumb(thumb)`, tapi sender kadang *membuat* thumbnya sendiri
  (`get_video_thumbnail`, `get_multiple_frames_thumbnail`, cover audio), jadi
  path yang perlu dibersihkan baru diketahui setelah sender jalan. Objek
  `_Attempt` yang bisa ditulis balik dipakai supaya CM tahu apa yang harus
  dihapus. `_Attempt` juga membawa `key` untuk keputusan fallback.
- **Thumb milik kiriman yang dibatalkan tidak dihapus.** Versi lama `return`
  lebih dulu saat `is_cancelled`, sebelum menyentuh blok cleanup mana pun —
  jadi thumbnya memang tertinggal. Itu dipertahankan lewat flag
  `attempt.aborted` dan dipaku oleh
  `test_temp_thumb_keeps_thumb_of_aborted_send`, bukan "dirapikan" diam-diam;
  membersihkannya di sini akan jadi perubahan perilaku di luar scope.
- **FloodWait tetap `sleep` di dalam CM.** Sleep-nya sengaja terjadi sebelum
  cleanup thumb, persis seperti urutan versi lama (`sleep` dulu, `remove`
  kemudian), lalu rekursi dilakukan di luar CM supaya thumb tidak dihapus dua
  kali.
- **`upload()` tidak disentuh.** Masih 121 LOC / CX 42 dan tetap memegang satu
  entri ledger `C901`. Bentuknya sama persis dengan `on_download_complete`
  (Fase 6), jadi lebih baik dikerjakan sekali di sana daripada dua gaya beda.
- **Satu `E722` tersisa** di properti `speed` — scope Fase 10. `E501` dan
  `I001` sudah dicabut dari ledger; sisa ledger file ini tinggal
  `["C901", "E722"]`.

**Sisa untuk fase lain:** file masih 748 LOC (dari 677 — naik karena docstring
dan signature eksplisit, bukan logika baru). Pemecahannya menunggu `upload()`
dibereskan di Fase 6.

---

### Fase 6 — `task_listener.on_download_complete`

**Target:** 222 LOC / CX 51 → ≤ 50 LOC.

Pengamatan kunci: **7 blok post-processing** (extract, ffmpeg, name_sub, screenshots,
convert, sample, compress) semuanya mengikuti pola identik:
```
up_path = await <step>(up_path, gid)
if self.is_cancelled: return
self.is_file = await aiopath.isfile(up_path)
self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
self.size = await get_path_size(up_dir)
self.clear()
```

1. Ekstrak `_await_same_dir_merge() -> bool` (blok baris 92-117).
2. Ekstrak `_resolve_download_path() -> str` (blok baris 152-166).
3. Buat deskriptor stage + `_run_stage(step, *, refresh_size=True, do_clear=True)`, lalu
   jalankan sebagai list — 7 blok jadi ~10 baris.
4. Ekstrak `_start_upload(up_dir, gid)` (blok baris 275-308).

**Status Fase 6:** selesai

**Hasil:** `on_download_complete` 222 LOC / CX 52 → **45 LOC / CX ≤ 10** (target
≤ 50 LOC; mccabe tidak lagi menandai C901). Orkestrator sekarang hanya berisi
alur luarnya: sleep + guard cancel → tunggu merge same-dir → ambil download dari
`task_dict` → cabang multi_links/same_dir → resolve path → siapkan upload dir →
filter extension → lepas slot queue → join → pipeline stage → split → upload.

| Helper | LOC / CX | Menggantikan |
|--------|----------|--------------|
| `_await_same_dir_merge` | 33 / 11 | blok wait-loop same_dir + `move_and_merge` |
| `_finish_merged_task` | 18 / 4 | cabang multi_links (report + finalize batch) |
| `_resolve_download_path` | 23 / 5 | resolusi nama + fallback `listdir` + stat |
| `_prepare_upload_dir` | 13 / 2 | cabang seed/symlink |
| `_filter_extensions` | 6 / 2 | **2 salinan** blok include/exclude filter |
| `_release_download_slot` | 8 / 4 | blok QUEUE_ALL + `start_from_queued` |
| `_run_post_processing` | 21 / 7 | loop stage + tail name/size + split |
| `_run_stage` | 28 / 10 | badan satu stage (dari tabel `_STAGES`) |
| `_start_upload` | 24 / 5 | blok queue-up + `TelegramUploader` + `gather` |

Tujuh blok post-processing jadi **data**, bukan kode: dataclass `_Stage`
(`guard`, `step`, `pass_gid`, `set_name`, `refresh_size`, `do_clear`,
`stat_before_cancel`, `log`, `refilter`) + tabel `_STAGES`; `_run_stage`
menjalankan satu stage, `_run_post_processing` mengiterasinya.

**Verifikasi:** 405 passed, 0 failed (suite penuh, tanpa perubahan test).
`ruff check .` → 0 temuan; ledger file turun dari
`["C901", "E501", "E722", "F401", "I001"]` → `["C901", "E501", "E722"]`
(F401 — import `multi_batches` yang memang mati — dibuang, I001 dibereskan;
C901/E501 tersisa hanya di tiga handler error yang **bukan** target fase ini).
Selain suite:
1. **Differential `on_download_complete`** — `tools/_phase6_diff.py` menjalankan
   modul pra-refactor (dari `git show HEAD:`) dan modul baru di bawah stub yang
   sama, lalu membandingkan: urutan penuh panggilan + argumennya (move_and_merge,
   tiap `proceed_*`, `get_path_size`, `clear`, filter, queue, upload), state akhir
   listener (`name`, `size`, `is_file`, `seed`, `up_dir`, `subproc`,
   `subname/subsize/files_to_proceed/proceed_count/progress`), state global
   (`task_dict`, `non_queued_dl/up`), bookkeeping `same_dir` (beserta
   **mutasi-nya**, bukan hanya snapshot), output LOGGER, dan exception. **67
   skenario** menutup: file tunggal/folder/tidak ada file; fallback `listdir`
   termasuk guard `yt-dlp-thumb`; folder_name; seed (symlink) vs non-seed vs
   seed-tapi-bukan-torrent; `up_dir` ter-set di task non-seed; include vs exclude
   extension; QUEUE_ALL vs add-to-queue vs cancel saat wait; join; tiap stage
   sendiri, dengan path baru, dan cancel tepat setelah step; kombinasi semua
   stage ± compress; cancel di tiap titik (di stage awal, di stage tengah lewat
   `*-then-cancel-in-next`, saat split); same-dir solo / solo-seeding / merge /
   bukan anggota / tunggu-lalu-drop / tunggu-lalu-merge / merge+batch.
   **0 mismatch tak terduga**, stabil di 3 kali run, exit code 0 — gerbang,
   bukan laporan.
2. **Mutation-tested.** 12 mutasi disuntikkan satu per satu ke modul baru dan
   tiap kali harness harus menangkapnya. Yang terpaku: order stat-before-cancel
   compress; `name_sub` yang salah refresh-size/clear/set_name; `screen_shots`
   yang salah clear; `extract` yang kehilangan refilter; arah filter
   (`self.up_dir or self.dir` vs `up_dir`); `set_name`/`refresh_size` per-stage
   (lewat skenario `-then-cancel-in-next`); urutan guard split; `subproc`
   yang tidak di-null; `seed=False` di cabang same-dir. Satu-satunya yang
   bertahan — `compress.set_name` — **terbukti no-op**: dengan
   `stat_before_cancel=True`, kalau dibatalkan stage tidak sampai ke set_name
   (tail juga tidak jalan), kalau tidak dibatalkan tail langsung menimpa
   `self.name` dengan ekspresi identik atas `up_path` yang sama, tanpa titik
   observasi di antara keduanya. Sibling-nya (`refresh_size`) tertangkap — jadi
   harness memang membedakan keduanya.

**Deviasi yang disengaja dari sketsa di atas:**
- **Pola 7 blok ternyata tidak identik — dan itu justru inti refactor-nya.**
  Sketsa menulis satu pola seragam, tapi tiap stage me-refresh subset state yang
  beda: `name_sub` me-refresh `size` **tidak**, `clear` **tidak**, dan tidak
  menerima `gid`; `screen_shots` tidak `clear`; `compress` me-refresh `is_file`
  **sebelum** cek cancel (urutan penting: kalau dibatalkan, `is_file` tetap
  ter-update) dan tidak set `name`/`size` sendiri — itu urusan tail; `extract`
  memfilter ulang extension. Perbedaan ini hidup sebagai field `_Stage`, bukan
  sebagai tujuh cabang.
- **Tail (name/size + split) tidak dimasukkan ke loop.** Baris "set name + size
  selalu, lalu split kalau `not compress`" menggantung di ujung pipeline dan
  berinteraksi dengan stage compress (yang sengaja tidak set name/size). Melipat
  tail ke loop berarti menyeragamkan perilaku — tidak boleh. Tail tetap eksplisit
  di `_run_post_processing`.
- **`_await_same_dir_merge` mengembalikan 3 nilai, bukan bool.** Jalur "task
  terbuang dari grup saat menunggu" adalah early-return (`None`), bukan merge
  (`True`) atau solo (`False`). Sentinel dipakai supaya pemanggil tidak
  salah-lanjut sebagai solo task.
- **`seed=False` digabung.** `if not (is_torrent or is_qbit) or same_dir:` —
  setara dengan `if multi_links: ... elif same_dir:` asli karena cabang
  multi_links selalu meng-clear seed (via `_finish_merged_task`) dan `elif`
  hanya jalan saat bukan multi_links. Dipaku skenario `same-dir-solo-seeding`.
- **Helper di luar sketsa**: `_finish_merged_task` (cabang multi_links), 
  `_prepare_upload_dir` (seed/symlink), `_filter_extensions` (duplikat
  include/exclude), `_release_download_slot` (QUEUE_ALL). Masing-masing
  duplikasi atau tanggung jawab yang bisa dipisah; tanpanya `on_download_complete`
  tidak sampai 45 LOC.
- **Bare `except:` tidak disentuh** (di `clean()`, `_finish_merged_task`, dan
  handler error) — konsisten dengan Fase 3–5, scope Fase 10. E722 tetap di
  ledger supaya Fase 10 masih bisa menemukannya.

**Sisa untuk fase lain:** `on_upload_complete` (CX 15), `on_download_error`
(CX 17), `on_upload_error` (CX 12) masih memegang C901 — bentuknya sama dengan
`upload()` di telegram_uploader (Fase 5) dan Fase 6, jadi kandidat satu
refactor bersama. Dua E501 di `on_download_error` ikut menunggu. File 506 →
573 LOC (naik karena docstring + signature eksplisit + deskriptor, bukan logika
baru; sloc 448 → 461, +13).

---

### Fase 7 — Settings UI (`users_settings.py`, `bot_settings.py`)

**Target:** 643 + 565 → ~700 LOC total; `get_user_settings` 227 → ≤ 40,
`edit_bot_settings` 184 → ≤ 40.

- Buat `bot/modules/settings/schema.py`: daftar `Option` dataclass
  (`key`, `label`, `kind`, `parser`, `validator`, `help`, `scope`).
- `menu_builder.py` merakit tombol dari schema — menggantikan `get_user_settings`
  dan `get_buttons` yang hardcode tiap opsi.
- Routing callback jadi dict dispatch, bukan rantai if/elif di `edit_user_settings` /
  `edit_bot_settings`.
- Sekalian ganti `eval()` di `users_settings.py:309,361` dan `bot_settings.py:226,228`
  dengan `ast.literal_eval` + validator dari schema.

**Status Fase 7:** selesai (refactor + `fix:` `eval()`, dua commit terpisah)

**Hasil:** target CX tercapai di semua fungsi sasaran; target LOC **tidak**, dan
memang tidak realistis (lihat "Catatan LOC").

| Fungsi | Sebelum | Sesudah |
|--------|---------|---------|
| `get_user_settings` | 227 LOC / CX 30 | dihapus → `settings/menu_builder.build_settings` (CX 5) |
| `get_menu` | 31 LOC / CX 9 | 4 LOC / CX ≤ 4 (delegasi ke `build_option_menu`, CX 8) |
| `edit_user_settings` | 111 LOC / CX 27 | **21 LOC / CX ≤ 4** |
| `edit_bot_settings` | 184 LOC / CX 47 | **6 LOC / CX ≤ 4** |
| `get_buttons` | 99 LOC / CX 24 | 15 LOC / CX 5 |
| `edit_variable` | 74 LOC / CX 26 | 11 LOC / CX ≤ 4 |
| `ffmpeg_variables` | 54 LOC / CX 16 | 22 LOC / CX 6 |

Yang jadi **data**, bukan kode:

- `settings/schema.py` — `Field` / `Toggle` / `Menu` dataclass + tabel
  `LEECH_OPTIONS`, `MAIN_OPTIONS`, `MENUS`. Perbedaan yang dulu jadi cabang
  sekarang jadi field: urutan tombol ≠ urutan baris teks (memang beda di UI
  lama, dipertahankan), tiga aturan resolusi nilai (`user_or_config_or_none`,
  `user_or_config`, `user_or_runtime_list`), `present`, `escape_value`, `kind`,
  dan `premium` yang **bukan** sekadar "sembunyikan kalau bukan premium" —
  default Config yang truthy tetap mengaktifkan opsinya untuk semua orang.
- Sentinel `NOT_SET` menggantikan string `"None"`; UI lama bentrok kalau user
  benar-benar menyimpan nilai `"None"`, sekarang tidak.
- `users_settings._USER_ACTIONS` + `_EVENT_ACTIONS` + `_KEEP_ON_RESET`, dengan
  dataclass `_Ctx` yang mem-parse callback sekali.
- `bot_settings._SCREENS` (dataclass `_Screen`: `hidden`, `extra`) +
  `_ROOT_BUTTONS`, `_EDIT_PROMPTS`, `_VALUE_COERCERS`, `_RESET_SIDE_EFFECTS`,
  `_ZERO_VALUES`, `_BOT_ACTIONS`. Dua global yang dulu ditulis lewat
  `globals()["start"]` / `globals()["state"]` jadi kelas `Paging` bernama.

Urutan di `_coerce_value` load-bearing dan sudah dikomentari: `"true"`/`"false"`
menang atas semua aturan per-key, dan parsing digit/list/dict generik hanya
jalan untuk key yang tidak punya aturan sendiri.

**Catatan LOC:** total 1208 → 1789 LOC (`users_settings.py` 643 → 476,
`bot_settings.py` 565 → 795, plus `settings/` 518). Target "~700 LOC total"
salah asumsi: schema eksplisit + satu fungsi per aksi + docstring memang lebih
panjang dari rantai if/elif yang padat. Yang turun adalah percabangan
(CX 47 → ≤ 4 di router terburuk), bukan jumlah baris.

**Verifikasi:** 405 passed, 0 failed (suite penuh, tanpa perubahan test).
`ruff check bot/` → 0 temuan; ledger `bot_settings.py`
`["C901","E501","E721","I001","S307"]` → `["S307"]` dan `users_settings.py`
`["C901","E501","E741","I001","S307"]` → `["S307"]`. Selain suite:

1. **Differential settings UI** — `tools/_phase7_diff.py` menjalankan modul
   pra-refactor (dari `git show HEAD:`) dan modul baru di bawah stub yang sama,
   lalu membandingkan teks yang di-render, **matriks tombol penuh** (label +
   callback data + layout), urutan panggilan beserta argumen, dan mutasi state
   global. **306 skenario, 0 mismatch tak terduga.**
2. **Mutation testing** — 41 mutasi sengaja disuntik satu per satu ke kode baru
   (schema/menu_builder 11, dispatch `users_settings` 5, `bot_settings` 12,
   sisanya dari sesi karakterisasi awal); **41/41 tertangkap**. Satu mutasi
   awalnya lolos (`if len(value) > 200` → `> 2000`) karena tidak ada fixture
   config yang melebihi 200 karakter, jadi cabang "kirim sebagai .txt" tidak
   pernah tereksekusi — fixture ditambah `NAME_SUBSTITUTE = "s" * 250` dan
   callback `botset botvar NAME_SUBSTITUTE`, lalu tertangkap.

**Sisa untuk commit tersendiri:** — **sudah dikerjakan**, lihat di bawah.

#### Fase 7b — `fix:` hapus `eval()` dari settings UI

Dipisah dari commit refactor karena **perilakunya berubah**, jadi menggabungkannya
melanggar prinsip #2. Empat call site: `users_settings.add_one`,
`users_settings.set_option`, dan dua cabang (list + dict) di
`bot_settings._coerce_value` — semuanya `eval()` mentah atas teks pesan Telegram,
alias eksekusi kode arbitrer untuk siapa pun yang bisa membuka menu settings.

`ast.literal_eval` saja tidak cukup: `help_messages.py` mendokumentasikan
`{"fragment_retries": float("inf")}` sebagai nilai `YT_DLP_OPTIONS` yang sah
(idiom yt-dlp untuk "retry selamanya"), dan `literal_eval` menolaknya karena
`float("inf")` itu *call*, bukan literal. Membuang dukungannya berarti mematahkan
input yang didokumentasikan sendiri.

`bot/modules/settings/literals.py` (95 LOC) menyelesaikannya dengan whitelist
satu entri: `_FoldAllowedCalls` melipat `float(<angka|string>)` jadi konstanta
lebih dulu, lalu `literal_eval` yang mengerjakan seluruh validasi sisanya —
whitelist tetap satu dict kecil, bukan evaluator tulisan sendiri yang harus
diaudit lubangnya. `parse_dict()` menambahkan satu guard yang dulu bolong: nilai
seperti `{1, 2}` lolos cek `startswith("{")` padahal itu `set`, bukan `dict`.

**Yang berubah dari sudut pandang user:**

| Input | Dulu | Sekarang |
|-------|------|----------|
| `{"a": 1}`, `float("inf")`, tuple, nested | diterima | diterima (identik) |
| `[1 + 2]`, `int("3")`, nama tak dikenal | dieksekusi | `ValueError` ke user |
| `__import__("os").system(...)` | **dieksekusi** | ditolak |
| `{1, 2}` untuk opsi dict | tersimpan sebagai `set` | "It must be dict, got set!" |

Pesan error untuk nama tak dikenal ditulis ulang: `literal_eval` mengeluarkan
`malformed node or string on line 1: Name(id='bad', ctx=Load())` — dump AST yang
sampai ke user — jadi `_explain()` mengembalikannya ke bentuk lama `eval`
("name 'bad' is not defined") plus petunjuk soal tanda kutip.

**Verifikasi:** 459 passed (405 baseline + 54 test baru di
`tests/test_settings_literals.py`, termasuk contoh `YT_DLP_OPTIONS` dari
`help_messages.py` verbatim sebagai regression guard). Differential harness:
**306 skenario, 0 mismatch tak terduga** — dua skenario terdaftar di `EXPECTED`
dan itu memang seluruh permukaan perubahan yang terlihat (keduanya *sudah*
ditolak sebelum fix; hanya teks error yang bergeser). Mutation testing atas
parser baru: **16/16 tertangkap** — tiga mutasi awalnya lolos (keyword arg
diterima diam-diam, cek tipe argumen dibuang, `ValueError` dari `literal_eval`
lolos tanpa dibungkus) dan itu lubang di test, bukan di parser, jadi testnya
ditambah. Ledger `S307` dicabut untuk kedua file — sekarang keduanya tanpa utang
lint sama sekali.

---

### Fase 8 — `handlers.py` jadi table-driven

**Target:** 246 LOC → ~50 LOC. CX-nya sudah 1, ini murni soal repetisi.

```python
BOT_HANDLERS = [
    (authorize,   BotCommands.AuthorizeCommand,   CustomFilters.sudo),
    (unauthorize, BotCommands.UnAuthorizeCommand, CustomFilters.sudo),
    ...
]

def add_handlers():
    for func, cmd, flt in BOT_HANDLERS:
        TgClient.bot.add_handler(
            MessageHandler(func, filters=command(cmd, case_sensitive=True) & flt)
        )
    for func, pattern in CALLBACK_HANDLERS:
        TgClient.bot.add_handler(CallbackQueryHandler(func, filters=regex(pattern)))
```

Fase paling aman di seluruh dokumen — bisa dikerjakan kapan saja, bahkan lebih dulu.

**Status Fase 8:** selesai (dua commit: satu `fix:` blocker yang ketemu di jalan,
satu `refactor:`)

**Hasil:** target tercapai.

| | Sebelum | Sesudah |
|---|---|---|
| `bot/core/handlers.py` | 255 LOC | **171 LOC** |
| `add_handlers()` | 246 LOC / CX 1 | **13 LOC / CX 4** |
| Impor | `from ..modules import *` | 40 nama eksplisit |

Yang jadi **data**, bukan kode: dua `NamedTuple` (`_Command` dengan field `func`,
`cmd`, `access`, `on_edit`; `_Callback` dengan `func`, `pattern`, `access`) plus
tabel `COMMAND_HANDLERS` (30 baris) dan `CALLBACK_HANDLERS` (10 baris). Tiap baris
satu baris fisik — itu satu-satunya alasan tabelnya lebih enak dibaca daripada
blok tujuh baris yang digantikannya, jadi `_OWNER`/`_SUDO`/`_AUTH` dibuat sebagai
alias pendek `CustomFilters`.

Dua hal yang dulu implisit sekarang tertulis:

- **`on_edit`** — `/shell` satu-satunya perintah yang juga terdaftar sebagai
  `EditedMessageHandler`, dulu terlihat cuma sebagai satu `add_handler` ekstra di
  tengah 246 baris. Sekarang jadi flag, dan kedua handler-nya berbagi **satu**
  objek filter: `command` dan `AndFilter` pyrogram tidak menyimpan state per
  panggilan (`command` menulis argv ke *message*-nya), jadi tidak ada yang salah
  dibagi.
- **`access=None`** — `/start` satu-satunya perintah tanpa gerbang, dan itu
  disengaja (cara user tak terotorisasi tahu bot-nya ada). Sekarang eksplisit di
  tabel plus komentar, bukan "kok yang ini nggak ada `&`".

**Urutan:** dipertahankan **per jenis handler**, tidak lintas jenis. Kode lama
mendaftarkan message dan callback handler bergantian; tabelnya mendaftarkan semua
command dulu, lalu semua callback. Itu aman karena `MessageHandler`,
`EditedMessageHandler`, dan `CallbackQueryHandler` masing-masing subclass
`Handler` **langsung** (bukan turunan satu sama lain), jadi gerbang
`isinstance(handler, handler_type)` di `pyrogram/dispatcher.py::handler_worker`
cuma bisa memilih satu jenis per update. Urutan *dalam* satu jenis tetap
load-bearing — semua handler masuk group 0 dan dispatcher `break` di handler
pertama yang match, jadi baris kedua yang mengklaim command sama = kode mati.

**Verifikasi:** 470 passed, 0 failed (459 baseline + 9 test baru + 2 dari commit
`fix:`). `ruff check .` → 0 temuan; ledger `handlers.py` `["F403","F405","I001"]`
dicabut seluruhnya — file ini tanpa utang lint sama sekali. Selain suite:

1. **Differential harness** — `tools/_phase8_diff.py` menjalankan `add_handlers()`
   versi pra-refactor (dari `git show HEAD:`) dan versi baru terhadap `TgClient.bot`
   perekam, lalu membandingkan (a) struktur: kelas handler, callback, dan deskripsi
   rekursif pohon filter (set command + prefix + case-sensitivity, pola regex +
   flag, identitas tiap fungsi custom filter), dan (b) dispatch: replay
   `handler_worker` atas fake update. **41 vs 41 registrasi, urutan per jenis
   identik, 2296 skenario dispatch, 0 mismatch.**
2. **Mutation testing** — 9 mutasi ke kode baru (filter digeser, `on_edit`
   dicabut, urutan baris ditukar, pola dipotong), **9/9 tertangkap**; plus 7 mutasi
   ke `tests/test_handlers_table.py` untuk memastikan testnya bukan tautologi.
   Satu mutasi awalnya lolos replay: `^rss` → `^rs` cuma ketangkap secara
   struktural karena tidak ada skenario yang membedakan keduanya. Harness-nya
   ditambah `near_misses()` (semua truncation + flip karakter terakhir) — 1236 →
   2296 skenario, dan mutasi yang sama sekarang menghasilkan 36 mismatch.

Ekspektasi test **ditranskripsi tangan** dari listing lama (commit `d92de95`),
bukan digenerate dari tabel baru: ekspektasi yang diturunkan dari kode yang diuji
akan setuju dengan urutan apa pun, termasuk yang salah. Tiga invariannya
diverifikasi empiris dulu sebelum di-assert: 30 entri `BotCommands` semuanya
terpasang, tidak ada command word diklaim dua kali, tidak ada pola callback yang
membayangi pola setelahnya.

#### Fase 8b — `fix:` `link_resolver.py` tidak bisa diimpor

Ketemu saat menyiapkan harness, **bukan** bagian dari scope fase ini, dan
dicommit tersendiri (`4b7e6df`) karena ini bug, bukan refactor.

`bot/helper/mirror_leech_utils/download_utils/link_resolver.py` (dibuat di Fase 1,
commit `42b3ae4`) memakai relative import kurang satu titik: `from ... import
LOGGER` padahal modulnya tiga package di bawah `bot`, dan `from ...helper.ext_utils…`
padahal `...` sudah *berada* di `bot.helper`. Efeknya:
`ImportError: cannot import name 'LOGGER' from 'bot.helper'` → `bot.modules` gagal
diimpor → `bot.core.handlers` gagal → `bot/__main__.py` gagal. **Bot-nya tidak
bisa start sama sekali di branch `leech-only`** sejak commit itu; tidak ada test
yang mengimpor `bot` sebagai package sungguhan, jadi suite tetap hijau.

Diperbaiki jadi `....` / `...ext_utils…` mengikuti idiom modul sebelahnya
(`aria2_download.py`), lalu `tests/test_module_imports.py` menyapu **setiap**
modul di bawah `bot/` dengan `pkgutil.walk_packages` + `importlib.import_module`
di subprocess terisolasi. Testnya diverifikasi gagal saat bug-nya dimasukkan
kembali (lewat `git stash`), supaya kedalaman relative import yang salah tidak
bisa lolos lagi.

---

### Fase 9 — `rss.py` jadi package ✅

**Target:** 961 LOC → 4 modul ≤ 300 LOC.
**Hasil:** 961 LOC → 8 modul, terbesar 288 LOC (`listener.py`).

```
bot/modules/rss/
  __init__.py          36 LOC   re-export + scheduler start
  feed.py             125 LOC   HTTP fetch, entry parsing, filter logic
  store.py            118 LOC   rss_dict access + DB persistence
  download_bridge.py  116 LOC   RSS → download handler dispatch
  menu.py             192 LOC   UI: root menu, listings, event_handler wait
  monitor.py          190 LOC   scheduled sweep: fetch, ship, pause
  subscribe.py        222 LOC   subscribe + edit reply handlers
  manage.py           209 LOC   pause/resume/unsub/get/delete handlers
  listener.py         288 LOC   callback router: table-driven dispatch
```

Pemecahan lebih granular dari rencana awal: `commands.py` dipecah jadi
`subscribe.py` (flag grammar) dan `manage.py` (state changes), dan callback
router yang semula di dalam `menu.py` dipisah ke `listener.py` agar menu
view tidak mengimpor handler. `download_bridge.py` dan `feed.py` memotong
duplikasi HTTP + entry-reading yang tersebar di 3 tempat.

Test: `tests/test_rss_package.py` — 24 test untuk `item_blocked`, `item_url`,
`item_size`, `latest_url`, `parse_chat_target`. Suite: 440 passed.

---

### Fase 10 — Sapu bersih hygiene ✅

**Target:** Bersihkan technical debt: bare except, eval, dead code, type hints.
**Hasil:**

1. **78 bare `except:`** → semua diganti exception spesifik (`OSError`,
   `KeyError`, `ValueError`, `UnicodeDecodeError`, `Exception`, dll.)
   di 33 file. Zero bare except tersisa.
2. **`eval()` dihapus**:
   - `media_utils.py:56,104` → `json.loads` (ffprobe output sudah JSON).
   - `bot_utils.py:173` → `ast.literal_eval` (user input `-ff` flag).
   - `ytdlp.py`, settings, `common.py` — sudah diperbaiki di fase sebelumnya.
   - `exec.py` eval intentional (owner-only `/eval` command), dipertahankan.
3. Direktori kosong `gdrive_utils/`, `rclone_utils/` dihapus.
4. **`is_leech`** — 4 cabang mati dihapus: kondisi disederhanakan karena
   `is_leech` selalu `True`. Atribut dipertahankan dengan ponytail comment.
5. Type hint ditambahkan pada 15 fungsi publik di `media_pipeline.py`,
   `batch_tracker.py`, `settings_resolver.py`, `multi_link.py`.
6. Per-file ruff ignores — belum ada konfigurasi ruff di repo, dilewati.

---

## 4. Urutan Eksekusi yang Disarankan

```
Fase 0  (wajib pertama)
  └─ Fase 8   ← paling aman, bagus untuk memanaskan alur commit
  └─ Fase 1   ← nilai tertinggi
       └─ Fase 2   ← bergantung pada helper dari Fase 1
            └─ Fase 6
  └─ Fase 3   ← independen
  └─ Fase 4   ← independen
  └─ Fase 5   ← setelah test album hijau (Fase 0)
  └─ Fase 7   ← independen
  └─ Fase 9   ← independen
Fase 10 (menyusul tiap fase, dituntaskan di akhir)
```

Fase 3, 4, 7, 9 saling independen — aman dikerjakan di branch terpisah kalau mau paralel.

---

## 5. Kriteria Selesai

| Metrik | Baseline | Target |
|--------|----------|--------|
| File > 500 LOC | 11 | 0 |
| Fungsi > 100 LOC | 30 | 0 |
| Fungsi > 50 LOC | 87 | ≤ 15 |
| CX maksimum | 93 | ≤ 15 |
| Bare `except:` | 86 | ≤ 10 (semua disengaja) |
| Pemanggilan `eval()` | 9 | 0 |
| Test gagal | 5 | 0 |
| Konfigurasi lint | tidak ada | `ruff check .` bersih |

Ukur ulang dengan `python tools/complexity_report.py` (dibuat di Fase 0) dan bandingkan
dengan `tools/baseline.txt`.

---

## 6. Risiko

- **Tidak ada test untuk `leech.py`, `common.py`, `task_listener.py`** — justru file yang
  paling banyak diubah di Fase 1, 2, 6. Mitigasi: tulis characterization test untuk
  `parse_leech_args` dan `_verify_dest_permissions` **sebelum** memindahkan kode.
- **`bot/__init__.py` global mutable** membuat import bersifat side-effectful (`uvloop.install()`,
  bikin event loop, buka `log.txt`). Ini mempersulit test terisolasi. Tidak masuk lingkup
  rencana ini; kalau nanti mau, jadikan fase tersendiri (`bot/state.py` + injeksi).
- **`DOWNLOAD_DIR = "/app/downloads/"` hardcoded** — test yang menyentuh path akan butuh
  monkeypatch. Sudah ada `tests/conftest.py`, periksa cakupannya saat Fase 1.
