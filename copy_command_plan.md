# Command `/copy <task_id>` — menyalin ulang task yang sudah selesai

**Deliverable turn ini: dokumen ini disalin ke repo sebagai `copy_command_plan.md`
(pola nama mengikuti `user_session_plan.md`). Tidak ada satu baris code pun yang
diubah.**

## Context

Bot ini sudah punya copy preset (`bot/helper/storage/copy_presets.py`, commit
`5b390f6`): sekumpulan chat tujuan bernama, milik per-user, dipilih **saat task
dijalankan** dengan `-c <nama>`. Yang tidak ada: cara menyalin task yang
**sudah selesai**. Kalau user lupa menulis `-c`, atau baru belakangan ingin isi
task itu masuk channel lain, satu-satunya jalan sekarang adalah leech ulang —
download dan upload berulang untuk file yang sudah ada di Telegram.

`/copy` menutup celah itu: menyalin, bukan mengunduh ulang. Supaya bisa,
pesan-pesan hasil setiap task harus dicatat ke database saat task selesai —
sekarang tidak ada yang mencatatnya. Yang tersimpan hari ini cuma
`_msgs_dict` (`{link: nama}`), hidup selama satu task, dan hanya terisi kalau
`FILES_LINKS` menyala.

Hasil akhir yang dituju:

```
/leech <link>          → selesai, pesan hasil memuat "Task ID: 12345"
/copy 12345            → bot balas ringkasan task + keyboard preset:
                         [ anime ] [ movies ]
                         [ music ]
                         [ Cancel ]
                         → pilih satu, salinan dikirim ke chat-chat preset itu
```

Nama preset tidak diketik. Alasannya bukan kenyamanan: nama preset lolos
`[A-Za-z0-9_-]{1,24}` (`copy_presets.py:23`), jadi salah ketik satu huruf adalah
error yang baru terlihat setelah perintah dikirim. Keyboard hanya menawarkan
nama yang benar-benar ada, dan sekaligus jadi tempat konfirmasi — `/copy` mengirim
file ke channel, dan itu tidak bisa dibatalkan setelah terkirim.

## Keputusan yang sudah diambil

| Hal | Pilihan |
|---|---|
| Bentuk perintah | `/copy <task_id>`, preset dipilih dari inline keyboard (+ tombol Cancel) |
| Referensi task | task id dicetak di pesan hasil task |
| Yang disimpan | **keduanya** — koordinat pesan *dan* `file_id`; `copy_message` dulu, `file_id` cadangan |
| Retensi | N record terakhir per user (konstanta `MAX_TASK_RECORDS`, default 200) |
| Akses | semua user authorized (`CustomFilters.authorized`) |

Catatan jujur soal cadangan `file_id`: `file_reference` di dalam `file_id`
kedaluwarsa (jam–hari) → `FILE_REFERENCE_EXPIRED`, dan `file_id` hasil upload
lewat user session belum tentu bisa dipakai akun bot. Karena itu jalur utamanya
`copy_message`/`copy_media_group`, dan `file_id` hanya dicoba ketika pesan
asalnya sudah hilang. Kegagalan cadangan dilaporkan per tujuan, tidak diam.

---

## Tiga commit

AGENTS.md §6.3: ekstraksi dan perubahan perilaku tidak digabung.

### Commit 1 — `refactor: lift copy-target verification and fan-out out of their callers`

Tidak ada perubahan perilaku. Tiga hal yang `/copy` butuhkan sekarang terkunci
di dalam class yang hanya bisa dibangun oleh sebuah task.

1. **`bot/helper/telegram/dest_chat.py`** (sudah memuat `get_dest_chat`,
   `get_dest_member`, `can_reach_dest`, `ChatLookupError`) — pindahkan ke sini:
   - `is_group_chat(chat)` dan `GROUP_CHAT_TYPES` (dari `settings_resolver.py:51-58, 27`)
   - `can_manage_and_delete(member)` (dari `settings_resolver.py:66-68`)
   - `verify_copy_target(entry, chat_id)` + `verify_copy_target_reachable(...)`
     (dari `settings_resolver.py:471-527`) — keduanya sudah tidak menyentuh
     state listener apa pun selain `TgClient.bot`, jadi pemindahannya lurus.

