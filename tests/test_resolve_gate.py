"""Tests for the resolve gate.

Bulk dispatch starts every task at once and the download queue is only consulted
*after* a link has been resolved, so a hundred-link batch used to ask the gateway
to resolve a hundred links in the same breath -- and got rate limited for it.
The gate is the fix, so what is pinned here is the count: never more than
``RESOLVE_CONCURRENCY`` inside the block, every waiter eventually gets in, and a
scrape that raises hands its slot back (the error is reported outside the gate,
and a slot leaked there would stall the rest of the batch).
"""

from __future__ import annotations

import asyncio

import pytest

from bot.core.config_manager import Config
from bot.helper.util import resolve_gate as rg


@pytest.fixture(autouse=True)
def _fresh_gate():
    """A semaphore is bound to the loop that first awaited it, and every test
    gets its own loop, so the cached one must not survive between tests."""
    rg.reset_resolve_gate()
    yield
    rg.reset_resolve_gate()


async def _peak_concurrency(count, hold=0.01):
    """Run *count* gated blocks at once; return the highest overlap seen."""
    state = {"now": 0, "peak": 0, "ran": 0}

    async def _one():
        async with rg.resolve_gate():
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            state["ran"] += 1
            await asyncio.sleep(hold)
            state["now"] -= 1

    await asyncio.gather(*[_one() for _ in range(count)])
    return state


async def test_gate_caps_concurrent_resolves(monkeypatch):
    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", 3)

    state = await _peak_concurrency(12)

    assert state["peak"] == 3
    # capped, not dropped: a gated link is delayed, never skipped
    assert state["ran"] == 12


async def test_zero_disables_the_gate(monkeypatch):
    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", 0)

    state = await _peak_concurrency(12)

    assert state["peak"] == 12


async def test_garbage_limit_disables_the_gate(monkeypatch):
    """A hand-typed setting must not take the resolve path down with it."""
    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", "four")

    state = await _peak_concurrency(5)

    assert state["ran"] == 5


async def test_limit_change_applies_without_a_restart(monkeypatch):
    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", 1)
    assert (await _peak_concurrency(4))["peak"] == 1

    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", 4)
    assert (await _peak_concurrency(4))["peak"] == 4


async def test_a_raising_block_releases_its_slot(monkeypatch):
    monkeypatch.setattr(Config, "RESOLVE_CONCURRENCY", 1)

    with pytest.raises(RuntimeError):
        async with rg.resolve_gate():
            raise RuntimeError("scrape blew up")

    # the next link must not wait on the slot the failed one held
    async def _acquire():
        async with rg.resolve_gate():
            return True

    assert await asyncio.wait_for(_acquire(), timeout=1)


def test_gate_is_on_by_default():
    """A default of 0 would leave bulk exactly where it was."""
    assert Config.RESOLVE_CONCURRENCY > 0
