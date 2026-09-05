# REQUIRED CONFIG
BOT_TOKEN = ""
OWNER_ID = 0
TELEGRAM_API = 0
TELEGRAM_HASH = ""
# OPTIONAL CONFIG
TG_PROXY = {}
USER_SESSION_STRING = ""
CMD_SUFFIX = ""
AUTHORIZED_CHATS = ""
SUDO_USERS = ""
# PostgreSQL connection string, e.g.
# postgresql://user:pass@host:5432/dbname. The schema is created on
# first boot; leave DATABASE_NAME empty when the URL names the database.
DATABASE_URL = ""
DATABASE_NAME = "mltb"
# DB_ENCRYPTION_KEY encrypts private files (cookies.txt, .netrc) before they
# are stored as encrypted blobs in PostgreSQL. It is read from
# the environment ONLY and is deliberately not a config variable: this file is
# itself saved to the database, so a key here would sit next to the data it
# protects. Set any passphrase, e.g. DB_ENCRYPTION_KEY=... in your .env or
# docker-compose. Leave it unset to store private files as plain bytes.
# Changing or losing it makes already-stored private files unreadable.
STATUS_LIMIT = 4
STATUS_UPDATE_INTERVAL = 15
FILELION_API = ""
STREAMWISH_API = ""
ALLDEBRID_API_KEY = ""
# Mega encrypts files client-side, so the bot decrypts the stream as it
# downloads. Metadata is resolved via gateway (https://api.piyann.me).
# MEGA_PROXY_URL can be set to one or multiple Cloudflare Worker proxies (comma/space separated)
# (e.g. "https://proxy-1.vianstefani754.workers.dev, https://proxy-2.vianstefani754.workers.dev").
# If left empty, it defaults to proxy-1 through proxy-5.
WARP_ENABLED = True
WARP_PROXY_PORT = 40000
MEGA_PROXY_URL = "https://proxy-1.vianstefani754.workers.dev, https://proxy-2.vianstefani754.workers.dev, https://proxy-3.vianstefani754.workers.dev, https://proxy-4.vianstefani754.workers.dev, https://proxy-5.vianstefani754.workers.dev"
MEGA_CONNECTIONS = 4
MEGA_MAX_RESTARTS = 3
GATEWAY_URL = ""
GATEWAY_TOKEN = ""
EXCLUDED_EXTENSIONS = ""
INCLUDED_EXTENSIONS = ""
INCOMPLETE_TASK_NOTIFIER = False
YT_DLP_OPTIONS = ""
NAME_SUBSTITUTE = r""
FFMPEG_CMDS = {"merge": ["-f concat -safe 0 -i mltb.txt -c copy mltb.mp4 -del"]}
# Update
UPSTREAM_REPO = ""
UPSTREAM_BRANCH = "master"
# Leech
LEECH_SPLIT_SIZE = 0
AS_DOCUMENT = False
EQUAL_SPLITS = False
MEDIA_GROUP = False
USER_TRANSMISSION = False
HYBRID_LEECH = False
LEECH_FILENAME_PREFIX = ""
LEECH_DUMP_CHAT = ""
CLONE_DUMP_CHATS = ""
THUMBNAIL_LAYOUT = ""
FILES_LINKS = False
# qBittorrent/Aria2c
TORRENT_TIMEOUT = 0
BASE_URL = ""
BASE_URL_PORT = 0
WEB_PINCODE = False
# Queueing system
QUEUE_ALL = 0
QUEUE_DOWNLOAD = 0
QUEUE_UPLOAD = 0
# How many links may be resolved (scraped / metadata-fetched) at the same time.
# The queue limits above only bound transfers and are checked after a link is
# resolved, so this is the knob for bulk (-b) runs: 4 links at a time keeps the
# gateway from rate-limiting the batch. 0 disables the gate.
RESOLVE_CONCURRENCY = 4
# RSS
RSS_DELAY = 600
RSS_CHAT = ""
RSS_SIZE_LIMIT = 0
# Torrent Search
SEARCH_API_LINK = ""
SEARCH_LIMIT = 0
SEARCH_PLUGINS = [
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/piratebay.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/limetorrents.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torlock.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torrentscsv.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/eztv.py",
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/nova3/engines/torrentproject.py",
    "https://raw.githubusercontent.com/MaurizioRicci/qBittorrent_search_engines/master/kickass_torrent.py",
    "https://raw.githubusercontent.com/MaurizioRicci/qBittorrent_search_engines/master/yts_am.py",
    "https://raw.githubusercontent.com/MadeOfMagicAndWires/qBit-plugins/master/engines/linuxtracker.py",
    "https://raw.githubusercontent.com/MadeOfMagicAndWires/qBit-plugins/master/engines/nyaasi.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/ettv.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/glotorrents.py",
    "https://raw.githubusercontent.com/LightDestory/qBittorrent-Search-Plugins/master/src/engines/thepiratebay.py",
    "https://raw.githubusercontent.com/v1k45/1337x-qBittorrent-search-plugin/master/leetx.py",
    "https://raw.githubusercontent.com/nindogo/qbtSearchScripts/master/magnetdl.py",
    "https://raw.githubusercontent.com/msagca/qbittorrent_plugins/main/uniondht.py",
    "https://raw.githubusercontent.com/khensolomon/leyts/master/yts.py",
]