2. **`bot/helper/storage/copy_presets.py`** — tambah
   `as_dump_target(entry, user_id)` dan `as_chat_id(value)`, isinya dari
   `settings_resolver._as_dump_target` (`:425-434`) dan `_as_chat_id` (`:61-63`).
   `user_id` jadi parameter karena `pm` berarti chat pemanggil, dan `/copy`
   pemanggilnya bukan pemilik task. Rumahnya di sini karena `_shape_error`
   (`copy_presets.py:88-109`) sudah mendokumentasikan tiga bentuk yang sama.

3. **`bot/helper/storage/copy_records.py`** (baru) — pindahkan loop kipas
   `_copy_to_clone_dumps` (`telegram_uploader.py:332-361`) menjadi
   `fan_out(pacer, targets, copy, from_chat_id, message_id)`, apa adanya:
   `message_thread_id` untuk topik, `reply_to_message_id` dari
   `last_sent_msg`, satu tujuan gagal tidak menghentikan yang lain.

4. **`settings_resolver.py`** dan **`telegram_uploader.py`** jadi delegasi tipis
   ke empat hal di atas.

Test: `tests/test_copy_presets.py` me-monkeypatch `sr.get_dest_chat` /
`sr.get_dest_member` / `sr.can_reach_dest` — target patch berpindah ke modul
`dest_chat`. `tests/test_telegram_uploader_album.py` juga menyentuh jalur copy;
sesuaikan. Tambah test untuk `as_dump_target` (termasuk `pm` → `user_id`).

### Commit 2 — `feat: record every uploaded message of a task`

Ini yang membuat `/copy` mungkin. Tanpa commit 3 pun berdiri sendiri: pesan
hasil task mulai mencantumkan task id, dan record tersimpan.

**Bentuk satu record** — di `copy_records.py`. Satu task menyimpan daftar
*unit* terurut, satu unit = satu perintah copy:

```python
{
  "mode": "single" | "group",
  "chat": int,                       # chat asal
  "msg":  int,                       # message id yang disalin (anggota mana pun untuk group)
  "media": [                         # cadangan file_id, satu entri per file di unit
      {"kind": "document"|"video"|"audio"|"photo", "file_id": str, "caption": str},
  ],
}
```

`single` → `copy_message`, cadangan `send_document`/`send_video`/... ·
`group` → `copy_media_group`, cadangan `send_media_group` dengan
`InputMediaVideo`/`InputMediaDocument`/`InputMediaPhoto` — persis cara
`media_group_batcher.py:128-137, 212-224` sudah membangunnya dari `file_id`.

**Titik pencatatan di `telegram_uploader.py`** — dua, keduanya sudah melakukan
pembukuan berbentuk sama untuk `_uncopied`:

- `_send_one` (`:648-653`): setelah kirim berhasil, catat satu unit `single`
  dari `self.anchor`. **Tanpa gate `copy_preset`** dan tanpa gate `FILES_LINKS`
  — itulah bedanya dengan `_uncopied`. Gate satu-satunya:
  `Config.DATABASE_URL` kosong → tidak usah menumpuk apa pun di memori.
- `retire_group` (`:313-318`): album menghapus pesan-pesan yang diserapnya, jadi
  coret unit `single` yang ber-`(chat, msg)` di `carried`, lalu catat satu unit
  `group` dari `sent[-1]` dengan `media` seluruh anggota album. Ini satu-satunya
  tempat di codebase yang sudah membatalkan catatan per-file, jadi polanya ada.

