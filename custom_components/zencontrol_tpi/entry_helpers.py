"""Helpers for working with zencontrol config entries."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import CONF_MAC, DOMAIN, controllers_from_entry_data, normalize_mac_id


def mac_is_configured(
    hass: HomeAssistant,
    mac: str,
    *,
    ignore_entry_id: str | None = None,
) -> bool:
    """Return whether a controller MAC belongs to another persisted entry."""
    mac_id = normalize_mac_id(mac)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == ignore_entry_id:
            continue
        if entry.unique_id and normalize_mac_id(entry.unique_id) == mac_id:
            return True
        for controller in controllers_from_entry_data(entry.data):
            if normalize_mac_id(str(controller.get(CONF_MAC, ""))) == mac_id:
                return True
    return False
