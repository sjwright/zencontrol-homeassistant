"""Simulator-backed setup → unload boundary test."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zencontrol_tpi.const import (
    CONFIG_VERSION,
    DOMAIN,
    entry_data_for_controller,
    normalize_mac,
    normalize_mac_id,
)
from custom_components.zencontrol_tpi.runtime import DATA_RUNTIME
from tests.conftest import LiveSimulator

pytestmark = [
    pytest.mark.simulator,
    pytest.mark.enable_socket,  # UDP bind to zencontrol-simulator
    pytest.mark.usefixtures("enable_custom_integrations"),
]


@pytest.mark.asyncio
async def test_setup_unload_against_simulator(
    hass: HomeAssistant,
    live_sim: LiveSimulator,
) -> None:
    """Load the integration against a live simulator, then unload cleanly.

    Exercises the real HA config-entry / platform / registry path with a real
    zencontrol-python client pointed at zencontrol-simulator.
    """
    mac = normalize_mac(live_sim.mac)
    mac_id = normalize_mac_id(mac)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=mac_id,
        title=live_sim.label,
        data=entry_data_for_controller(
            {
                "host": "127.0.0.1",
                "port": live_sim.port,
                "mac": mac,
                "name": "sim",
                "label": live_sim.label,
            },
            unicast=True,
        ),
        version=CONFIG_VERSION,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data.get(DOMAIN, {}).get(DATA_RUNTIME) is not None

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    status_entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "sim_controller_status"
    )
    assert status_entity_id is not None
    status_state = hass.states.get(status_entity_id)
    assert status_state is not None
    assert status_state.state == "online"

    entry_entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entry_entities) > 1  # status + discovered bus entities

    light_entities = [e for e in entry_entities if e.domain == "light"]
    assert light_entities, "expected discovered light entities from simulator world"

    device_entries = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert any((DOMAIN, mac) in device_entry.identifiers for device_entry in device_entries)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DATA_RUNTIME not in hass.data.get(DOMAIN, {})
    # HA may restore a unavailable placeholder; the live entity must be gone.
    status_after = hass.states.get(status_entity_id)
    assert status_after is None or status_after.state == "unavailable"
