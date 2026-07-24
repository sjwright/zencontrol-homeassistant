"""Tests that setup waits for is_controller_ready() before discovery/events."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.zencontrol_tpi.const import (
    CONTROLLER_STATUS_ONLINE,
    CONTROLLER_STATUS_STARTING,
    CONTROLLER_STATUS_UNREACHABLE,
)
from custom_components.zencontrol_tpi.hub import ZenHub


def _hub_with_controller(ready_sequence: list[bool | None]) -> ZenHub:
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry._tasks = set()

    with patch.object(ZenHub, "__init__", lambda self, *a, **k: None):
        hub = ZenHub.__new__(ZenHub)

    hub.hass = hass
    hub.entry = entry
    hub.runtime = MagicMock()
    hub.runtime.started = False
    hub.runtime.listener_up = True
    hub.runtime.async_ensure_started = AsyncMock()
    hub.runtime.async_configure_controller_events = AsyncMock()
    hub._stopping = False
    hub._controller_status = CONTROLLER_STATUS_UNREACHABLE
    hub._status_entity = None
    hub._discovery_complete = False
    hub._discovery_notified = False
    hub._setup_complete = False
    hub._discovery_callbacks = []
    hub._light_entities = {}
    hub._group_entities = {}
    hub._button_entities = {}
    hub._motion_sensor_entities = {}
    hub._absolute_input_entities = {}
    hub._sv_sensor_entities = {}
    hub._sv_switch_entities = {}
    hub._profile_entities = {}
    hub._scene_entities = {}
    hub.sync_device_assignments = MagicMock()
    hub._discover_entities = AsyncMock()
    hub._refresh_light_states = AsyncMock()
    hub._notify_discovery_complete = AsyncMock()
    hub._async_notify_discovery_best_effort = AsyncMock()

    calls = {"n": 0}

    async def is_ready() -> bool | None:
        i = min(calls["n"], len(ready_sequence) - 1)
        calls["n"] += 1
        return ready_sequence[i]

    ctrl = MagicMock()
    ctrl.label = "House"
    ctrl.host = "10.0.0.1"
    ctrl.version = "1.0"
    ctrl.is_controller_ready = AsyncMock(side_effect=is_ready)
    ctrl.interview = AsyncMock()
    hub.controller = ctrl
    hub.controllers = [ctrl]
    return hub


@pytest.mark.asyncio
async def test_wait_polls_until_ready() -> None:
    hub = _hub_with_controller([False, False, True])
    with patch(
        "custom_components.zencontrol_tpi.hub.CONTROLLER_READY_POLL_INTERVAL", 0
    ):
        await hub._wait_for_controller()
    assert hub.controller.is_controller_ready.await_count == 3
    hub.controller.interview.assert_awaited_once()
    # Online is deferred until async_start finishes listener/event setup.
    assert hub.controller_status == CONTROLLER_STATUS_STARTING
    assert hub.available is False


@pytest.mark.asyncio
async def test_wait_marks_starting_then_unreachable() -> None:
    hub = _hub_with_controller([None])
    with pytest.raises(ConfigEntryNotReady, match="Cannot reach"):
        await hub._wait_for_controller()
    assert hub.controller_status == CONTROLLER_STATUS_UNREACHABLE
    assert hub.available is False
    hub.controller.interview.assert_not_awaited()


@pytest.mark.asyncio
async def test_entities_unavailable_while_starting() -> None:
    hub = _hub_with_controller([False, True])
    with patch(
        "custom_components.zencontrol_tpi.hub.CONTROLLER_READY_POLL_INTERVAL", 0
    ):
        # Drive one not-ready poll by interrupting after status flips.
        async def ready_then_stop() -> bool | None:
            hub.set_controller_status(CONTROLLER_STATUS_STARTING)
            assert hub.available is False
            assert hub.is_controller_available(hub.controller) is False
            return True

        hub.controller.is_controller_ready = AsyncMock(side_effect=ready_then_stop)
        await hub._wait_for_controller()
    assert hub.controller_status == CONTROLLER_STATUS_STARTING
    assert hub.available is False


@pytest.mark.asyncio
async def test_async_start_enables_events_only_after_ready_when_runtime_up() -> None:
    hub = _hub_with_controller([True])
    hub.runtime.started = True
    hub._wait_for_controller = AsyncMock()

    await hub.async_start()

    hub._wait_for_controller.assert_awaited_once()
    hub.runtime.async_ensure_started.assert_awaited_once()
    hub.runtime.async_configure_controller_events.assert_awaited_once_with(
        hub.controller
    )


@pytest.mark.asyncio
async def test_async_start_skips_late_configure_on_first_start() -> None:
    hub = _hub_with_controller([True])
    hub.runtime.started = False
    hub._wait_for_controller = AsyncMock()

    await hub.async_start()

    hub.runtime.async_ensure_started.assert_awaited_once()
    hub.runtime.async_configure_controller_events.assert_not_awaited()
