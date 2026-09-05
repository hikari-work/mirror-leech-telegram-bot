# Plan Migrasi Database: MongoDB → PostgreSQL

**Deliverable turn ini: dokumen ini disalin ke repo sebagai
`postgres_migration_plan.md`. Tidak ada satu baris code pun yang diubah.**

## 1. Context — apa yang dicek, apa adanya

Database bot saat ini **MongoDB**, diakses **langsung tanpa ORM** lewat
`pymongo` async di satu modul: `bot/helper/ext_utils/db_handler.py`
(class `DbManager`, singleton `database`), plus `gridfs.asynchronous` untuk
penyimpanan blob. Tidak ada lapisan abstraksi penyimpanan — `DbManager`
memakai API koleksi pymongo apa adanya (`find`, `update_one`, `replace_one`,
`drop`), dan `bot/core/startup.py` bahkan menembus `database.db.<collection>`
secara mentah di 20+ titik.

Target migrasi: **PostgreSQL**, dengan strategi yang sudah dipilih:

| Hal | Pilihan |
|---|---|
| Strategi cutover | **Sekali putus** — tool migrasi data dijalankan sekali saat maintenance window; kode Mongo dibuang setelahnya |
| Pengujian | **Hermetik** (default `pytest`, tanpa server) **+ integrasi** terhadap Postgres nyata, jalan di CI sebagai service container, lokal hanya kalau `PG_TEST_URL` di-set |
| Driver | `psycopg` v3 async (`psycopg[binary]`) — bukan ORM, bukan SQLAlchemy, bukan asyncpg |
| Bentuk penyimpanan | Tabel relasional + kolom `jsonb` untuk dokumen yang memang berupa dict bersarang |
| Skema | DDL idempotent (`CREATE TABLE IF NOT EXISTS`) yang diterapkan saat `connect()` — analog dengan koleksi Mongo yang "selalu ada" |
| API | `DbManager` dipertahankan: nama method & signature tidak berubah, jadi pemanggil tidak berubah |

Alasan tiap pilihan ada di §3–§4. Yang paling menentukan bentuk schema: cara
kerja bot terhadap DB adalah **muat-seluruh-koleksi-ke-memori saat boot, lalu
replace satu baris/dok utuh per perubahan** (`user_data`, `rss_dict`,
`settings.config`, dsb. semuanya dict hidup di memori dan ditulis ulang
utuh). Pola "tulis utuh" itu memetakan 1:1 ke baris `jsonb`, bukan ke
normalisasi per-field.

### 1.1 Inventaris data Mongo hari ini

Semua koleksi di bawah satu database `DATABASE_NAME` (default `mltb`).

| Koleksi | Kunci | Bentuk dokumen | Pemakaian |
|---|---|---|---|
| `settings.config` | `_id` = bot id (string, = `TgClient.ID`) | dict skalar campur JSON: semua atribut `Config` (`YT_DLP_OPTIONS`, `TG_PROXY`, `FFMPEG_CMDS`, `SEARCH_PLUGINS` adalah dict/list) | boot `load_settings`; ubah di menu bot settings (`update_config` = `$set` merge satu/beberapa key) |
| `settings.deployConfig` | `_id` = bot id | snapshot `vars(config)` dari file `config.py`, minus `DB_ENCRYPTION_KEY` | `update_deploy_config()` saat `config.py` disimpan; dibersihkan `migrate_legacy_keys` |
| `settings.aria2c` | `_id` = bot id | opsi aria2 (dict string→string) | boot; `update_aria2` |
| `settings.qbittorrent` | `_id` = bot id | opsi qbittorrent | boot; `update_qbittorrent` / `save_qbit_settings` |
| `users` | `_id` = user id (**global, tidak per-bot**) | pengaturan user (skalar; file dipisah ke GridFS) | `update_user_data` = `replace_one` utuh; boot `restore_users` |
| `rss.<bot_id>` (koleksi dinamis per bot) | `_id` = user id | `{title: {link, last_feed, last_title, inf, exf, paused, command, sensitive, tag}}` — bersarang dua tingkat | seluruh lifecycle RSS; **tulis = replace utuh per user** |
| `tasks.<bot_id>` | `_id` = link | `{cid, tag}` | incomplete-task notifier; `get_incomplete_tasks` membaca **lalu `drop()` seluruh koleksi** |
| `copies.<bot_id>` | `_id` = `"{cid}:{mid}"` | `{cid, mid, user, name, at, units}`; `units` = list dict bersarang (`mode`/`chat`/`msg`/`media[]`), dipakai `/copy` | `save_copy_record` (upsert), `find_copy_records` (`find({"mid": ...})`), prune 200/user |
| GridFS bucket `files` | filename = `"{bot_id or TgClient.ID}/{path}"` | bytes **terenkripsi** (`blob_crypto.blob_box`), dipecah 255 KB per chunk oleh GridFS | file privat (`cookies.txt`, `.netrc`, `config.py`, dsb.) & thumbnail `users/<uid>/THUMBNAIL`; tiap `save_blob` = upload revisi baru lalu `_prune_blob` sisakan 1 |

