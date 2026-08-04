"""The zencontrol-tpi integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_UNICAST,
    CONFIG_VERSION,
    DOMAIN,
    PLATFORMS,
    controllers_from_entry_data,
    entry_data_for_controller,
    migrate_entry_data_to_v3,
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
from .runtime import SharedZenRuntime

_LOGGER = logging.getLogger(__name__)

__all__ = ["ZencontrolTpiConfigEntry", "entry_data_for_controller"]


async def async_setup_entry(hass: HomeAssistant, entry: ZencontrolTpiConfigEntry) -> bool:
    """Set up zencontrol-tpi from a config entry."""
    force_full_discovery = pop_force_full_discovery(entry.entry_id)

    runtime = SharedZenRuntime.async_get_or_create(hass)
    hub = ZenHub(hass, entry, runtime, force_full_discovery=force_full_discovery)
    entry.runtime_data = hub

    platforms_forwarded = False
    try:
        await hub.async_setup()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_forwarded = True
        await hub.async_start()
    except BaseException:
        # Cleanup on any failure (including CancelledError), then re-raise as-is.
        # Retryable errors are already ConfigEntryNotReady from the hub; unexpected
        # exceptions must surface rather than being wrapped into a retry loop.
        await hub.async_stop()
        if platforms_forwarded:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZencontrolTpiConfigEntry) -> bool:
    """Unload a zencontrol-tpi config entry."""
    if not hass.is_stopping:
        mark_force_full_discovery(entry.entry_id)

    # Unload entities while the hub is still valid; only detach the shared
    # runtime after platforms succeed (HA recommended unload order).
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete persisted discovery manifest when the config entry is removed."""
    await DiscoveryManifestStore(hass, entry.entry_id).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to one-controller-per-entry with per-controller flags."""
    if entry.version >= CONFIG_VERSION:
        return True

    _LOGGER.info(
        "Migrating zencontrol entry %s from version %s to %s",
        entry.entry_id,
        entry.version,
        CONFIG_VERSION,
    )

    version = entry.version
    data: dict[str, Any] = dict(entry.data)
    title = entry.title
    unique_id = entry.unique_id

    if version < 2:
        controllers = controllers_from_entry_data(data)
        unicast = bool(data.get(CONF_UNICAST, False))

        if not controllers:
            data = {**migrate_entry_data_to_v3(data)}
            version = 3
        else:
            primary = dict(controllers[0])
            extras = controllers[1:]
            primary_mac = normalize_mac_id(str(primary.get(CONF_MAC, "")))
            title = str(primary.get(CONF_LABEL) or primary.get(CONF_NAME) or "zencontrol")
            unique_id = primary_mac or unique_id
            data = {
                **migrate_entry_data_to_v3(
                    {CONF_CONTROLLERS: [primary], CONF_UNICAST: unicast}
                )
            }

            for ctrl_cfg in extras:
                mac_id = normalize_mac_id(str(ctrl_cfg.get(CONF_MAC, "")))
                if not mac_id:
                    _LOGGER.warning("Skipping migration of controller without MAC: %s", ctrl_cfg)
                    continue
                if mac_is_configured(hass, mac_id, ignore_entry_id=entry.entry_id):
                    _LOGGER.info("Controller %s already has an entry; skipping import", mac_id)
                    continue

                imported = migrate_entry_data_to_v3(
                    {CONF_CONTROLLERS: [dict(ctrl_cfg)], CONF_UNICAST: unicast}
                )
                await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_IMPORT},
                    data={
                        **imported,
                        "title": str(
                            ctrl_cfg.get(CONF_LABEL) or ctrl_cfg.get(CONF_NAME) or "zencontrol"
                        ),
                        "migrate_from_entry_id": entry.entry_id,
                    },
                )

            _LOGGER.debug(
                "Migration kept primary controller on entry %s; spawned %d import flows",
                entry.entry_id,
                len(extras),
            )
            version = 3

    if version < 3:
        data = {**migrate_entry_data_to_v3(data)}
        version = 3

    hass.config_entries.async_update_entry(
        entry,
        title=title,
        unique_id=unique_id,
        data=data,
        version=CONFIG_VERSION,
    )
    return True