**Batas per task, bukan batas storage.** Sewaktu `units` masih satu dokumen
`jsonb` (Mongo 16 MB, lalu tabel `copy_records`), daftar tersarang itu
menembus batas ukuran dokumen. Sekarang tiap unit = satu baris `copy_units` +
satu baris `copy_unit_media` per file, jadi tak ada batas ukuran dokumen lagi.
`MAX_RECORD_UNITS = 1000` dipertahankan sebagai batas **satu task** — penjaga
agar satu task tidak membanjiri tabel — dan baris yang dibuang tetap di-log,
karena truncation yang diam akan terbaca sebagai "semuanya tercatat".

**Serah-terima ke listener.** Uploader menaruh daftar unit di
`self._listener.copy_units`; deklarasinya di `bot/helper/common.py` (dekat
`:44-45`) dan `bot/helper/task/_host.py` (dekat `:90`), sesuai §3
AGENTS.md soal kontrak atribut. Penulisan ke DB terjadi di
`task_listener.on_upload_complete` (`:439`), tempat `rm_complete_task` sudah
menulis DB dan tempat pesan hasil dibangun.

**`bot/helper/storage/db_handler.py`** — tabel `copy_tasks`/`copy_units`/
`copy_unit_media` (satu task = baris parent + anak, `bot_id` per-bot seperti
`rss`/`incomplete_tasks`; pengganti kolom `units` jsonb di `copy_records`, yang
di-backfill lalu di-drop `tools/migrate_copy_records_to_rows.py`). Tiga method,
semuanya dibuka `if self._return: return` seperti seluruh file:

```python
async def save_copy_record(self, cid, mid, user_id, name, units) -> None
async def find_copy_records(self, mid) -> list[dict]
async def _prune_copy_records(self, user_id) -> None   # sisakan MAX_TASK_RECORDS terbaru
```

Dokumen yang dibaca pemanggil tetap berbentuk lama — `_id = f"{cid}:{mid}"`
(diturunkan Python saat join, bukan kolom), plus `mid`, `cid`, `user`, `name`,
`at` (epoch int), `units`. `_id` gabungan karena `listener.mid` adalah
`self.message.id` (`common.py:28`) — unik per chat, **tidak** lintas chat.
Prune: `SELECT cid, mid ... ORDER BY at DESC, mid DESC OFFSET
MAX_TASK_RECORDS`, lalu satu `DELETE FROM copy_tasks` per baris basi (FK
cascade membersihkan unit/media anaknya). Repo ini belum pernah membuat index
(nol `create_index`); dengan N=200 per user tidak perlu, dan itu dicatat di
docstring supaya keputusannya terlihat.

**Task id di pesan hasil** — dua jalur:

- Non-batch, `on_upload_complete` (`:465-469`): tambah
  `\n<b>Task ID: </b><code>{self.mid}</code>` di header, sebelum baris `cc:`.
- Batch, `batch_tracker._publish_summary` (`:145-167`): tiap anak punya mid
  sendiri; bawa `"mid"` di payload `record_batch_result`
  (`task_listener.py:453-463`) dan cetak satu baris `Task IDs:` berisi mid tiap
  anak. Tanpa ini, task dalam bulk tidak akan pernah bisa di-`/copy`.

Test baru `tests/test_copy_records.py`: satu file → satu unit `single`; album →
satu unit `group` dan `single` yang diserapnya tercoret; split part jadi unit
sendiri; pencatatan tetap jalan dengan `FILES_LINKS` mati dan tanpa
`copy_preset`; pemotongan di batas unit ter-log; prune menyisakan N terbaru.
Ikut diperluas: `tests/test_telegram_uploader_album.py` (jalur `retire_group`).

### Commit 3 — `feat: add /copy to resend a finished task to a chosen preset`

Dua entry point: perintahnya yang membuka pilihan, dan callback yang
mengeksekusi.

#### `bot/modules/copy.py` (baru) — tahap 1, perintah

`@new_task async def copy_task(_, message)`, mengikuti idiom
`bot/modules/shell.py`:

1. `message.text.split()` → butuh tepat satu argumen. Kurang/lebih → balas cara
   pakai. Bukan lewat `arg_parser`: `arg_parser` melipat token sebelum flag
   pertama ke `"link"` (`bot_utils.py:181-184`), jadi argumen posisional tidak
   cocok.
