from importlib import import_module
from ast import literal_eval
from os import getenv

from bot import LOGGER


class Config:
    ALLDEBRID_API_KEY = ""
    TORBOX_API_KEY = ""
    AS_DOCUMENT = False
    AUTHORIZED_CHATS = ""
    BASE_URL = ""
    BASE_URL_PORT = 80
    BOT_TOKEN = ""
    CMD_SUFFIX = ""
    CLONE_DUMP_CHATS = ""
    DATABASE_URL = ""
    DATABASE_NAME = "mltb"
    EQUAL_SPLITS = False
    EXCLUDED_EXTENSIONS = ""
    INCLUDED_EXTENSIONS = ""
    FFMPEG_CMDS = {}
    FILELION_API = ""
    FILES_LINKS = False
    INCOMPLETE_TASK_NOTIFIER = False
    LEECH_DUMP_CHAT = ""
    LEECH_FILENAME_PREFIX = ""
    LEECH_SPLIT_SIZE = 2097152000
    MEDIA_GROUP = False
    # Mega encrypts every file client-side, so the bot decrypts the stream as it
    # downloads. Mega also meters anonymous downloads per IP, so the traffic
    # goes through WARP's SOCKS5 proxy and the tunnel is restarted for a fresh
    # IP whenever Mega reports the quota spent.
    WARP_ENABLED = True
    WARP_PROXY_PORT = 40000
    # Gateway configuration for scrapers and resolvers
    GATEWAY_URL = "https://api.piyann.me"
    GATEWAY_TOKEN = ""
    # An explicit proxy for Mega traffic, overriding WARP's own listener.
    MEGA_PROXY_URL = ""
    # Ranged connections per file. Mega tolerates a handful; too many is what
    # walks into a rate limit.
    MEGA_CONNECTIONS = 4
    # How many times one file may rotate the egress IP before giving up.
    MEGA_MAX_RESTARTS = 3
    HYBRID_LEECH = False
    NAME_SUBSTITUTE = r""
    OWNER_ID = 0
    QUEUE_ALL = 0
    QUEUE_DOWNLOAD = 0
    QUEUE_UPLOAD = 0
    RSS_CHAT = ""
    RSS_DELAY = 600
    RSS_SIZE_LIMIT = 0
    SEARCH_API_LINK = ""
    SEARCH_LIMIT = 0
    SEARCH_PLUGINS = []
    STATUS_LIMIT = 4
    STATUS_UPDATE_INTERVAL = 15
    STREAMWISH_API = ""
    SUDO_USERS = ""
    TELEGRAM_API = 0
    TELEGRAM_HASH = ""
    TG_PROXY = {}
    THUMBNAIL_LAYOUT = ""
    TORRENT_TIMEOUT = 0
    UPSTREAM_REPO = ""
    UPSTREAM_BRANCH = "master"
    USER_SESSION_STRING = ""
    USER_TRANSMISSION = False
    WEB_PINCODE = False
    YT_DLP_OPTIONS = {}

    @classmethod
    def _convert(cls, key: str, value):
        if not hasattr(cls, key):
            raise KeyError(f"{key} is not a valid configuration key.")

        expected_type = type(getattr(cls, key))

        if value is None:
            return None

        if isinstance(value, expected_type):
            return value

        if expected_type is bool:
            return str(value).strip().lower() in {"true", "1", "yes"}

        if expected_type in [list, dict]:
            if not isinstance(value, str):
                raise TypeError(
                    f"{key} should be {expected_type.__name__}, got {type(value).__name__}"
                )

            if not value:
                return expected_type()

            try:
                evaluated = literal_eval(value)
                if not isinstance(evaluated, expected_type):
                    raise TypeError(
                        f"Expected {expected_type.__name__}, got {type(evaluated).__name__}"
                    )
                return evaluated
            except (ValueError, SyntaxError, TypeError) as e:
                raise TypeError(
                    f"{key} should be {expected_type.__name__}, got invalid string: {value}"
                ) from e

        try:
            return expected_type(value)
        except (ValueError, TypeError) as exc:
            raise TypeError(
                f"Invalid type for {key}: expected {expected_type}, got {type(value)}"
            ) from exc

    @classmethod
    def get(cls, key: str):
        return getattr(cls, key, None)

    @classmethod
    def set(cls, key: str, value) -> None:
        if not hasattr(cls, key):
            raise KeyError(f"{key} is not a valid configuration key.")

        converted_value = cls._convert(key, value)
        setattr(cls, key, converted_value)

    @classmethod
    def get_all(cls):
        return {
            key: getattr(cls, key)
            for key in cls.__dict__.keys()
            if not key.startswith("__") and not callable(getattr(cls, key))
        }

    @classmethod
    def _is_valid_config_attr(cls, attr: str) -> bool:
        if attr.startswith("__") or callable(getattr(cls, attr, None)):
            return False
        return hasattr(cls, attr)

    @classmethod
    def _process_config_value(cls, attr: str, value):
        if not value:
            return None

        converted_value = cls._convert(attr, value)

        if isinstance(converted_value, str):
            converted_value = converted_value.strip()

        if attr in {
            "BASE_URL",
            "SEARCH_API_LINK",
            "MEGA_PROXY_URL",
        }:
            return converted_value.strip("/") if converted_value else ""

        return converted_value

    @classmethod
    def _load_from_module(cls) -> bool:
        try:
            settings = import_module("config")
        except ModuleNotFoundError:
            return False

        for attr in dir(settings):
            if not cls._is_valid_config_attr(attr):
                continue

            raw_value = getattr(settings, attr)
            processed_value = cls._process_config_value(attr, raw_value)

            if processed_value is not None:
                setattr(cls, attr, processed_value)

        return True

    @classmethod
    def _load_from_env(cls) -> None:
        for attr in dir(cls):
            if not cls._is_valid_config_attr(attr):
                continue

            env_value = getenv(attr)
            if env_value is None:
                continue

            processed_value = cls._process_config_value(attr, env_value)
            if processed_value is not None:
                setattr(cls, attr, processed_value)

    @classmethod
    def _validate_required_config(cls) -> None:
        required_keys = ["BOT_TOKEN", "OWNER_ID", "TELEGRAM_API", "TELEGRAM_HASH"]

        for key in required_keys:
            value = getattr(cls, key)
            if isinstance(value, str):
                value = value.strip()
            if not value:
                raise ValueError(f"{key} variable is missing!")

    @classmethod
    def load(cls) -> None:
        if not cls._load_from_module():
            LOGGER.info(
                "Config module not found, loading from environment variables..."
            )
            cls._load_from_env()

        cls._validate_required_config()

    @classmethod
    def load_dict(cls, config_dict) -> None:
        for key, value in config_dict.items():
            if not hasattr(cls, key):
                continue

            processed_value = cls._process_config_value(key, value)

            if processed_value is not None:
                setattr(cls, key, processed_value)

        cls._validate_required_config()
