"""Tests for config-entry helper functions."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zencontrol_tpi.const import CONF_CONTROLLERS, CONF_MAC, DOMAIN
from custom_components.zencontrol_tpi.entry_helpers import mac_is_configured


async def test_mac_is_configured_checks_entry_data_and_unique_id(
    hass: HomeAssistant,
) -> None:
    """Configured MACs are found in legacy data and entry unique IDs."""
    data_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CONTROLLERS: [
                {CONF_MAC: "AA:BB:CC:DD:EE:01"},
                {CONF_MAC: "AA:BB:CC:DD:EE:02"},
            ]
        },
    )
    unique_id_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AABBCCDDEE03",
    )
    data_entry.add_to_hass(hass)
    unique_id_entry.add_to_hass(hass)

    assert mac_is_configured(hass, "aa-bb-cc-dd-ee-01")
    assert mac_is_configured(hass, "AA:BB:CC:DD:EE:02")
    assert mac_is_configured(hass, "AA:BB:CC:DD:EE:03")
    assert not mac_is_configured(hass, "AA:BB:CC:DD:EE:04")
    assert not mac_is_configured(
        hass,
        "AA:BB:CC:DD:EE:01",
        ignore_entry_id=data_entry.entry_id,
    )