Catatan penting yang ikut terbawa ke desain baru:

- **No-DB mode.** `DATABASE_URL=""` → `database._return = True`, semua method
  `return` dini; bot tetap hidup dengan memori + `user_sessions.json`. Perilaku
  ini **wajib dipertahankan** — `/copy` bahkan menolak tanpa database
  (`copy.py:76`).
- **Satu DB menghuni beberapa bot.** `_blob_name` men-namespace blob per bot id;
  koleksi `rss`/`tasks`/`copies` dinamis per bot; dokumen `settings.*` ber-`_id`
  bot. Yang **global** cuma koleksi `users`. Di PG ini menjadi kolom `bot_id`
  di tiap tabel yang relevan — nilai persis sama (`TgClient.ID`).
- **`_prune_blob` keep=1** menjadikan tiap nama blob berisi **tepat satu revisi
  terbaru** — setara dengan *upsert satu baris per nama*.
- **Tanpa index di sisi Mongo.** `_prune_copy_records` sengaja tidak pakai index
  (`db_handler.py` — scan koleksi ratusan dok per user lebih murah daripada
  index yang hanya dibaca prune itu). Skala data ratusan–ribuan dok, bukan
  jutaan. Desain PG mengikuti: **tidak menambah index** kecuali yang jadi PK.
- **Blob disimpan sudah terenkripsi.** Tool migrasi tidak perlu tahu kunci —
  menyalin ciphertext mentah sudah cukup.

### 1.2 Kenapa "cek database" penting sebelum menulis schema

Bagian tersulit migrasi bukan SQL-nya — SQL-nya kecil (§5). Yang berisiko
adalah **menyamakan perilaku halus** yang sekarang gratis di Mongo: dokumen
`settings.config` adalah dict campuran tipe yang di-update dengan `$set`-merge
(`startup.py:174`, `bot_settings.py`); `units` di `copy_records` adalah JSON
bersarang yang dibaca utuh oleh `/copy` dan **di-serialize ulang ke dict
Python** (`copy.py:109`); `get_incomplete_tasks` menghapus seluruh koleksi
setelah dibaca; mode no-DB adalah *state* runtime (`_return`), bukan
ketiadaan tabel. Semuanya harus direproduksi persis, bukan didekati.

---

## 2. Skema PostgreSQL

Satu file `bot/helper/ext_utils/pg_schema.sql` (atau konstanta multi-line di
modul adapter) berisi DDL idempotent, diterapkan saat koneksi pertama dibuka
(`CREATE TABLE IF NOT EXISTS`). Tidak ada alembic — skema sekecil ini dan satu
bot per deploy tidak butuh versioning migrasi skema; perubahan skema ke depan
cukup `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` di file yang sama.

