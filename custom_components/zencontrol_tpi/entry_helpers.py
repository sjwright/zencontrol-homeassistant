"""Helpers for working with zencontrol config entries."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_MAC, DOMAIN, controllers_from_entry_data, normalize_mac, normalize_mac_id


def config_entry_for_mac(
    hass: HomeAssistant,
    mac: str,
    *,
    ignore_entry_id: str | None = None,
) -> ConfigEntry | None:
    """Find the persisted owner of a controller, including legacy entries."""
    mac_id = normalize_mac_id(mac)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == ignore_entry_id:
            continue
        if entry.unique_id and normalize_mac_id(entry.unique_id) == mac_id:
            return entry
        for controller in controllers_from_entry_data(entry.data):
            if normalize_mac_id(str(controller.get(CONF_MAC, ""))) == mac_id:
                return entry
    return None


def mac_is_configured(hass: HomeAssistant, mac: str, *, ignore_entry_id: str | None = None) -> bool:
    """Return whether a controller MAC belongs to another persisted entry."""
    return config_entry_for_mac(hass, mac, ignore_entry_id=ignore_entry_id) is not None


def async_relink_migrated_devices(
    hass: HomeAssistant,
    *,
    old_entry_id: str,
    new_entry_id: str,
    mac: str,
) -> None:
    """Move devices for this MAC (and its sub-devices) from old entry to new."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    mac_norm = normalize_mac(mac)
    mac_id = normalize_mac_id(mac)
    sub_prefix = f"{mac_norm}:sub:"
    for device_entry in list(dr.async_entries_for_config_entry(device_registry, old_entry_id)):
        domain_identifiers = [ident for ident in device_entry.identifiers if ident[0] == DOMAIN]
        if not domain_identifiers:
            continue
        if not any(ident == mac_norm or ident == mac_id or ident.startswith(sub_prefix) for _, ident in domain_identifiers):
            continue
        # Entity ownership is separate from device ownership in HA. Move both
        # without recreating records, preserving names, areas and disabled flags.
        for entity in er.async_entries_for_device(entity_registry, device_entry.id, include_disabled_entities=True):
            if entity.config_entry_id == old_entry_id:
                entity_registry.async_update_entity(entity.entity_id, config_entry_id=new_entry_id)
        device_registry.async_update_device(
            device_entry.id,
            add_config_entry_id=new_entry_id,
            remove_config_entry_id=old_entry_id,
        )
