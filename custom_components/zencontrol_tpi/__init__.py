"""The zencontrol-tpi integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import PLATFORMS
from .hub import (
    ZencontrolTpiConfigEntry,
    ZenHub,
    mark_force_full_discovery,
    pop_force_full_discovery,
)
from .manifest_store import DiscoveryManifestStore

__all__ = ["ZencontrolTpiConfigEntry"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ZencontrolTpiConfigEntry
) -> bool:
    """Set up zencontrol-tpi from a config entry."""
    force_full_discovery = pop_force_full_discovery(entry.entry_id)

    hub = ZenHub(hass, entry, force_full_discovery=force_full_discovery)
    entry.runtime_data = hub

    await hub.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        await hub.async_start()
    except Exception as err:
        # Clean up so a subsequent HA retry starts fresh: unload the platforms
        # that were forwarded above.
        await hub.async_stop()
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if isinstance(err, ConfigEntryNotReady):
            raise
        raise ConfigEntryNotReady(f"zencontrol setup failed: {err}") from err

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ZencontrolTpiConfigEntry
) -> bool:
    """Unload a zencontrol-tpi config entry."""
    if not hass.is_stopping:
        mark_force_full_discovery(entry.entry_id)

    hub = entry.runtime_data
    await hub.async_stop()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: ZencontrolTpiConfigEntry
) -> None:
    """Delete persisted discovery manifest when the config entry is removed."""
    await DiscoveryManifestStore(hass, entry.entry_id).async_remove()