```sql
CREATE TABLE IF NOT EXISTS settings_config (
    bot_id text PRIMARY KEY,
    data   jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_deploy (
    bot_id text PRIMARY KEY,
    data   jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_aria2 (
    bot_id text PRIMARY KEY,
    data   jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_qbit (
    bot_id text PRIMARY KEY,
    data   jsonb NOT NULL
);

-- Global, tidak per-bot — persis seperti koleksi `users` hari ini.
CREATE TABLE IF NOT EXISTS users (
    user_id bigint PRIMARY KEY,
    data    jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS rss (
    bot_id  text NOT NULL,
    user_id bigint NOT NULL,
    data    jsonb NOT NULL,          -- {title: {link, last_feed, ...}}
    PRIMARY KEY (bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS incomplete_tasks (
    bot_id text   NOT NULL,
    link   text   NOT NULL,
    cid    bigint NOT NULL,
    tag    text   NOT NULL,
    PRIMARY KEY (bot_id, link)
);

CREATE TABLE IF NOT EXISTS copy_records (
    bot_id  text   NOT NULL,
    id      text   NOT NULL,          -- "{cid}:{mid}", bekas _id dokumen
    cid     bigint NOT NULL,
    mid     bigint NOT NULL,
    user_id bigint NOT NULL,
    name    text   NOT NULL,
    at      bigint NOT NULL,          -- int(time()), diurutkan untuk prune
    units   jsonb  NOT NULL,
    PRIMARY KEY (bot_id, id)
);

CREATE TABLE IF NOT EXISTS blobs (
    name       text      PRIMARY KEY,
    data       bytea     NOT NULL,    -- ciphertext dari blob_box.encrypt
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

**Kenapa satu dokumen dict → satu baris `jsonb`, bukan normalisasi:**

1. Seluruh pola akses bot adalah *baca semua → mutasi dict di memori → tulis
   utuh*. Normalisasi per-field = menulis ulang N baris per perubahan padahal
   yang disimpan cuma dict yang sama.
2. Isi dokumen **tidak punya skema tetap**: `settings.config` menerima atribut
   `Config` baru yang belum dikenal saat tabel dibuat; `rss` memuat feed dengan
   key sewenang-wenang (judul feed = key); `units` adalah JSON bersarang yang
   kedalaman & bentuknya ditentukan kode pemanggil. `jsonb` menampung itu semua
   tanpa skema ketat, persis seperti dokumen Mongo.
3. `jsonb` di psycopg ter-adaptasi otomatis ke/dari `dict` Python — kode tidak
   perlu `json.dumps`/`loads` manual di tiap method. Ini alasan memilih psycopg
   ketimbang asyncpg (asyncpg mengembalikan `jsonb` sebagai `str` kecuali ada
   `set_type_codec` per koneksi).

**Pemetaan cara tulis utuh ke SQL** — inti adapter (detail §3):

- *replace_one({_id}, doc, upsert)* → `INSERT ... ON CONFLICT (pk) DO UPDATE
  SET data = EXCLUDED.data` (users, rss, settings_*).
- *update_one({_id}, {"$set": sebagian})* → merge di sisi PG, bukan baca-lalu-
  tulis: `INSERT ... ON CONFLICT (pk) DO UPDATE SET data = data || $2::jsonb`
  (untuk settings.config/aria2/qbit yang memang `$set`-merge).
- *find_one / find semua* → `SELECT data ... WHERE bot_id = $1` lalu
  `json.loads` otomatis oleh psycopg.
- *list_blobs prefix* → `SELECT name FROM blobs WHERE substr(name, 1,
  length($1)) = $1` — uji prefix **tanpa wildcard**, jadi tak ada masalah
  escape `%`/`_`/`.` (alasan comment asli memilih range scan ketimbang regex di
  Mongo). Ini sengaja seq-scan, konsisten dengan keputusan "tanpa index".
- *save_blob* → `INSERT INTO blobs(name, data) VALUES ($1,$2) ON CONFLICT
  (name) DO UPDATE SET data = EXCLUDED.data, updated_at = now()`. Satu revisi
  per nama = perilaku efektif `_prune_blob(keep=1)`.
- *delete_blob* → `DELETE FROM blobs WHERE name = $1`.
- *get_incomplete_tasks* → `SELECT ... WHERE bot_id = $1` **lalu** `DELETE FROM
  incomplete_tasks WHERE bot_id = $1` — urutan baca-lalu-hapus dipertahankan.
- *trunc_table("rss"/"tasks")* → `DELETE FROM <tabel> WHERE bot_id = $1`.
- *find_copy_records(mid)* → `SELECT ... WHERE bot_id = $1 AND mid = $2`
  (seq-scan, tanpa index — mirror Mongo).
- *_prune_copy_records(user)* → pilih `id` lama: `SELECT id FROM copy_records
  WHERE bot_id = $1 AND user_id = $2 ORDER BY at DESC, id LIMIT ... OFFSET
  MAX_TASK_RECORDS` lalu `DELETE ... WHERE bot_id=$1 AND id = ANY($2)`. Tambah
  pengurut kedua `id` supaya urutan deterministik saat `at` kembar.

---

## 3. Bentuk adapter — psycopg v3 async

Tidak ada ORM, sesuai budaya repo yang baru saja *membuang* empat dependency
demi dua helper lokal (`e6082d0`) dan lebih suka kode eksplisit. psycopg v3
dipakai raw:

- Dependency baru: `psycopg[binary]` di `requirements.txt` (satu baris;
  wheel biner `psycopg-binary` mengikuti). Versi di-*pin* oleh pengunci yang
  sama dengan dependency lain.
- Koneksi: satu `psycopg.AsyncConnection` yang dipegang `DbManager`, dibuka di
  `connect()` dan ditutup di `disconnect()` — analog satu `AsyncMongoClient`.
  Pemakaian bot single-loop dan jarang (boot + tiap perubahan settings), jadi
  **tidak perlu connection pool**. `autocommit = True` untuk pola
  *one-shot statement* yang dominan (setiap method = satu `execute`, Mongo juga
  per-command); transaksi eksplisit hanya jika satu method butuh beberapa
  statement atomik — dan hari ini tidak ada yang butuh.
- Parameter pakai placeholder `%s` psycopg (portabel), bukan `$1` asyncpg.
- `jsonb` → `dict`/`list` Python otomatis dua arah.

`DbManager` tetap satu-satunya pemegang koneksi; `startup.py` **berhenti
menembus `database.db.*`** — semua akses mentah itu difold ke method
`DbManager` baru (§4).

### 3.1 Mengganti pymongo, mempertahankan kontrak

Lapisan pymongo yang dipakai `DbManager` hari ini (koleksi, `find_one`,
`update_one`, `replace_one`, cursor `.sort/.skip/to_list`) **bukan bagian dari
kontrak** — kontraknya adalah perilaku observable method `DbManager` + nilai
kembali `find_copy_records`/`get_incomplete_tasks` (list dict) yang dibaca
pemanggil. Adapter PG menggantikan driver, API tetap:

| Method `DbManager` (dipakai di luar modul) | Jadi di PG |
|---|---|
| `connect` / `disconnect` | buka/tutup `AsyncConnection` + apply DDL idempotent; `_return` guard dipertahankan apa adanya |
| `save_blob` / `get_blob` / `list_blobs` / `delete_blob` | tabel `blobs` (§2); `_blob_name`, enkripsi `blob_box`, strip prefix bot **tidak berubah** |
| `update_private_file` | sama (save/delete blob + `update_deploy_config` kalau `config.py`) |
| `update_deploy_config` | upsert `settings_deploy` |
| `update_config(dict_)` | `settings_config` `||` merge |
| `update_aria2` / `update_qbittorrent` / `save_qbit_settings` | upsert/`||`-merge `settings_aria2` / `settings_qbit` |
| `update_user_data` / `update_user_doc` | replace utuh `users` (+ blob thumbnail) |
| `rss_update` / `rss_update_all` / `rss_delete` | replace utuh / delete baris `rss(bot_id, user_id)` |
| `add_incomplete_task` / `rm_complete_task` / `get_incomplete_tasks` | insert / delete / select-lalu-delete `incomplete_tasks` |
| `save_copy_record` / `find_copy_records` / `_prune_copy_records` | upsert / select-by-mid / prune `copy_records` |
| `trunc_table(name)` | `DELETE FROM <tabel> WHERE bot_id = $1` |

`user_sessions.py` membaca `database._return` dan `startup.py` mengecek
`database.db is None` sebagai probe "terkoneksi?" — keduanya dipertahankan
(dan `database.db is None` diganti properti `is_connected` yang setara, lihat
§4).

---

## 4. Config & perubahan kode pemanggil

### 4.1 Config

- `DATABASE_URL` berubah makna dari `mongodb://...` menjadi
  `postgresql://user:pass@host:5432/dbname`. Format yang lama tidak dikenali
  adapter → kalau string mulai `mongodb://`, log error eksplisit "still
  pointing at MongoDB; migrate first (§7)".
