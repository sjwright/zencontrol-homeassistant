"""Regression: config-entry startup must not deadlock CREATE_ENTRY."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.zencontrol_tpi.hub import ZenHub


def _hub_for_start(*, entry_tasks: set[asyncio.Task[Any]] | None = None) -> ZenHub:
    hass = MagicMock()
    hass.async_block_till_done = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry._tasks = entry_tasks if entry_tasks is not None else set()
    entry.async_create_task = MagicMock()
    entry.async_create_background_task = MagicMock()

    with patch.object(ZenHub, "__init__", lambda self, *a, **k: None):
        hub = ZenHub.__new__(ZenHub)

    hub.hass = hass
    hub.entry = entry
    hub.runtime = MagicMock()
    hub.runtime.started = False
    hub.runtime.async_ensure_started = AsyncMock()
    hub.runtime.async_configure_controller_events = AsyncMock()
    hub.controller = MagicMock()
    hub._stopping = False
    hub._controller_status = "unreachable"
    hub._status_entity = None
    hub._discovery_complete = False
    hub._discovery_notified = False
    hub._setup_complete = False
    hub._discovery_callbacks = []
    hub.sync_device_assignments = MagicMock()
    hub._wait_for_controller = AsyncMock()
    hub._discover_entities = AsyncMock()
    hub._refresh_light_states = AsyncMock()
    hub.set_controller_status = MagicMock()
    return hub


@pytest.mark.asyncio
async def test_async_start_ignores_unrelated_hass_hang() -> None:
    """CREATE_ENTRY awaits setup; waiting on all hass tasks deadlocks the UI."""
    hub = _hub_for_start()

    async def hang_forever() -> None:
        await asyncio.Event().wait()

    # Old bug: async_start awaited this and never returned to the config flow.
    hub.hass.async_block_till_done = hang_forever

    await asyncio.wait_for(hub.async_start(), timeout=1.0)

    hub.set_controller_status.assert_called_with("online")
    assert hub._discovery_notified is True
    hub.sync_device_assignments.assert_called()


@pytest.mark.asyncio
async def test_notify_awaits_only_new_entry_entity_add_tasks() -> None:
    """Platform adds are tracked on the config entry; await those, nothing else."""
    entry_tasks: set[asyncio.Task[Any]] = set()
    hub = _hub_for_start(entry_tasks=entry_tasks)

    entity_add_done = asyncio.Event()

    async def entity_add() -> None:
        await asyncio.sleep(0)
        entity_add_done.set()

    async def platform_callback() -> None:
        task = asyncio.create_task(entity_add())
        entry_tasks.add(task)
        task.add_done_callback(entry_tasks.discard)

    unrelated = asyncio.create_task(asyncio.Event().wait())
    try:
        hub._discovery_callbacks = [platform_callback]
        await asyncio.wait_for(hub._notify_discovery_complete(), timeout=1.0)
        assert entity_add_done.is_set()
        assert unrelated.done() is False
    finally:
        unrelated.cancel()
        with pytest.raises(asyncio.CancelledError):
            await unrelated


@pytest.mark.asyncio
async def test_notify_is_idempotent() -> None:
    hub = _hub_for_start()
    calls = 0

    async def platform_callback() -> None:
        nonlocal calls
        calls += 1

    hub._discovery_callbacks = [platform_callback]
    await hub._notify_discovery_complete()
    await hub._notify_discovery_complete()
    assert calls == 1


@pytest.mark.asyncio
async def test_notify_times_out_stuck_entity_add() -> None:
    entry_tasks: set[asyncio.Task[Any]] = set()
    hub = _hub_for_start(entry_tasks=entry_tasks)

    async def stuck_add() -> None:
        await asyncio.Event().wait()

    async def platform_callback() -> None:
        task = asyncio.create_task(stuck_add())
        entry_tasks.add(task)
        task.add_done_callback(entry_tasks.discard)

    hub._discovery_callbacks = [platform_callback]
    with (
        patch("custom_components.zencontrol_tpi.hub._ENTITY_ADD_TIMEOUT", 0.05),
        pytest.raises(ConfigEntryNotReady, match="Timed out"),
    ):
        await hub._notify_discovery_complete()


@pytest.mark.asyncio
async def test_setup_failure_notify_does_not_mask_original_error() -> None:
    hub = _hub_for_start()
    hub._wait_for_controller = AsyncMock(
        side_effect=ConfigEntryNotReady("controller down")
    )

    async def bad_callback() -> None:
        raise RuntimeError("platform boom")

    hub._discovery_callbacks = [bad_callback]

    with pytest.raises(ConfigEntryNotReady, match="controller down"):
        await hub.async_start()
