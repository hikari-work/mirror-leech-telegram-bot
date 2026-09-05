from sys import exit
from importlib import import_module
from logging import (
    FileHandler,
    StreamHandler,
    INFO,
    basicConfig,
    error as log_error,
    info as log_info,
)
from os import path, remove, getenv
from subprocess import run as srun
from typing import Dict, Any

from psycopg import connect as pg_connect
from psycopg.rows import dict_row

if path.exists("log.txt"):
    with open("log.txt", "r+") as f:
        f.truncate(0)

if path.exists("rlog.txt"):
    remove("rlog.txt")

basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)


def load_config() -> Dict[str, Any]:
    """Load configuration from config module or environment variables."""
    try:

        settings = import_module("config")
        return {
            key: value.strip() if isinstance(value, str) else value
            for key, value in vars(settings).items()
            if not key.startswith("__")
        }
    except ModuleNotFoundError:
        log_info("Config module not found, loading from environment variables...")
        return {
            "BOT_TOKEN": getenv("BOT_TOKEN", ""),
            "DATABASE_URL": getenv("DATABASE_URL", ""),
            "DATABASE_NAME": getenv("DATABASE_NAME", "mltb"),
            "UPSTREAM_REPO": getenv("UPSTREAM_REPO", ""),
            "UPSTREAM_BRANCH": getenv("UPSTREAM_BRANCH", "master"),
        }


config_file = load_config()

BOT_TOKEN = config_file.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    log_error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

BOT_ID = BOT_TOKEN.split(":", 1)[0]

DATABASE_NAME = config_file.get("DATABASE_NAME", "mltb")

if DATABASE_URL := config_file.get("DATABASE_URL", "").strip():
    try:
        if DATABASE_NAME:
            conn = pg_connect(DATABASE_URL, dbname=DATABASE_NAME, autocommit=True)
        else:
            conn = pg_connect(DATABASE_URL, autocommit=True)
        conn.row_factory = dict_row
        row = conn.execute(
            "SELECT data FROM settings_config WHERE bot_id = %s", (BOT_ID,)
        ).fetchone()
        stored = row["data"] if row is not None else None
        if stored:
            for key in ("UPSTREAM_REPO", "UPSTREAM_BRANCH"):
                if key in stored:
                    config_file[key] = stored[key]
        conn.close()
    except Exception as e:
        log_error(f"Database ERROR: {e}")

UPSTREAM_REPO = config_file.get("UPSTREAM_REPO", "").strip()

UPSTREAM_BRANCH = config_file.get("UPSTREAM_BRANCH", "").strip() or "master"

if UPSTREAM_REPO:
    if path.exists(".git"):
        srun(["rm", "-rf", ".git"])

    update = srun(
        [
            f"git init -q \
                     && git config --global user.email e.anastayyar@gmail.com \
                     && git config --global user.name mltb \
                     && git add . \
                     && git commit -sm update -q \
                     && git remote add origin {UPSTREAM_REPO} \
                     && git fetch origin -q \
                     && git reset --hard origin/{UPSTREAM_BRANCH} -q"
        ],
        shell=True,
    )

    if update.returncode == 0:
        log_info("Successfully updated with latest commit from UPSTREAM_REPO")
    else:
        log_error(
            "Something went wrong while updating, check UPSTREAM_REPO if valid or not!"
        )