- `DATABASE_NAME` dipertahankan (default `mltb`): kalau di-set, ia jadi nama
  database yang dituju (override nama di URL bila ada); kalau kosong, pakai
  nama dari URL. Ini mempertahankan dua variabel config yang sudah ada.
- `DB_ENCRYPTION_KEY` tidak tersentuh — enkripsi blob tetap di `blob_crypto`
  dan tetap dibaca dari env saja.

### 4.2 Fold akses mentah `database.db.*` (startup.py)

Ini satu-satunya kebocoran lapisan. Semua 20+ akses di `startup.py` difold
jadi method baru `DbManager` yang kecil & testable, lalu `startup.py` hanya
memakai `database.*` dan probe `database.is_connected`:

- `get_config_doc(bot_id) -> dict | None` — SELECT `settings_config`.
- `set_config_doc(bot_id, dict_)` / `merge_config_doc(bot_id, dict_)` — untuk
  `$set`-merge dengan cek keberadaan di `save_settings`/`load_settings`
  (`startup.py:162-176, 251-261`).
- `get_aria2_doc` / `get_qbit_doc`, `has_aria2_doc` / `has_qbit_doc`, `set_*`.
- `get_user_rows() -> list[tuple[int, dict]]` — ganti `users.find({})`.
- `migrate_legacy_keys`: unset legacy config keys & legacy user keys menjadi
  `pop` pada dict di sisi Python lalu replace/merge — `$unset` Mongo tidak punya
  padanan SQL yang lebih bersih daripada "baca → pop → tulis", dan ini
  idempotent juga. Legacy-blob prune memakai `list_blobs`/`delete_blob` yang
  sudah ada.
