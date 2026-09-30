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
