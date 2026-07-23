"""Tests for SharedZenRuntime attach/detach lifetime."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.zencontrol_tpi.const import (
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    DOMAIN,
)
from custom_components.zencontrol_tpi.runtime import (
    DATA_RUNTIME,
    SharedZenRuntime,
)


def _ctrl_cfg(**overrides: Any) -> dict[str, Any]:
    data = {
        "host": "10.0.0.1",
        "port": 5108,
        CONF_MAC: "AA:BB:CC:DD:EE:01",
        CONF_NAME: "10001",
        CONF_LABEL: "House",
    }
    data.update(overrides)
    return data


def _hub(entry_id: str = "entry-1") -> MagicMock:
    hub = MagicMock()
    hub.entry = SimpleNamespace(entry_id=entry_id)
    hub.controller = None
    hub.controllers = []
    hub.handle_listener_connect = AsyncMock()
    hub.handle_listener_disconnect = MagicMock()
    return hub


@pytest.mark.asyncio
async def test_runtime_attach_detach_closes_when_empty() -> None:
    """Last detach closes the client and clears hass.data."""
    hass = MagicMock()
    hass.data = {}

    fake_ctrl = SimpleNamespace(
        name="10001",
        label="House",
        mac="AA:BB:CC:DD:EE:01",
        filtering=False,
    )
    fake_zen = MagicMock()
    fake_zen.add_controller.return_value = fake_ctrl
    fake_zen.remove_controller = AsyncMock()
    fake_zen.configure_controller_events = AsyncMock()
    fake_zen.start = AsyncMock()
    fake_zen.aclose = AsyncMock()
    fake_zen.discovered_controllers = []

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass, unicast=False)
        hub = _hub()
        ctrl = await runtime.async_attach(hub, _ctrl_cfg())
        assert ctrl is fake_ctrl
        assert hass.data[DOMAIN][DATA_RUNTIME] is runtime

        await runtime.async_ensure_started()
        fake_zen.start.assert_awaited_once()

        await runtime.async_detach("entry-1")
        fake_zen.remove_controller.assert_awaited()
        fake_zen.aclose.assert_awaited()
        assert DATA_RUNTIME not in hass.data.get(DOMAIN, {})


@pytest.mark.asyncio
async def test_runtime_second_attach_keeps_client() -> None:
    """Detaching one of two entries leaves the runtime running."""
    hass = MagicMock()
    hass.data = {}

    ctrls = {
        "10001": SimpleNamespace(
            name="10001", label="A", mac="AA:BB:CC:DD:EE:01", filtering=False
        ),
        "10002": SimpleNamespace(
            name="10002", label="B", mac="AA:BB:CC:DD:EE:02", filtering=False
        ),
    }

    def add_controller(**kwargs: Any) -> Any:
        return ctrls[kwargs["name"]]

    fake_zen = MagicMock()
    fake_zen.add_controller.side_effect = add_controller
    fake_zen.remove_controller = AsyncMock()
    fake_zen.configure_controller_events = AsyncMock()
    fake_zen.start = AsyncMock()
    fake_zen.aclose = AsyncMock()
    fake_zen.discovered_controllers = []

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        await runtime.async_attach(
            _hub("e1"),
            _ctrl_cfg(name="10001", mac="AA:BB:CC:DD:EE:01"),
        )
        await runtime.async_ensure_started()
        await runtime.async_attach(
            _hub("e2"),
            _ctrl_cfg(
                host="10.0.0.2",
                name="10002",
                mac="AA:BB:CC:DD:EE:02",
                label="B",
            ),
        )
        fake_zen.configure_controller_events.assert_awaited()

        await runtime.async_detach("e1")
        fake_zen.aclose.assert_not_awaited()
        assert hass.data[DOMAIN][DATA_RUNTIME] is runtime

        await runtime.async_detach("e2")
        fake_zen.aclose.assert_awaited()