- `drop_rss(bot_id)` / `drop_tasks(bot_id)` menggantikan `database.db.rss[...].drop()`.

Hasilnya: tidak ada satu pun `import pymongo` / `gridfs` tersisa di `bot/`;
`db_handler.py` (atau modul pengganti seperinya, lihat §5) jadi satu-satunya
file yang menyentuh SQL.

### 4.3 `update.py`

`update.py` (deploy/update path) memakai **sync** `MongoClient` untuk membaca
config deploy. Perlu diperiksa saat eksekusi apakah jalur itu masih relevan;
kalau ya, ganti dengan koneksi psycopg **sync** ke tabel `settings_deploy`
(bukan async). Ini satu-satunya pemakai sync di luar bot, jadi bagian dari
commit tersendiri, bukan ikut commit utama.

---

## 5. Urutan commit (satu perubahan logis per commit, AGENTS.md §6)

Setiap commit wajib: test yang **sudah dibuktikan gagal tanpa perubahannya**
(ditulis di body commit), ruff tanpa temuan baru di file yang disentuh (dan
cabut baris ledger `db_handler.py`/`startup.py`/`user_sessions.py` yang ada di
`pyproject.toml` §Fase 10 kalau file-nya jadi bersih), pyrefly 0 errors,
anotasi tipe lengkap (§3 AGENTS). Baseline ruff direkam sebelum mulai (§2
AGENTS).

Nomor urut penting: **commit test-dulu-di-atas-API-baru** hadir sebelum
implementasi adapter, jadi "dibuktikan gagal" berarti ImportError/AttributeError
modul yang belum ada.

1. **`feat: add a postgres-backed DbManager beside the mongodb one`**
   - File baru: `bot/helper/ext_utils/pg_db_handler.py` (atau adapter dalam
     `db_handler.py` dengan backend terpilih via `Config.DATABASE_URL`), berisi
     koneksi psycopg + apply DDL + seluruh method `DbManager` yang diimplement
     ulang di atas PG. Belum di-wire: `database` tetap instance Mongo lama.
   - Test hermetik pertama (`tests/test_pg_handler.py`) — ditulis terhadap
     method yang belum ada → gagal, lalu lulus.
2. **`test: add hermetic unit tests for every pg DbManager method`** — lihat
   §6.1. Hermetik murni, tanpa server.
3. **`feat: wire the pg backend in and fold startup raw access into methods`**
   - `bot/core/startup.py` + `bot_helper/ext_utils/user_sessions.py` berhenti
     memegang pymongo; akses mentah `database.db.*` pindah ke method
     `DbManager` (§4.2). `database` kini backend PG. **Tidak ada perubahan
     perilaku** — ini commit terbesar, satu-satunya yang mengubah pemanggil.
   - Verifikasi: seluruh suite lama (872 test) tetap hijau tanpa perubahan isi
     test kecuali yang memang mengimpor bentuk pymongo (§6.2).
4. **`test: update the db-half of copy-record tests to the pg shape`** —
   `tests/test_copy_records.py` bagian "database round trip" yang meng-inject
   `dbm.db = SimpleNamespace(copies=...)` (bentuk pymongo) diganti inject
   adapter PG (bentuk baru), kalau langkah 3 memang mengubah bentuk itu. Kalau
   `DbManager` memakai seam store, test cukup bertukar fake — lihat §6.
5. **`feat: add real-postgres integration suite + CI service`** — file
   `tests/test_pg_integration.py` (marker `db`, skip kalau `PG_TEST_URL`
   kosong), registrasi marker `db` di `pytest.ini`, job CI baru yang
   menjalankan `pytest -m db` dengan service container postgres (§6.3).
6. **`feat: mongo→pg one-shot data migration tool`** — `tools/migrate_mongo_to_pg.py`
   (§7). Tidak ikut test suite (di `tools/`, di luar cakupan pyrefly).
7. **`docs: point README and config comments at PostgreSQL`** —
   `README.md:218-220`, `config_sample.py`, `docker-compose.yml` comment yang
   menyebut MongoDB/GridFS; tambah langkah provisioning PG singkat.