2. `DATABASE_URL` kosong → katakan fitur ini butuh database, berhenti.
3. Cari record: `database.find_copy_records(mid)`. Terima `<mid>` maupun bentuk
   eksplisit `<cid>:<mid>`. 0 hasil → tidak ada record (jelaskan: task lebih tua
   dari fitur ini, atau sudah lewat batas retensi). >1 hasil → utamakan yang
   `cid`-nya sama dengan chat tempat `/copy` dijalankan; kalau tetap ambigu,
   tolak dan tampilkan bentuk `cid:mid` yang bisa dipilih.
4. Ambil preset pemanggil: `presets_of(user_data.get(user_id, {}))` — preset
   **yang menekan tombol**, bukan pemilik task. Kosong → arahkan ke
   `/usetting` → Leech → Copy Presets, tanpa keyboard.
5. Balas ringkasan + keyboard: nama task, jumlah unit, task id, lalu satu tombol
   per preset dan satu tombol `Cancel`. `ButtonMaker`
   (`bot/helper/telegram/button_build.py`): `data_button(name, ...)` per
   preset, `data_button("Cancel", ..., "footer")`, `build_menu(2)`.

#### Callback data dan state yang menyertainya

Anggaran callback data 64 byte adalah alasan `copy_presets.NAME_PATTERN` sesempit
itu (`copy_presets.py:8-26`), dan di sini anggarannya lebih sempit lagi: `cid`
bisa 14 karakter (`-1001234567890`), ditambah `mid`, ditambah nama preset 24
karakter, ditambah user id — `copyt <uid> <cid>:<mid> <name>` bisa menyentuh
batas. Jadi **task id tidak dititipkan ke callback data.**

Bentuknya: `copyt <user_id> <index>` untuk memilih, `copyt <user_id> x` untuk
batal — di bawah 20 byte, dan pola `<prefix> <user_id> <verb>` sama dengan
`userset` (`users_settings.py:402`).

State perantaranya satu dict di memori modul, seperti `handler_dict`
(`users_settings.py:46`):

```python
pending = {}   # (chat_id, prompt_msg_id) -> {"user": int, "record": dict,
               #                              "presets": [(name, entries)], "at": float}
```

Index menunjuk ke `presets` yang **benar-benar ditampilkan**, bukan ke nama yang
dibaca ulang saat tombol ditekan — kalau user mengedit presetnya di antara dua
momen itu, tombol tetap berarti apa yang tertulis di atasnya.

Entri kedaluwarsa setelah `PENDING_TTL = 300` detik dan dihapus begitu dipakai;
prompt yang basi dijawab "prompt ini sudah kedaluwarsa, jalankan `/copy` lagi".
Dict ini hilang saat bot restart — konsekuensinya sama, dan disebut di help.

#### `bot/modules/copy.py` — tahap 2, callback

`@new_task async def copy_choice(client, query)`:

1. `query.data.split()` → `["copyt", user_id, index_or_x]`.
   `query.from_user.id != int(data[1])` → `query.answer("Not yours!",
   show_alert=True)`. Pemeriksaan ini di callback, bukan di filter registrasi,
   sama seperti `edit_user_settings` (`users_settings.py:618`).
2. `x` → edit prompt jadi "Copy cancelled.", hapus entri pending,
   `auto_delete_message`.
3. Ambil entri pending dari `(chat_id, message.id)`. Tidak ada / kedaluwarsa →
   pesan kedaluwarsa. Ada `/copy` lain dari user yang sama sedang jalan → tolak,
   supaya perintah yang ter-kirim dua kali tidak menggandakan salinan.
4. Bangun tujuan: `as_dump_target(entry, user_id)` per entri →
   `{(chat_id, thread_id): {"last_sent_msg": None}}`, lalu `verify_copy_target`
   untuk **setiap** tujuan sebelum satu pun salinan dikirim, dengan pesan error
   yang sama seperti jalur `-c` (`settings_resolver.py:471-527`). Di sini
   verifikasi lebih murah daripada di jalur `-c`: tidak ada download yang sudah
   terbuang, tapi tetap di depan — memberitahu satu channel tak tertulis setelah
   separuh file terkirim adalah keadaan yang tidak bisa dirapikan.
