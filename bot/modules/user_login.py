from pyrogram import Client, enums
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeEmpty,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)
from pyrogram.types import User

from .. import LOGGER, user_data, user_clients
from ..core.config_manager import Config
from ..core.telegram_manager import get_user_client, stop_user_client
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.user_sessions import save_user_session
from ..helper.telegram_helper.conversation import wait_for_reply
from ..helper.telegram_helper.message_utils import (
    delete_message,
    send_message,
)

TIMEOUT = 300
pending_logins = set()


async def _ask(client, message, user_id, text):
    """Send text and return the text of the next reply of user_id in this chat.

    The reply is deleted right away since it holds sensitive data. Returns None
    on timeout and the prompt is appended to garbage to be deleted later.
    """
    prompt = await send_message(message, text)
    if isinstance(prompt, str):
        return None, None
    return prompt, await wait_for_reply(client, message, user_id, TIMEOUT)


@new_task
async def user_login(client, message):
    if message.chat.type != enums.ChatType.PRIVATE or not message.from_user:
        await send_message(
            message, "Use this command in my PM, your credentials are private!"
        )
        return
    user_id = message.from_user.id
    if not Config.TELEGRAM_API or not Config.TELEGRAM_HASH:
        await send_message(message, "TELEGRAM_API and TELEGRAM_HASH are required!")
        return
    if user_id in pending_logins:
        await send_message(
            message, "You already have a login in progress! Finish it first."
        )
        return

    pending_logins.add(user_id)
    garbage = [message]
    temp_client = None
    result = ""
    try:
        prompt, phone_number = await _ask(
            client,
            message,
            user_id,
            f"Send your phone number in international format. Example: <code>+628123456789</code>\n\nTimeout: {TIMEOUT} sec",
        )
        garbage.append(prompt)
        if not phone_number:
            result = "Timed out! Send /login again."
            return
        if phone_number.startswith("/"):
            result = "Login cancelled!"
            return

        temp_client = Client(
            f"login_{user_id}",
            Config.TELEGRAM_API,
            Config.TELEGRAM_HASH,
            proxy=Config.TG_PROXY,
            in_memory=True,
            no_updates=True,
            parse_mode=enums.ParseMode.HTML,
            sleep_threshold=60,
        )
        await temp_client.connect()

        try:
            sent_code = await temp_client.send_code(phone_number)
        except PhoneNumberInvalid:
            result = "Invalid phone number! Send /login again."
            return
        except FloodWait as f:
            result = f"Telegram is limiting this number, try again after {f.value} sec!"
            return

        prompt, otp = await _ask(
            client,
            message,
            user_id,
            "Send the OTP you received.\n\n<b>Separate every digit with a space</b> "
            "(example: <code>1 2 3 4 5</code>), otherwise Telegram will detect it and "
            f"revoke the code!\n\nTimeout: {TIMEOUT} sec",
        )
        garbage.append(prompt)
        if not otp:
            result = "Timed out! Send /login again."
            return
        if otp.startswith("/"):
            result = "Login cancelled!"
            return
        otp = "".join(otp.split())

        try:
            signed_in = await temp_client.sign_in(
                phone_number, sent_code.phone_code_hash, otp
            )
            if not isinstance(signed_in, User):
                result = "This number is not registered on Telegram, sign up first!"
                return
        except SessionPasswordNeeded:
            prompt, password = await _ask(
                client,
                message,
                user_id,
                f"Send your two-step verification (2FA) password.\n\nTimeout: {TIMEOUT} sec",
            )
            garbage.append(prompt)
            if not password:
                result = "Timed out! Send /login again."
                return
            if password.startswith("/"):
                result = "Login cancelled!"
                return
            try:
                await temp_client.check_password(password)
            except PasswordHashInvalid:
                result = "Wrong 2FA password! Send /login again."
                return
        except (PhoneCodeInvalid, PhoneCodeEmpty):
            result = "Wrong OTP! Send /login again."
            return
        except PhoneCodeExpired:
            result = "The OTP has expired! Send /login again."
            return

        session_string = await temp_client.export_session_string()
        await temp_client.disconnect()
        temp_client = None

        await stop_user_client(user_id)
        update_user_ldata(user_id, "USER_SESSION_STRING", session_string)
        await save_user_session(user_id)
        LOGGER.info(f"USER_SESSION_STRING has been saved for {user_id}")
        result = (
            "Logged in successfully! Your telegram links will be downloaded with "
            "your own session from now on. Use /logout to remove it."
        )
    except Exception as e:
        LOGGER.error(f"Login failed for {user_id}. {e}")
        result = f"ERROR: {e}"
    finally:
        pending_logins.discard(user_id)
        if temp_client is not None:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
        if result:
            await send_message(message, result)
        for msg in garbage:
            if msg is not None:
                await delete_message(msg)


@new_task
async def user_logout(_, message):
    user_id = (message.from_user or message.sender_chat).id
    if not user_data.get(user_id, {}).get("USER_SESSION_STRING"):
        await send_message(message, "You are not logged in!")
        return
    client = await get_user_client(user_id)
    if client is not None:
        try:
            await client.log_out()
            user_clients.pop(user_id, None)
        except Exception as e:
            LOGGER.error(f"Failed to revoke session of {user_id}. {e}")
    await stop_user_client(user_id)
    update_user_ldata(user_id, "USER_SESSION_STRING", "")
    await save_user_session(user_id)
    LOGGER.info(f"USER_SESSION_STRING has been removed for {user_id}")
    await send_message(message, "Logged out! Your session string has been removed.")