Commit 1–4 adalah *refactor perilaku-tidak-berubah* terhadap pemanggil; karena
pemanggilnya tidak berubah isinya (hanya `database.db.*` mentah yang difold),
dua-lapis harness diff/mutasi (`tools/_phase11b_*`, AGENTS §4) **tidak
diharuskan untuk seluruh suite** — yang diharuskan adalah test hermetik baru
yang membuktikan kontrak method, plus integrasi SQL (§6). Alasan dicatat
eksplisit di sini supaya tidak dibaca sebagai pelanggaran AGENTS §4: yang
berubah adalah *backend*, bukan *bentuk panggilan*; pemanggil di-*cover* oleh
872 test lama yang sudah ada dan tetap hijau.

---

## 6. Rencana test

Tujuan: (a) setiap method `DbManager` PG terbukti benar secara hermetik tanpa
server, (b) SQL-nya terbukti valid & perilakunya benar di Postgres nyata di CI,
(c) pemanggil (startup, copy, rss, settings) tidak berubah perilaku — dibuktikan
oleh suite lama yang tetap hijau.

### 6.1 Hermetik — `tests/test_pg_handler.py` (default `pytest`, tanpa server)

Pola yang dipakai `test_copy_records.py` untuk Mongo (fake koleksi) dipakai
ulang untuk PG: fake yang meniru **permukaan yang dipakai method**, bukan
driver sungguhan. Karena psycopg dipegang satu titik di adapter, taruh seam
kecil: `DbManager` memanggil satu method `_execute(sql, params) -> result` yang
di test diganti AsyncMock/ fake yang **mencatat SQL + params** dan menjawab
baris sesuai fixture. Ini sekaligus membuktikan *method memilih SQL & params
yang benar* (seperti differential harness menangkap panggilan).

Kasus per area (tiap `async def test_...`, `asyncio_mode = auto`):

- **koneksi & guard** — `_return = True` → setiap method no-op tanpa memanggil
  SQL; `connect` yang gagal kembali ke state disconnected (`_return` True,
  `is_connected` False); DDL di-apply sekali & idempotent (SQL kedua kalinya
  tidak error — diverifikasi nyata di integrasi, §6.3).
- **settings** — `update_config` menghasilkan merge `data || $2` (bukan
  replace), upsert saat baris belum ada; `get_config_doc` None saat kosong.
- **users** — `update_user_data` replace utuh: dokumen lama hilang key-nya
  kalau sudah tidak ada di dict (beda dengan merge); `USER_DOC_KEYS` di-pop
  sebelum simpan.
- **rss** — save/replace utuh per `(bot_id, user_id)`; delete; `trunc` hanya
  untuk bot yang sama.
- **tasks** — add/rm; `get_incomplete_tasks` = **select lalu delete** untuk bot
  yang sama, mengembalikan dict `{cid: {tag: [link,...]}}`.
- **copy records** — `save_copy_record` upsert; `find_copy_records(mid)`
  mengembalikan semua `mid` sama lintas chat; `_prune_copy_records` menyisakan
  `MAX_TASK_RECORDS` terbaru per user dan tidak menyentuh user lain — port
  kasus dari `test_copy_records.py` DB-half.
- **blob** — `save_blob` = upsert nama yang sama (revisi kedua menimpa);
  `_blob_name` men-namespace bot; `list_blobs` prefix strip namespace &
  dedup; `get_blob` None saat tak ada; `delete_blob`.

Test hermetik menegakkan **bentuk SQL** (mis. `update_config` harus
`data = data || %s`, bukan `data = %s`) — karena di situlah beda replace vs
merge yang menentukan perilaku.

### 6.2 Test lama yang tersentuh

- `tests/test_copy_records.py` — dua hal: (1) fixture `uploader_module` men-
  stub `bot.core.config_manager.Config.DATABASE_URL = "mongodb://test"` →
  ganti string PG, dan `_recording()` mengaktifkan perekaman lewat
  `Config.DATABASE_URL` non-kosong; (2) bagian *database round trip*
  (`FakeCursor`, `FakeCopies`, `_db(fake)`) meng-inject bentuk pymongo ke
  `DbManager.db` → diganti inject fake adapter PG bila bentuknya berubah
  (§5 commit 4). Isi asersi **tidak berubah** — hanya saluran fake-nya.
- `tests/test_module_imports.py` — mengimpor setiap modul `bot/`; tetap harus
  lulus, artinya `db_handler` tidak boleh impor psycopg di level modul dengan
  efek samping koneksi (import aman, koneksi hanya di `connect()`).

### 6.3 Integrasi — `tests/test_pg_integration.py` (Postgres nyata)