5. Salin: untuk tiap unit berurutan, `copy_unit(pacer, targets, unit)` di
   `copy_records.py` — `fan_out` dengan `TgClient.bot.copy_media_group` atau
   `copy_message`; kalau pesan asalnya sudah hilang, jatuh ke `send_*` /
   `send_media_group` dari `file_id`. `pacer = FloodPacer(lambda: False)`
   (`upload_utils/flood_pacer.py:37`) supaya flood wait ditunggu, bukan
   menggagalkan.
6. Lapor: prompt yang sama di-edit jadi progres selama jalan (throttled seperti
   `batch_tracker._progress_text`), lalu ringkasan per-tujuan — berapa unit
   berhasil, berapa gagal, dan sebabnya. Satu tujuan bermasalah bukan masalah
   tujuan lain, invarian yang sama dengan `_copy_to_clone_dumps`.

#### Wiring (lima sentuhan, semuanya berpola tabel)

| File | Perubahan |
|---|---|
| `bot/helper/telegram/bot_commands.py` | `CopyCommand = f"copy{i}"` |
| `bot/modules/__init__.py` | import `copy_task`, `copy_choice` + `__all__` |
| `bot/core/handlers.py` | import, `_Command(copy_task, BotCommands.CopyCommand, _AUTH, desc="Copy a finished task to a preset")` di `COMMAND_HANDLERS`, dan `_Callback(copy_choice, "^copyt")` di `CALLBACK_HANDLERS` |
| `bot/helper/util/help_messages.py` | satu baris di `help_string` (`:363-393`); sebut `/copy` di teks `copy_preset` (`:176-183`) |

`desc=` itu yang mengisi menu command Telegram lewat `set_commands()`
(`handlers.py:173-181`) — tidak ada langkah terpisah. `^copyt` tidak
bertabrakan dengan sepuluh pola yang ada (`botset`, `canall`, `stopm`, `sel`,
`help`, `rss`, `botrestart`, `status`, `torser`, `userset`), dan
`test_no_callback_pattern_shadows_a_later_one` yang menjaganya.

`tests/test_handlers_table.py` — `EXPECTED_COMMANDS` (`:44-75`) dan
`EXPECTED_CALLBACKS` disalin tangan dari listing lama, jadi baris `/copy` dan
`^copyt` harus ditambahkan manual; invarian
`test_no_command_word_is_claimed_twice` dan
`test_every_declared_bot_command_is_wired` ikut menjaganya.

#### Test baru `tests/test_copy_command.py`

Tahap 1: argumen kurang/lebih; tanpa `DATABASE_URL`; task id tidak ada; task id
ambigu lintas chat; bentuk `cid:mid` eksplisit; user tanpa preset tidak dapat
keyboard; keyboard memuat satu tombol per preset plus Cancel; callback data
setiap tombol di bawah 64 byte.

Tahap 2: tombol ditekan orang lain → ditolak; `Cancel` tidak mengirim apa pun;
entri pending kedaluwarsa; index menunjuk preset yang ditampilkan meski preset
user berubah setelah prompt; tujuan tidak lolos verifikasi → **nol** salinan
terkirim; jalur `copy_message`; jalur cadangan `file_id` saat pesan asal hilang;
satu tujuan gagal, sisanya tetap jalan; urutan unit terjaga; `/copy` kedua dari
user yang sama saat yang pertama jalan → ditolak.

---

## Yang perlu diperhatikan

- **`-c` tidak berubah.** `/copy` memakai penyimpanan preset yang sama tapi
  tidak menyentuh `_resolve_copy_preset`. Sesudah commit 1 keduanya berbagi
  verifikasi tujuan dan loop kipas yang sama.
