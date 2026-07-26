"""Domain-owned ZenControl client shared by all config entries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import zencontrol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from zencontrol import (
    DiscoveredController,
    ZenAbsoluteInput,
    ZenButton,
    ZenColour,
    ZenController,
    ZenGroup,
    ZenLight,
    ZenMotionSensor,
    ZenProfile,
    ZenSystemVariable,
)

from .const import (
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_UNICAST,
    DEFAULT_PORT,
    DOMAIN,
    normalize_mac,
    normalize_mac_id,
)
from .entry_helpers import mac_is_configured

if TYPE_CHECKING:
    from .hub import ZenHub

_LOGGER = logging.getLogger(__name__)

DATA_RUNTIME = "runtime"


def async_get_runtime(hass: HomeAssistant) -> SharedZenRuntime | None:
    """Return the shared runtime if it exists."""
    return hass.data.get(DOMAIN, {}).get(DATA_RUNTIME)


class SharedZenRuntime:
    """One ZenControl / event listener for every loaded config entry.

    Entries attach and detach by MAC. The client is started when the first
    attached hub finishes setup, and closed when the last entry detaches.
    """

    def __init__(self, hass: HomeAssistant, *, unicast: bool = False) -> None:
        self.hass = hass
        self.unicast = unicast
        self.zen = zencontrol.ZenControl(logger=_LOGGER, unicast=unicast)
        self._hubs_by_entry: dict[str, ZenHub] = {}
        self._hubs_by_mac: dict[str, ZenHub] = {}
        self._controllers_by_entry: dict[str, ZenController] = {}
        self._next_controller_id = 1
        self._started = False
        self._listener_up = False
        self._stopping = False
        self._start_lock = asyncio.Lock()
        self._attach_lock = asyncio.Lock()

        self.zen.callbacks.on_connect = self._on_connect
        self.zen.callbacks.on_disconnect = self._on_disconnect
        self.zen.callbacks.on_resync = self._on_resync
        self.zen.callbacks.light_change = self._on_light_change
        self.zen.callbacks.group_change = self._on_group_change
        self.zen.callbacks.button_press = self._on_button_press
        self.zen.callbacks.button_long_press = self._on_button_long_press
        self.zen.callbacks.motion_event = self._on_motion_event
        self.zen.callbacks.absolute_input_change = self._on_absolute_input_change
        self.zen.callbacks.system_variable_change = self._on_sv_change
        self.zen.callbacks.profile_change = self._on_profile_change
        self.zen.callbacks.controller_discovered = self._on_controller_discovered
        self.zen.callbacks.controller_status_change = self._on_controller_status

    @property
    def listener_up(self) -> bool:
        """Return True when the shared event listener is connected."""
        return self._listener_up

    @property
    def started(self) -> bool:
        """Return True when the shared ZenControl client has been started."""
        return self._started

    @classmethod
    def async_get_or_create(cls, hass: HomeAssistant, *, unicast: bool = False) -> SharedZenRuntime:
        """Return the domain runtime, creating it if needed."""
        domain_data = hass.data.setdefault(DOMAIN, {})
        runtime = domain_data.get(DATA_RUNTIME)
        if runtime is None:
            runtime = cls(hass, unicast=unicast)
            domain_data[DATA_RUNTIME] = runtime
            _LOGGER.debug("Created shared zencontrol runtime (unicast=%s)", unicast)
        elif bool(runtime.unicast) != bool(unicast):
            _LOGGER.debug(
                "Shared runtime already running with unicast=%s; ignoring entry preference unicast=%s",
                runtime.unicast,
                unicast,
            )
        return runtime

    def hub_for_controller(self, zen_ctrl: ZenController) -> ZenHub | None:
        """Return the hub that owns this controller, if attached."""
        if zen_ctrl.mac:
            return self._hubs_by_mac.get(normalize_mac_id(str(zen_ctrl.mac)))
        for hub in self._hubs_by_entry.values():
            if hub.controller is not None and hub.controller.name == zen_ctrl.name:
                return hub
        return None

    async def async_attach(
        self,
        hub: ZenHub,
        ctrl_cfg: dict[str, Any],
    ) -> ZenController:
        """Register a controller for this hub and return the ZenController."""
        async with self._attach_lock:
            entry_id = hub.entry.entry_id
            if entry_id in self._hubs_by_entry:
                raise HomeAssistantError(f"Config entry {entry_id} is already attached to the runtime")

            mac = normalize_mac(str(ctrl_cfg[CONF_MAC]))
            mac_id = normalize_mac_id(mac)
            if mac_id in self._hubs_by_mac:
                raise HomeAssistantError(f"Controller {mac} is already attached by another entry")

            controller_id = self._next_controller_id
            self._next_controller_id += 1
            ctrl = self.zen.add_controller(
                id=controller_id,
                name=ctrl_cfg[CONF_NAME],
                label=ctrl_cfg[CONF_LABEL],
                host=ctrl_cfg["host"],
                port=int(ctrl_cfg.get("port", DEFAULT_PORT)),
                mac=mac,
            )

            self._hubs_by_entry[entry_id] = hub
            self._hubs_by_mac[mac_id] = hub
            self._controllers_by_entry[entry_id] = ctrl

            # Do not configure TPI events here — the controller may still be
            # booting. ZenHub enables events only after is_controller_ready().

            _LOGGER.info(
                "Attached controller %s (%s) to shared runtime (%d entries)",
                ctrl.label,
                mac,
                len(self._hubs_by_entry),
            )
            return ctrl

    async def async_configure_controller_events(self, ctrl: ZenController | None) -> None:
        """Enable TPI events for a controller that is already ready.

        No-op when the shared listener is not running yet; ``async_ensure_started``
        configures every registered controller when it first binds the listener.
        """
        if not self._started or ctrl is None:
            return
        try:
            await self.zen.configure_controller_events(ctrl)
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable TPI events for {ctrl.label}") from err

    async def async_detach(self, entry_id: str) -> None:
        """Unregister a hub; close the client when no entries remain."""
        async with self._attach_lock:
            hub = self._hubs_by_entry.pop(entry_id, None)
            ctrl = self._controllers_by_entry.pop(entry_id, None)
            if hub is None and ctrl is None:
                return

            if ctrl is not None and ctrl.mac:
                self._hubs_by_mac.pop(normalize_mac_id(str(ctrl.mac)), None)
            elif hub is not None and hub.controller is not None and hub.controller.mac:
                self._hubs_by_mac.pop(normalize_mac_id(str(hub.controller.mac)), None)

            if ctrl is None and hub is not None:
                ctrl = hub.controller

            if ctrl is not None:
                try:
                    await self.zen.remove_controller(ctrl)
                except Exception:
                    _LOGGER.exception("Error removing controller for entry %s", entry_id)

            if hub is not None:
                hub.controller = None

            if self._hubs_by_entry or self._controllers_by_entry:
                _LOGGER.info(
                    "Detached entry %s from shared runtime (%d remain)",
                    entry_id,
                    len(self._hubs_by_entry),
                )
                return

            _LOGGER.info("Last entry detached; closing shared zencontrol runtime")
            await self.async_close()

    async def async_ensure_started(self) -> None:
        """Start the shared event listener if it is not already running."""
        async with self._start_lock:
            if self._started or self._stopping:
                return
            await self.zen.start()
            self._started = True
            self._listener_up = True

    async def async_close(self) -> None:
        """Stop the client and remove this runtime from hass.data."""
        if self._stopping:
            return
        self._stopping = True
        self._listener_up = False
        self._started = False
        self._hubs_by_entry.clear()
        self._hubs_by_mac.clear()
        self._controllers_by_entry.clear()

        zen = self.zen
        await zen.aclose()
        zen.callbacks.on_connect = None
        zen.callbacks.on_disconnect = None
        zen.callbacks.on_resync = None
        zen.callbacks.light_change = None
        zen.callbacks.group_change = None
        zen.callbacks.button_press = None
        zen.callbacks.button_long_press = None
        zen.callbacks.motion_event = None
        zen.callbacks.absolute_input_change = None
        zen.callbacks.system_variable_change = None
        zen.callbacks.profile_change = None
        zen.callbacks.controller_discovered = None
        zen.callbacks.controller_status_change = None

        domain_data = self.hass.data.get(DOMAIN)
        if isinstance(domain_data, dict) and domain_data.get(DATA_RUNTIME) is self:
            domain_data.pop(DATA_RUNTIME, None)
            if not domain_data:
                self.hass.data.pop(DOMAIN, None)

    # ------------------------------------------------------------------
    # Callbacks → per-entry hubs
    # ------------------------------------------------------------------

    async def _on_connect(self) -> None:
        _LOGGER.info("zencontrol event listener connected")
        self._listener_up = True
        for hub in list(self._hubs_by_entry.values()):
            await hub.handle_listener_connect()

    async def _on_disconnect(self) -> None:
        _LOGGER.info("zencontrol event listener disconnected")
        self._listener_up = False
        for hub in list(self._hubs_by_entry.values()):
            hub.handle_listener_disconnect()

    async def _on_resync(self) -> None:
        """Event session restored after a gap — re-poll without flipping availability."""
        _LOGGER.info("zencontrol event listener resynced after session gap")
        for hub in list(self._hubs_by_entry.values()):
            await hub.handle_listener_resync()

    async def _on_controller_status(self, ctrl: ZenController, status: str) -> None:
        hub = self.hub_for_controller(ctrl)
        if hub is not None:
            await hub.handle_controller_status(status)

    async def _on_light_change(
        self,
        light: ZenLight,
        level: int | None = None,
        colour: ZenColour | None = None,
        scene: int | None = None,
    ) -> None:
        hub = self.hub_for_controller(light.address.controller)
        if hub is not None:
            hub.handle_light_change(light)

    async def _on_group_change(
        self,
        group: ZenGroup,
        level: int | None = None,
        colour: ZenColour | None = None,
        scene: int | None = None,
        discoordinated: bool | None = None,
    ) -> None:
        hub = self.hub_for_controller(group.address.controller)
        if hub is not None:
            hub.handle_group_change(group)

    async def _on_button_press(self, button: ZenButton) -> None:
        hub = self.hub_for_controller(button.instance.address.controller)
        if hub is not None:
            hub.handle_button_press(button)

    async def _on_button_long_press(self, button: ZenButton) -> None:
        hub = self.hub_for_controller(button.instance.address.controller)
        if hub is not None:
            hub.handle_button_long_press(button)

    async def _on_motion_event(self, sensor: ZenMotionSensor, occupied: bool) -> None:
        hub = self.hub_for_controller(sensor.instance.address.controller)
        if hub is not None:
            hub.handle_motion_event(sensor, occupied)

    async def _on_absolute_input_change(self, absolute_input: ZenAbsoluteInput, value: int) -> None:
        hub = self.hub_for_controller(absolute_input.instance.address.controller)
        if hub is not None:
            hub.handle_absolute_input_change(absolute_input, value)

    async def _on_sv_change(
        self,
        system_variable: ZenSystemVariable,
        value: int,
        changed: bool,
        by_me: bool,
    ) -> None:
        hub = self.hub_for_controller(system_variable.controller)
        if hub is not None:
            hub.handle_sv_change(system_variable, value, by_me=by_me)

    async def _on_profile_change(self, profile: ZenProfile) -> None:
        hub = self.hub_for_controller(profile.controller)
        if hub is not None:
            hub.handle_profile_change(profile)

    async def _on_controller_discovered(self, discovered: DiscoveredController) -> None:
        """Schedule enrichment + discovery flow off the event consumer path."""
        mac = discovered.mac
        host = discovered.host
        if not mac or not host:
            return
        mac_n = normalize_mac(str(mac))
        mac_id = normalize_mac_id(mac_n)

        if mac_is_configured(self.hass, mac_id):
            return
        if mac_id in self._hubs_by_mac:
            return

        # Identity-only callback must not await command queries on the consumer.
        self.hass.async_create_task(self._async_offer_discovered(discovered))

    async def _async_offer_discovered(self, discovered: DiscoveredController) -> None:
        """Probe controller label, then start a discovery config flow."""
        try:
            enriched = await self.zen.enrich_discovered(discovered)
        except Exception:
            _LOGGER.debug(
                "Label probe failed for discovered controller %s (%s)",
                discovered.host,
                discovered.mac,
                exc_info=True,
            )
            enriched = discovered

        mac_n = normalize_mac(str(enriched.mac))
        mac_id = normalize_mac_id(mac_n)
        if mac_is_configured(self.hass, mac_id) or mac_id in self._hubs_by_mac:
            return

        label = (enriched.label or "").strip() or mac_n
        port = int(enriched.port or DEFAULT_PORT)
        host = enriched.host
        _LOGGER.info(
            "Discovered zencontrol controller %s (%s) label=%r",
            host,
            mac_n,
            label,
        )
        await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "discovery"},
            data={
                "host": host,
                "port": port,
                "mac": mac_n,
                "label": label,
            },
        )


def entry_unicast(data: Mapping[str, Any]) -> bool:
    """Return the unicast flag from entry data."""
    return bool(data.get(CONF_UNICAST, False))
