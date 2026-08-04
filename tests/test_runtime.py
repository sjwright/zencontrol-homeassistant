"""Tests for SharedZenRuntime attach/detach lifetime."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from homeassistant.core import HomeAssistant
from zencontrol import DiscoveredController

from custom_components.zencontrol_tpi.const import (
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    DOMAIN,
    ControllerConfig,
)
from custom_components.zencontrol_tpi.hub import ZenHub
from custom_components.zencontrol_tpi.runtime import (
    DATA_RUNTIME,
    SharedZenRuntime,
)
from tests.fakes import FakeHub, FakeZenControl


def _ctrl_cfg(**overrides: Any) -> ControllerConfig:
    data: ControllerConfig = {
        "host": "10.0.0.1",
        "port": 5108,
        CONF_MAC: "AA:BB:CC:DD:EE:01",
        CONF_NAME: "10001",
        CONF_LABEL: "House",
    }
    data.update(overrides)  # type: ignore[typeddict-item]
    return data


def _hass() -> MagicMock:
    hass = create_autospec(HomeAssistant, instance=True)
    hass.data = {}
    return hass


@pytest.mark.asyncio
async def test_runtime_attach_detach_closes_when_empty() -> None:
    """Last detach closes the client and clears hass.data."""
    hass = _hass()
    fake_zen = FakeZenControl()

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        hub = cast(ZenHub, FakeHub())
        ctrl = await runtime.async_attach(hub, _ctrl_cfg())
        assert ctrl is fake_zen.controllers[0]
        assert fake_zen.add_controller_calls[0]["unicast"] is False
        assert fake_zen.add_controller_calls[0]["tcp"] is False
        assert hass.data[DOMAIN][DATA_RUNTIME] is runtime

        await runtime.async_ensure_started()
        fake_zen.start.assert_awaited_once()

        await runtime.async_detach("entry-1")
        fake_zen.remove_controller.assert_awaited()
        fake_zen.aclose.assert_awaited()
        assert DATA_RUNTIME not in hass.data.get(DOMAIN, {})


@pytest.mark.asyncio
async def test_runtime_discovery_enriches_label_before_flow() -> None:
    """Runtime discovery probes QUERY_CONTROLLER_LABEL before starting the flow."""
    hass = _hass()
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    flow_init = AsyncMock()
    hass.config_entries.flow.async_init = flow_init

    enriched = DiscoveredController(
        host="10.0.0.9",
        mac="AA:BB:CC:DD:EE:09",
        label="Annex",
        port=5108,
    )
    fake_zen = FakeZenControl()
    fake_zen.enrich_discovered = AsyncMock(return_value=enriched)

    discovered = DiscoveredController(
        host="10.0.0.9",
        mac="AA:BB:CC:DD:EE:09",
        label=None,
        port=5108,
    )

    with (
        patch(
            "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
            return_value=fake_zen,
        ),
        patch(
            "custom_components.zencontrol_tpi.runtime.mac_is_configured",
            return_value=False,
        ),
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        await runtime._on_controller_discovered(discovered)
        await asyncio.sleep(0)

    fake_zen.enrich_discovered.assert_awaited_once_with(discovered)
    flow_init.assert_awaited_once()
    assert flow_init.await_args is not None
    assert flow_init.await_args.kwargs["data"]["label"] == "Annex"


@pytest.mark.asyncio
async def test_runtime_resync_refreshes_hubs() -> None:
    """Session-gap on_resync refreshes hubs without marking the listener down."""
    hass = _hass()
    fake_zen = FakeZenControl()

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        hub = cast(ZenHub, FakeHub())
        await runtime.async_attach(hub, _ctrl_cfg())
        runtime._listener_up = True

        await runtime._on_resync()

        assert runtime.listener_up is True
        cast(FakeHub, hub).handle_listener_resync.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_second_attach_keeps_client() -> None:
    """Detaching one of two entries leaves the runtime running."""
    hass = _hass()
    fake_zen = FakeZenControl()

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        await runtime.async_attach(
            cast(ZenHub, FakeHub("e1")),
            _ctrl_cfg(name="10001", mac="AA:BB:CC:DD:EE:01"),
        )
        await runtime.async_ensure_started()
        ctrl_b = await runtime.async_attach(
            cast(ZenHub, FakeHub("e2")),
            _ctrl_cfg(
                host="10.0.0.2",
                name="10002",
                mac="AA:BB:CC:DD:EE:02",
                label="B",
            ),
        )
        # Attach must not enable events before the hub has waited for ready.
        fake_zen.configure_controller_events.assert_not_awaited()
        await runtime.async_configure_controller_events(ctrl_b)  # type: ignore[arg-type]
        fake_zen.configure_controller_events.assert_awaited_once_with(ctrl_b)

        await runtime.async_detach("e1")
        fake_zen.aclose.assert_not_awaited()
        assert hass.data[DOMAIN][DATA_RUNTIME] is runtime

        await runtime.async_detach("e2")
        fake_zen.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_runtime_passes_per_controller_unicast_and_tcp() -> None:
    """Each attached controller gets its own unicast/tcp flags."""
    hass = _hass()
    fake_zen = FakeZenControl()

    with patch(
        "custom_components.zencontrol_tpi.runtime.zencontrol.ZenControl",
        return_value=fake_zen,
    ):
        runtime = SharedZenRuntime.async_get_or_create(hass)
        await runtime.async_attach(
            cast(ZenHub, FakeHub("e1")),
            _ctrl_cfg(name="10001", mac="AA:BB:CC:DD:EE:01", unicast=True, tcp=False),
        )
        await runtime.async_attach(
            cast(ZenHub, FakeHub("e2")),
            _ctrl_cfg(
                host="10.0.0.2",
                name="10002",
                mac="AA:BB:CC:DD:EE:02",
                label="B",
                unicast=False,
                tcp=True,
            ),
        )

    assert fake_zen.add_controller_calls[0]["unicast"] is True
    assert fake_zen.add_controller_calls[0]["tcp"] is False
    assert fake_zen.add_controller_calls[1]["unicast"] is False
    assert fake_zen.add_controller_calls[1]["tcp"] is True