- **`_uncopied` tetap ada dan terpisah.** Ia hanya hidup untuk task ber-`-c` dan
  hanya sampai task selesai; `copy_units` dicatat untuk setiap task. Menyatukan
  keduanya akan mengubah perilaku `-c` — di luar cakupan.
- **Album dan `MEDIA_GROUP`.** Jalur `-c` memaksa `MEDIA_GROUP` menyala
  (`telegram_uploader.py:145-149`). `/copy` tidak bisa: pengelompokan sudah
  terjadi. Jadi task yang di-upload tanpa album akan disalin satu-satu, dan itu
  benar — yang tercatat adalah bentuk sebenarnya di Telegram.
- **Bot harus bisa membaca chat asal.** Salinan dikirim sebagai `TgClient.bot`
  (`telegram_uploader.py:325, 375`). Kalau upload aslinya lewat user session ke
  chat yang bot-nya tidak ada, `copy_message` gagal dan cadangan `file_id` yang
  menentukan — kemungkinan besar juga gagal. Kasus ini dilaporkan apa adanya.
- **`/copy` yang sedang jalan tidak bisa dibatalkan.** Tombol Cancel hanya
  berlaku sebelum eksekusi mulai; integrasi dengan `/cancel` di luar cakupan.
  Dibatasi satu per user sebagai gantinya.

---

## Verifikasi (untuk saat implementasi dijalankan)

**Tiga gerbang** (AGENTS.md §2) — rekam baseline ruff **sebelum** menyentuh apa
pun, karena 41 temuan yang ada bukan izin:

```bash
ruff check . --output-format concise | sort > /tmp/before.txt
# ... kerjakan ...
.venv/bin/python -m pytest -q
ruff check . --output-format concise | sort > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt          # kosong, atau hanya baris yang hilang
.venv/bin/pyrefly check --baseline pyrefly-baseline.json
ruff check <file-yang-diubah> --select ANN   # anotasi lengkap, §3
```

Tiap commit membawa test yang **sudah dilihat gagal** tanpa perubahannya;
pembuktian itu ditulis di body commit (§4, §7).

**Test terarah selama kerja:**

```bash
.venv/bin/python -m pytest tests/test_copy_records.py tests/test_copy_command.py \
    tests/test_copy_presets.py tests/test_handlers_table.py \
    tests/test_telegram_uploader_album.py -q
```

**Smoke test di bot yang jalan** — yang tidak bisa dibuktikan unit test:

1. `/usetting` → Leech → Copy Presets → buat preset `smoke` berisi satu chat
   yang bot-nya admin.
2. `/leech <link kecil>` **tanpa** `-c`. Pesan hasil harus memuat
   `Task ID: <n>`.
3. `/copy <n>` → muncul ringkasan + tombol `smoke` dan `Cancel`.
4. Tekan `Cancel` → tidak ada file terkirim ke mana pun.
5. `/copy <n>` lagi → tekan `smoke` → file muncul di chat preset, berantai
   sebagai reply, dan ringkasan melaporkan jumlah yang berhasil.
6. `/copy <n>` dari akun authorized lain, lalu akun pertama menekan tombolnya →
   ditolak dengan alert.
7. Preset memuat satu chat yang bot-nya bukan admin → ditolak sebelum satu pun
   file terkirim, dengan nama chat yang gagal.
8. `/copy 999999` dan `/copy` tanpa argumen → dua pesan error yang berbeda dan
   jelas.
9. Diamkan prompt lebih dari `PENDING_TTL`, lalu tekan tombolnya → pesan
   kedaluwarsa, bukan traceback.
10. Cek DB (psql): `copy_tasks` memuat satu baris per task, `copy_units`/
    `copy_unit_media` baris anak dengan `seq`/`idx` terurut, dan setelah lebih
    dari `MAX_TASK_RECORDS` task, jumlah baris `copy_tasks` per user berhenti
    bertambah.
11. Uji album: leech folder multi-file dengan `MEDIA_GROUP` menyala, lalu
    `/copy` — harus tiba sebagai album, bukan file terpisah.
