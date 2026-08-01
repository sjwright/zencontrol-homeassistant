"""The zencontrol-tpi integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_UNICAST,
    CONFIG_VERSION,
    DOMAIN,
    PLATFORMS,
    normalize_mac_id,
)
from .entry_helpers import mac_is_configured
from .hub import (
    ZencontrolTpiConfigEntry,
    ZenHub,
    mark_force_full_discovery,
    pop_force_full_discovery,
)
from .manifest_store import DiscoveryManifestStore
from .runtime import SharedZenRuntime, entry_unicast

_LOGGER = logging.getLogger(__name__)

__all__ = ["ZencontrolTpiConfigEntry"]


async def async_setup_entry(hass: HomeAssistant, entry: ZencontrolTpiConfigEntry) -> bool:
    """Set up zencontrol-tpi from a config entry."""
    force_full_discovery = pop_force_full_discovery(entry.entry_id)

    runtime = SharedZenRuntime.async_get_or_create(hass, unicast=entry_unicast(entry.data))
    hub = ZenHub(hass, entry, runtime, force_full_discovery=force_full_discovery)
    entry.runtime_data = hub

    platforms_forwarded = False
    try:
        await hub.async_setup()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_forwarded = True
        await hub.async_start()
    except asyncio.CancelledError:
        await hub.async_stop()
        if platforms_forwarded:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise
    except Exception as err:
        await hub.async_stop()
        if platforms_forwarded:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if isinstance(err, ConfigEntryNotReady):
            raise
        raise ConfigEntryNotReady(f"zencontrol setup failed: {err}") from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZencontrolTpiConfigEntry) -> bool:
    """Unload a zencontrol-tpi config entry."""
    if not hass.is_stopping:
        mark_force_full_discovery(entry.entry_id)

    hub = entry.runtime_data
    await hub.async_stop()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ZencontrolTpiConfigEntry) -> None:
    """Delete persisted discovery manifest when the config entry is removed."""
    await DiscoveryManifestStore(hass, entry.entry_id).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to one-controller-per-entry."""
    if entry.version >= CONFIG_VERSION:
        return True

    _LOGGER.info(
        "Migrating zencontrol entry %s from version %s to %s",
        entry.entry_id,
        entry.version,
        CONFIG_VERSION,
    )

    controllers = list(entry.data.get(CONF_CONTROLLERS, []))
    unicast = bool(entry.data.get(CONF_UNICAST, False))

    if not controllers:
        hass.config_entries.async_update_entry(entry, version=CONFIG_VERSION)
        return True

    primary = controllers[0]
    extras = controllers[1:]

    primary_mac = normalize_mac_id(str(primary.get(CONF_MAC, "")))
    title = str(primary.get(CONF_LABEL) or primary.get("name") or "zencontrol")

    hass.config_entries.async_update_entry(
        entry,
        title=title,
        unique_id=primary_mac or entry.unique_id,
        data={
            CONF_CONTROLLERS: [primary],
            CONF_UNICAST: unicast,
        },
        version=CONFIG_VERSION,
    )

    for ctrl_cfg in extras:
        mac_id = normalize_mac_id(str(ctrl_cfg.get(CONF_MAC, "")))
        if not mac_id:
            _LOGGER.warning("Skipping migration of controller without MAC: %s", ctrl_cfg)
            continue
        if mac_is_configured(hass, mac_id, ignore_entry_id=entry.entry_id):
            _LOGGER.info("Controller %s already has an entry; skipping import", mac_id)
            continue

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_CONTROLLERS: [ctrl_cfg],
                CONF_UNICAST: unicast,
                "title": str(ctrl_cfg.get(CONF_LABEL) or ctrl_cfg.get("name") or "zencontrol"),
                "migrate_from_entry_id": entry.entry_id,
            },
        )

    _LOGGER.debug(
        "Migration kept primary controller on entry %s; spawned %d import flows",
        entry.entry_id,
        len(extras),
    )
    return True


def entry_data_for_controller(ctrl_cfg: dict[str, Any], *, unicast: bool = False) -> dict[str, Any]:
    """Build persisted entry data for a single controller."""
    return {
        CONF_CONTROLLERS: [ctrl_cfg],
        CONF_UNICAST: unicast,
    }
