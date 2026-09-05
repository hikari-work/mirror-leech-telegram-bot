from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from json import dumps, loads

from ... import LOGGER, user_data
from ..storage.db_handler import database

SESSIONS_FILE = "user_sessions.json"


async def _dump_local_sessions():
    data = {
        str(uid): udict["USER_SESSION_STRING"]
        for uid, udict in user_data.items()
        if udict.get("USER_SESSION_STRING")
    }
    async with aiopen(SESSIONS_FILE, "w") as f:
        await f.write(dumps(data, indent=2))


async def load_user_sessions():
    """Load personal session strings from local json. Used when database is off."""
    if not await aiopath.exists(SESSIONS_FILE):
        return
    try:
        async with aiopen(SESSIONS_FILE, "r") as f:
            data = loads(await f.read())
    except Exception as e:
        LOGGER.error(f"Failed to read {SESSIONS_FILE}. {e}")
        return
    for uid, session_string in data.items():
        if not session_string:
            continue
        user_data.setdefault(int(uid), {})["USER_SESSION_STRING"] = session_string
    if data:
        LOGGER.info(f"User session(s) has been imported from {SESSIONS_FILE}")


async def save_user_session(user_id):
    """Persist USER_SESSION_STRING of user_id to database or to local json."""
    if not database._return:
        await database.update_user_data(user_id)
        return
    try:
        await _dump_local_sessions()
    except Exception as e:
        LOGGER.error(f"Failed to save session in {SESSIONS_FILE}. {e}")
