"""Tests for Telegram client creation with proxy configurations."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bot.core.config_manager import Config
from bot.core.telegram_manager import TgClient, get_user_client


async def test_start_bot_empty_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty TG_PROXY dict should not crash Kurigram Client with ValueError."""
    monkeypatch.setattr(Config, "BOT_TOKEN", "123456789:ABCDefghIJKLmnOPqrSTuvWXyz")
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Config, "TG_PROXY", {})

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        with patch(
            "bot.core.telegram_manager.own_account",
            return_value=type("User", (), {"username": "testbot"})(),
        ):
            await TgClient.start_bot()
            assert TgClient.bot is not None
            assert TgClient.bot.proxy is None


async def test_start_bot_valid_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured TG_PROXY dict should be normalized and stored on Client."""
    monkeypatch.setattr(Config, "BOT_TOKEN", "123456789:ABCDefghIJKLmnOPqrSTuvWXyz")
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(
        Config,
        "TG_PROXY",
        {"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080},
    )

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        with patch(
            "bot.core.telegram_manager.own_account",
            return_value=type("User", (), {"username": "testbot"})(),
        ):
            await TgClient.start_bot()
            assert TgClient.bot is not None
            assert TgClient.bot.proxy is not None


async def test_start_user_empty_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_user with empty TG_PROXY should instantiate Client without error."""
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Config, "USER_SESSION_STRING", "BQAAAAA...")
    monkeypatch.setattr(Config, "TG_PROXY", {})

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        with patch(
            "bot.core.telegram_manager.own_account",
            return_value=type("User", (), {"is_premium": False})(),
        ):
            await TgClient.start_user()
            assert TgClient.user is not None
            assert TgClient.user.proxy is None


async def test_get_user_client_empty_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_user_client with empty TG_PROXY should instantiate Client without error."""
    from bot import user_data

    user_id = 99999
    user_data[user_id] = {"USER_SESSION_STRING": "BQAAAAA..."}
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Config, "TG_PROXY", {})

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        client = await get_user_client(user_id)
        assert client is not None
        assert client.proxy is None


def test_user_credentials_fall_back_to_bot_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who set no user pair keeps using the bot's, as before."""
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "bot-hash")
    monkeypatch.setattr(Config, "USER_TELEGRAM_API", 0)
    monkeypatch.setattr(Config, "USER_TELEGRAM_HASH", "")

    assert Config.user_credentials() == (12345, "bot-hash")


def test_user_credentials_use_user_pair_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete user pair is what the user-account clients sign in with."""
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "bot-hash")
    monkeypatch.setattr(Config, "USER_TELEGRAM_API", 6789)
    monkeypatch.setattr(Config, "USER_TELEGRAM_HASH", "user-hash")

    assert Config.user_credentials() == (6789, "user-hash")


def test_user_credentials_half_set_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half a pair would pair one app's id with another's hash; use neither."""
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "bot-hash")
    monkeypatch.setattr(Config, "USER_TELEGRAM_API", 6789)
    monkeypatch.setattr(Config, "USER_TELEGRAM_HASH", "")

    assert Config.user_credentials() == (12345, "bot-hash")


async def test_start_user_uses_user_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The USER_SESSION_STRING client is a user account, so it takes the pair."""
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Config, "USER_TELEGRAM_API", 6789)
    monkeypatch.setattr(
        Config, "USER_TELEGRAM_HASH", "fedcba9876543210fedcba9876543210"
    )
    monkeypatch.setattr(Config, "USER_SESSION_STRING", "BQAAAAA...")
    monkeypatch.setattr(Config, "TG_PROXY", {})

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        with patch(
            "bot.core.telegram_manager.own_account",
            return_value=type("User", (), {"is_premium": False})(),
        ):
            await TgClient.start_user()
            assert TgClient.user is not None
            assert TgClient.user.api_id == 6789
            assert TgClient.user.api_hash == "fedcba9876543210fedcba9876543210"
    TgClient.user = None


async def test_get_user_client_uses_user_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sessions born from /login are used with the pair they were made under."""
    from bot import user_clients, user_data

    user_id = 99998
    user_data[user_id] = {"USER_SESSION_STRING": "BQAAAAA..."}
    monkeypatch.setattr(Config, "TELEGRAM_API", 12345)
    monkeypatch.setattr(Config, "TELEGRAM_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Config, "USER_TELEGRAM_API", 6789)
    monkeypatch.setattr(
        Config, "USER_TELEGRAM_HASH", "fedcba9876543210fedcba9876543210"
    )
    monkeypatch.setattr(Config, "TG_PROXY", {})

    with patch("pyrogram.Client.start", new_callable=AsyncMock):
        client = await get_user_client(user_id)

    try:
        assert client is not None
        assert client.api_id == 6789
        assert client.api_hash == "fedcba9876543210fedcba9876543210"
    finally:
        user_clients.pop(user_id, None)
        user_data.pop(user_id, None)