Menegakkan yang tidak bisa ditangkap fake: **SQL valid**, DDL idempotent,
perilaku `jsonb` round-trip (`units` list-dict bersarang utuh kembali), range
`substr` prefix blob dengan nama yang mengandung `.`/`/`, urutan deterministik
saat `at` kembar, dan `DELETE ... ANY(...)` prune.

Mekanisme gating:

```ini
# pytest.ini — tambah marker (--strict-markers aktif, wajib didaftarkan)
markers =
    slow: ...
    db: tests needing a real PostgreSQL (set PG_TEST_URL to run locally)
```

```python
# di test_pg_integration.py
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("PG_TEST_URL"), reason="set PG_TEST_URL to run db integration"
)

@pytest.mark.db
async def test_schema_apply_is_idempotent(): ...
```

- Lokal: `PG_TEST_URL=postgresql://... .venv/bin/python -m pytest -m db`.
- Default `pytest` **tidak** menyentuh server: file integrasi ke-skip karena env
  kosong. Suit hermetik tetap satu perintah.
- CI: job baru di `.github/workflows/ci.yml` (atau service di job test yang
  sudah ada) menjalankan `pytest -m db` dengan service container:

```yaml
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: mltb
      POSTGRES_PASSWORD: mltb
      POSTGRES_DB: mltb_test
    ports: ["5432:5432"]
```

  lalu langkah test `env: PG_TEST_URL: postgresql://mltb:mltb@localhost:5432/mltb_test`.

Kasus integrasi: idempotensi DDL dua kali; round-trip tiap tabel (insert →
select → bandingkan dict); blob `save`/`get`/`list`/`delete` dengan nama yang
sulit; prune copy records deterministik; `get_incomplete_tasks` select-lalu-
delete; *no-DB* probe tidak membuka koneksi. Fixture membuat database/skema
bersih per test (mis. schema terpisah atau `TRUNCATE` antar-test), bukan
menumpuk state.

### 6.4 Bukti "test bisa gagal" per commit

Mengikuti AGENTS §4: test baru ditulis lebih dulu terhadap method yang belum
ada → body commit melaporkan kegagalan yang terlihat (mis.
`ImportError: cannot import name 'PgDbManager'`, atau asersi yang menyala saat
backend masih Mongo). Test hermetik PG yang dijalankan saat backend masih Mongo
harus gagal dengan alasan yang jelas — itu buktinya.

---

## 7. Migrasi data sekali jalan — `tools/migrate_mongo_to_pg.py`

Jalan di **luar** bot (bot dimatikan saat maintenance), memakai koneksi **sync**
psycopg supaya tidak butuh loop bot, dan `pymongo.MongoClient` sync. Blob
dibaca lewat `gridfs` stream (yang merakit ulang chunk 255 KB) — tool **tidak**
perlu `DB_ENCRYPTION_KEY` karena menyalin ciphertext mentah.

Langkah (idempotent & resumable — bisa diulang, tidak dobel):

1. `--mongo-url`, `--mongo-db` (default `DATABASE_NAME`), `--pg-url`,
   `--bot-id` (default `TgClient.ID`, kalau tool dijalankan dari env bot;
   lewatkan kalau berjalan di mesin lain).
2. Buka PG, apply `pg_schema.sql`.
3. Untuk tiap tabel: `SELECT` dari Mongo **semua** dokumen lalu `INSERT ... ON
   CONFLICT DO NOTHING`/`UPDATE` → verifikasi jumlah baris sama.
4. Koleksi dinamis per bot (`rss.<bot>`, `tasks.<bot>`, `copies.<bot>`): hanya
   koleksi **milik `--bot-id`** yang dimigrasi (ada kemungkinan koleksi bot
   lain di DB yang sama — jangan disentuh).
5. Blob: `bucket.find({"filename": {"$gte": bot+"/", "$lt": bot+"/￿"}})`, untuk
   tiap nama stream ke PG. Verifikasi panjang bytes per nama sama.
6. Laporan akhir: jumlah baris & total bytes per tabel, plus **selisih apa pun
   yang gagal** — `success:false` tanpa error itu tidak cukup, harus ada angka.

Urutan maintenance window di VPS (Docker, memori `deployment-vps`):

1. `docker compose stop app` (bot berhenti; Mongo tidak ikut berhenti).
2. Provision PostgreSQL (container `postgres:16` + volume; kredensial di env,
   bukan di repo). Opsional tapi disarankan: pg_dump cadangan.
3. Jalankan tool dengan `--mongo-url` dari `.env` VPS & URL PG baru; periksa
   laporan.
