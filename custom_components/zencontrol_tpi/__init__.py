"""The zencontrol-tpi integration."""

from __future__ import annotations

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
    controller_from_entry_data,
    normalize_mac_id,
)
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ZencontrolTpiConfigEntry
) -> bool:
    """Set up zencontrol-tpi from a config entry."""
    force_full_discovery = pop_force_full_discovery(entry.entry_id)

    runtime = SharedZenRuntime.async_get_or_create(
        hass, unicast=entry_unicast(entry.data)
    )
    hub = ZenHub(
        hass, entry, runtime, force_full_discovery=force_full_discovery
    )
    entry.runtime_data = hub

    platforms_forwarded = False
    try:
        await hub.async_setup()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_forwarded = True
        await hub.async_start()
    except Exception as err:
        await hub.async_stop()
        if platforms_forwarded:
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


def _mac_configured(hass: HomeAssistant, mac_id: str, *, skip_entry_id: str) -> bool:
    """Return True if any other entry already has this controller MAC in data."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == skip_entry_id:
            continue
        ctrl = controller_from_entry_data(entry.data)
        if ctrl and normalize_mac_id(str(ctrl.get(CONF_MAC, ""))) == mac_id:
            return True
        if entry.unique_id == mac_id:
            return True
    return False


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

    for ctrl in extras:
        mac_id = normalize_mac_id(str(ctrl.get(CONF_MAC, "")))
        if not mac_id:
            _LOGGER.warning(
                "Skipping migration of controller without MAC: %s", ctrl
            )
            continue
        if _mac_configured(hass, mac_id, skip_entry_id=entry.entry_id):
            _LOGGER.info(
                "Controller %s already has an entry; skipping import", mac_id
            )
            continue

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_CONTROLLERS: [ctrl],
                CONF_UNICAST: unicast,
                "title": str(
                    ctrl.get(CONF_LABEL) or ctrl.get("name") or "zencontrol"
                ),
                "migrate_from_entry_id": entry.entry_id,
            },
        )

    _LOGGER.debug(
        "Migration kept primary controller on entry %s; spawned %d import flows",
        entry.entry_id,
        len(extras),
    )
    return True


def entry_data_for_controller(
    ctrl: dict[str, Any], *, unicast: bool = False
) -> dict[str, Any]:
    """Build persisted entry data for a single controller."""
    return {
        CONF_CONTROLLERS: [ctrl],
        CONF_UNICAST: unicast,
    }