4. Ubah `DATABASE_URL` di env ke PG; `docker compose up -d app`.
5. Boot: `restore_*` membaca PG — verifikasi user_data, rss, config, dan
   thumbnail/user blob pulih (bandingkan jumlah dengan laporan tool).
6. Rollback: balik `DATABASE_URL` ke Mongo dan `up -d` — Mongo belum disentuh
   (tool hanya baca). Setelah beberapa hari stabil, baru matikan Mongo.

Batas: data kecil (ratusan–ribuan dok, blob < ~100 MB), jadi sekali putus tanpa
strategi dual-write. Kalau nanti bot dipakai banyak user/chat aktif, naikkan
ke migrasi per-koleksi bertahap — di luar scope dokumen ini.

---

## 8. Perilaku yang sengaja dipertahankan (jangan "diperbaiki" diam-diam)

Hal-hal berikut tampak janggal tapi **disengaja**; migrasi wajib mengawetkannya
persis, dan penyimpangan apa pun harus jadi commit `fix:` tersendiri dengan
test-nya:

1. **No-DB mode**: `DATABASE_URL` kosong → semua method no-op, bot hidup dari
   memori + `user_sessions.json`.
2. **`users` global, sisanya per-bot.** Koleksi `users` dan blob `users/...`
   ternyata tetap di-namespace per bot via `_blob_name` (default `TgClient.ID`)
   — hanya tabel `users` yang tidak punya `bot_id`. Jangan menyeragamkan.
3. **Replace utuh vs merge.** `update_user_data` & `rss_update` = replace utuh
   (key yang hilang dari dict **hilang** dari DB); `update_config`/`update_aria2`/
   `update_qbittorrent` = merge `$set` (key lain **tidak** hilang). Ini beda
   yang menentukan dan paling gampang salah di SQL.
4. **`get_incomplete_tasks` menghapus** koleksi setelah dibaca; `_prune_blob`
   keep=1; prune copy 200/user lintas-user-terpisah.
5. **Tanpa index non-PK**, konsisten dengan komentar `_prune_copy_records`.
6. **Enkripsi blob** tetap di `blob_box` sebelum masuk DB; DB tidak pernah
   melihat plaintext.

---

## 9. Risiko & mitigasi

| Risiko | Mitigasi |
|---|---|
| Perilaku replace-vs-merge salah | Test hermetik menegakkan bentuk SQL (§6.1); integrasi round-trip dict (§6.3) |
| `jsonb` round-trip berubah bentuk (list→tuple, dst.) | psycopg adaptasi jsonb standar; integrasi membandingkan dict persis (deep-equal), termasuk `units` bersarang |
| Urutan string prefix blob beda karena collation | Hindari LIKE & `>` — pakai `substr(name,1,length($1)) = $1` (bebas collation & wildcard) |
| `startup.py` refactor paling besar → regresi boot | Fold ke method kecil yang di-cover test hermetik; verifikasi boot nyata di VPS (§7.6); satu commit terpisah |
| Blob Mongo ter-chunk; salah salin | Baca via GridFS stream (assembled), verifikasi panjang bytes per nama di tool |
| Dua bot berbagi satu DB | Tool hanya migrasi koleksi `--bot-id`; tabel PG berkolom `bot_id` |
| Test default butuh server | Marker `db` + skip kalau `PG_TEST_URL` kosong; CI sediakan service (§6.3) |
| `psycopg[binary]` tidak tersedia untuk platform VPS | Wheels biner linux x86_64 tersedia; fallback `psycopg` (pure python + libpq) kalau perlu — dicatat di requirements |
| Lupa URL Mongo di `.env` VPS | §7.1 baca dari env VPS; jangan hardcode |

---

## 10. Yang belum diputuskan / perlu diukur saat eksekusi

- Bentuk seam test yang tepat: `DbManager._execute` dicatat-fake (§6.1) vs
  store objek yang di-inject. Keputusan ini menentukan apakah
  `tests/test_copy_records.py` DB-half perlu berubah di commit 4 — pilih saat
  menulis commit 1, dan sesuaikan rencana commit 4.
- Apakah `update.py` jalur sync masih dipakai di deploy (pemilik repo VPS) —
  perlu konfirmasi; kalau tidak, §4.3 jadi commit kecil terpisah atau dihapus.
- Angka di AGENTS.md (872 test, 41 temuan ruff, 36 pyrefly suppressed) adalah
  snapshot — ukur ulang sebelum mulai (§2 AGENTS).
- Lokasi PG di VPS (container sendiri vs managed) & kredensial — di luar repo,
  dipegang owner; plan mengasumsikan `postgres:16` container + volume.
