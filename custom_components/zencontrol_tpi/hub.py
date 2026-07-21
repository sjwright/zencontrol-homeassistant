"""ZenHub: manages the ZenControl lifecycle and entity dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import zencontrol  # type: ignore[import-untyped]
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_CONTROLLERS, CONF_LABEL, CONF_MAC, CONF_NAME, CONF_UNICAST
from .manifest_store import (
    DiscoveryManifestStore,
    build_manifest,
    load_entities_from_manifest,
)
from .rate_limiter import RateLimiter
from .sysvar import classify_sysvar_entity

_LOGGER = logging.getLogger(__name__)

_STARTUP_RETRY_INTERVAL = 10  # seconds between is_controller_ready polls
_READY_QUERY_TIMEOUT = 10.0
_READY_WAIT_MAX = 300.0  # give up waiting for controller boot after 5 minutes

# Entry IDs that should force full bus discovery on the next setup (reload).
_FORCE_FULL_DISCOVERY: set[str] = set()


def pop_force_full_discovery(entry_id: str) -> bool:
    """Return and clear whether this entry should force full discovery."""
    try:
        _FORCE_FULL_DISCOVERY.remove(entry_id)
    except KeyError:
        return False
    return True


def mark_force_full_discovery(entry_id: str) -> None:
    """Request full bus discovery on the next setup of this entry."""
    _FORCE_FULL_DISCOVERY.add(entry_id)


class ZenHub:
    """Manages the zencontrol-python client and dispatches events to HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ZencontrolTpiConfigEntry,
        *,
        force_full_discovery: bool = False,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._force_full_discovery = force_full_discovery
        self._manifest_store = DiscoveryManifestStore(hass, entry.entry_id)
        self._rate_limiter = RateLimiter(max_concurrent=5, delay_between_batches=0.1)
        self._available = False
        self._stopping = False
        # Per-controller online flags (name → bool). Event-listener up/down still
        # gates all entities via _available; this tracks controller.connected too.
        self._controller_online: dict[str, bool] = {}

        self.zen: zencontrol.ZenControl | None = None
        self.controllers: list[zencontrol.ZenController] = []

        self.lights: list[zencontrol.ZenLight] = []
        self.groups: list[zencontrol.ZenGroup] = []
        self.buttons: list[zencontrol.ZenButton] = []
        self.motion_sensors: list[zencontrol.ZenMotionSensor] = []
        self.sv_switches: list[zencontrol.ZenSystemVariable] = []
        self.sv_sensors: list[zencontrol.ZenSystemVariable] = []
        self.profiles: list[zencontrol.ZenProfile] = []

        self._discovery_callbacks: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._discovery_complete = False

        self._light_entities: dict[Any, Any] = {}
        self._group_entities: dict[Any, Any] = {}
        self._button_entities: dict[Any, Any] = {}
        self._motion_sensor_entities: dict[Any, Any] = {}
        self._sv_sensor_entities: dict[Any, Any] = {}
        self._sv_switch_entities: dict[Any, Any] = {}
        self._profile_entities: dict[Any, Any] = {}
        self._scene_entities: dict[Any, Any] = {}

    @property
    def available(self) -> bool:
        """Return True when the event listener is up and discovery succeeded."""
        return self._available

    def is_controller_available(self, zen_ctrl: Any | None = None) -> bool:
        """Return availability for a specific controller (or hub-wide if None)."""
        if not self._available:
            return False
        if zen_ctrl is None:
            return True
        name = getattr(zen_ctrl, "name", None)
        if name is None:
            return True
        return self._controller_online.get(name, bool(getattr(zen_ctrl, "connected", True)))

    # ------------------------------------------------------------------
    # Entity registration
    # ------------------------------------------------------------------

    def register_light_entity(self, zen_light: Any, entity: Any) -> None:
        self._light_entities[zen_light] = entity

    def register_group_entity(self, zen_group: Any, entity: Any) -> None:
        self._group_entities[zen_group] = entity

    def register_button_entity(self, zen_button: Any, entity: Any) -> None:
        self._button_entities[zen_button] = entity

    def register_motion_sensor_entity(self, zen_sensor: Any, entity: Any) -> None:
        self._motion_sensor_entities[zen_sensor] = entity

    def register_sv_sensor_entity(self, zen_sv: Any, entity: Any) -> None:
        self._sv_sensor_entities[zen_sv] = entity

    def register_sv_switch_entity(self, zen_sv: Any, entity: Any) -> None:
        self._sv_switch_entities[zen_sv] = entity

    def register_profile_entity(self, zen_controller: Any, entity: Any) -> None:
        # ZenController is a dataclass (unhashable). Key by name instead.
        self._profile_entities[zen_controller.name] = entity

    def register_scene_entity(self, zen_group: Any, entity: Any) -> None:
        self._scene_entities[zen_group] = entity

    def register_discovery_callback(
        self, callback: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Register a coroutine to call when discovery completes."""
        if self._discovery_complete:
            self.hass.async_create_task(callback())
        else:
            self._discovery_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Setup / Start / Stop
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Create ZenControl, wire callbacks, and add controllers from config."""
        data = self.entry.data
        unicast: bool = data.get(CONF_UNICAST, False)

        self.zen = zencontrol.ZenControl(
            logger=_LOGGER,
            unicast=unicast,
        )

        self.zen.on_connect = self._on_connect
        self.zen.on_disconnect = self._on_disconnect
        self.zen.light_change = self._on_light_change
        self.zen.group_change = self._on_group_change
        self.zen.button_press = self._on_button_press
        self.zen.button_long_press = self._on_button_long_press
        self.zen.motion_event = self._on_motion_event
        self.zen.system_variable_change = self._on_sv_change
        self.zen.profile_change = self._on_profile_change

        for idx, ctrl_cfg in enumerate(data.get(CONF_CONTROLLERS, []), start=1):
            ctrl = self.zen.add_controller(
                id=idx,
                name=ctrl_cfg[CONF_NAME],
                label=ctrl_cfg[CONF_LABEL],
                host=ctrl_cfg["host"],
                port=ctrl_cfg.get("port", 5108),
                mac=ctrl_cfg.get(CONF_MAC),
            )
            self.controllers.append(ctrl)
            self._controller_online[ctrl.name] = False

    async def async_start(self) -> None:
        """Wait for controllers, discover entities, refresh state, start events."""
        try:
            await self._wait_for_controllers()
            await self._discover_entities()
            await self._refresh_light_states()
            assert self.zen is not None
            await self.zen.start()
            self._available = True
            await self._notify_discovery_complete()
        except ConfigEntryNotReady:
            self._available = False
            await self._notify_discovery_complete()
            raise
        except asyncio.CancelledError:
            _LOGGER.debug("ZenHub startup task cancelled")
            raise
        except Exception as err:
            self._available = False
            await self._notify_discovery_complete()
            raise ConfigEntryNotReady(
                f"zencontrol setup failed: {err}"
            ) from err

    async def _wait_for_controllers(self) -> None:
        """Poll until every controller is ready, then interview.

        Raises ConfigEntryNotReady if a controller is unreachable or still
        booting after ``_READY_WAIT_MAX`` seconds so HA can retry with backoff.
        """
        for ctrl in self.controllers:
            _LOGGER.info("Waiting for controller %s to be ready…", ctrl.label)
            deadline = asyncio.get_running_loop().time() + _READY_WAIT_MAX
            while True:
                try:
                    ready = await asyncio.wait_for(
                        ctrl.is_controller_ready(),
                        timeout=_READY_QUERY_TIMEOUT,
                    )
                except TimeoutError:
                    ready = None

                if ready is None:
                    raise ConfigEntryNotReady(
                        f"Cannot reach controller {ctrl.label} ({ctrl.host})"
                    )
                if ready:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise ConfigEntryNotReady(
                        f"Controller {ctrl.label} ({ctrl.host}) still starting "
                        f"after {_READY_WAIT_MAX:.0f}s"
                    )
                _LOGGER.info(
                    "Controller %s still starting up, retrying in %ds…",
                    ctrl.label,
                    _STARTUP_RETRY_INTERVAL,
                )
                await asyncio.sleep(_STARTUP_RETRY_INTERVAL)

            await ctrl.interview()
            ctrl.connected = True
            self._controller_online[ctrl.name] = True
            _LOGGER.info(
                "Controller %s ready (version %s)", ctrl.label, ctrl.version
            )

    async def _discover_entities(self) -> None:
        """Full bus discovery or cached manifest load."""
        if self._force_full_discovery:
            manifest = None
        else:
            manifest = await self._manifest_store.async_load()

        if manifest:
            _LOGGER.info("Loading entities from cached discovery manifest")
            try:
                needs_save = await load_entities_from_manifest(self, manifest)
                if needs_save:
                    _LOGGER.info("Cached manifest outdated; re-saving after hydrate failures")
                    await self._manifest_store.async_save(build_manifest(self))
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Cached manifest invalid (%s), running full discovery", err
                )
                manifest = None

        if not manifest:
            if self._force_full_discovery:
                _LOGGER.info("Running full entity discovery (reload requested)")
            else:
                _LOGGER.info("Running full entity discovery")
            await self._run_full_discovery()
            await self._manifest_store.async_save(build_manifest(self))

        _LOGGER.info(
            "Discovery complete: %d lights, %d groups, %d buttons, "
            "%d motion sensors, %d sv_switches, %d sv_sensors, %d profiles",
            len(self.lights),
            len(self.groups),
            len(self.buttons),
            len(self.motion_sensors),
            len(self.sv_switches),
            len(self.sv_sensors),
            len(self.profiles),
        )

    async def _run_full_discovery(self) -> None:
        """Scan the bus for all entity types."""
        assert self.zen is not None

        raw_lights = await self.zen.get_lights()
        raw_groups = await self.zen.get_groups()
        raw_buttons = await self.zen.get_buttons()
        raw_sensors = await self.zen.get_motion_sensors()
        raw_svars = await self.zen.get_system_variables()
        raw_profiles = await self.zen.get_profiles()

        self.lights = sorted(raw_lights, key=lambda lt: lt.address.number)
        self.groups = sorted(raw_groups, key=lambda g: g.address.number)
        self.buttons = sorted(
            raw_buttons,
            key=lambda b: (b.instance.address.number, b.instance.number),
        )
        self.motion_sensors = sorted(
            raw_sensors,
            key=lambda s: (s.instance.address.number, s.instance.number),
        )
        self.profiles = sorted(
            raw_profiles, key=lambda p: (p.controller.name, p.number)
        )

        self.sv_switches = []
        self.sv_sensors = []
        for sv in sorted(raw_svars, key=lambda s: s.id):
            as_sensor, as_switch = classify_sysvar_entity(sv)
            if as_switch:
                self.sv_switches.append(sv)
            if as_sensor:
                self.sv_sensors.append(sv)

    async def _refresh_light_states(self) -> None:
        """Batch refresh runtime state after discovery (mqtt_bridge pattern).

        Interview/hydrate only restore static metadata. Current light/group
        levels and system-variable values are queried here.
        """
        coros: list[Coroutine[Any, Any, Any]] = [
            light.refresh_state_from_controller()
            for light in self.lights
        ]
        coros.extend(
            group.refresh_state_from_controller()
            for group in self.groups
            if group.lights
        )
        coros.extend(
            sensor.refresh_state_from_controller()
            for sensor in self.motion_sensors
        )
        seen_sv: set[tuple[str, int]] = set()
        for sv in (*self.sv_switches, *self.sv_sensors):
            key = (sv.controller.name, sv.id)
            if key in seen_sv:
                continue
            seen_sv.add(key)
            coros.append(sv.refresh_state_from_controller())
        if coros:
            _LOGGER.debug(
                "Refreshing state for %d lights/groups/sysvars", len(coros)
            )
            results = await self._rate_limiter.execute_batch(
                coros, return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception):
                    _LOGGER.warning("State refresh failed: %s", result)

    async def _notify_discovery_complete(self) -> None:
        """Signal platforms that discovery finished (success or failure)."""
        self._discovery_complete = True
        for cb in self._discovery_callbacks:
            await cb()

    async def async_stop(self) -> None:
        """Stop monitoring, close UDP clients, and clear callbacks."""
        self._stopping = True
        self._available = False
        if self.zen is not None:
            await self.zen.aclose()
            self.zen.on_connect = None
            self.zen.on_disconnect = None
            self.zen.light_change = None
            self.zen.group_change = None
            self.zen.button_press = None
            self.zen.button_long_press = None
            self.zen.motion_event = None
            self.zen.system_variable_change = None
            self.zen.profile_change = None

    # ------------------------------------------------------------------
    # zencontrol-python callbacks → HA entity dispatch
    # ------------------------------------------------------------------

    def _set_all_controllers_online(self, online: bool) -> None:
        """Update per-controller online flags (e.g. on event-listener connect)."""
        for ctrl in self.controllers:
            ctrl.connected = online
            self._controller_online[ctrl.name] = online

    async def _on_connect(self) -> None:
        """Library reconnect supervisor calls this on each successful session."""
        _LOGGER.info("zencontrol event listener connected")
        self._available = True
        self._set_all_controllers_online(True)
        # Initial setup already refreshed before zen.start(); re-query only on reconnect.
        if self._discovery_complete and not self._stopping:
            await self._refresh_light_states()
        self._write_entity_states()

    async def _on_disconnect(self) -> None:
        """Library notifies disconnect; reconnect is handled inside ZenControl."""
        _LOGGER.info("zencontrol event listener disconnected")
        self._available = False
        self._set_all_controllers_online(False)
        self._write_entity_states()

    def _write_entity_states(self) -> None:
        """Push current state (including availability) for all registered entities."""
        for entity in (
            *self._light_entities.values(),
            *self._group_entities.values(),
            *self._button_entities.values(),
            *self._motion_sensor_entities.values(),
            *self._sv_sensor_entities.values(),
            *self._sv_switch_entities.values(),
            *self._profile_entities.values(),
            *self._scene_entities.values(),
        ):
            # Only write state for entities that have been added to HA.
            # During a failed setup, entities may be constructed but never
            # registered, so entity_id is not yet set.
            if entity.entity_id:
                entity.async_write_ha_state()

    async def _on_light_change(
        self,
        light: Any,
        level: int | None = None,
        colour: Any | None = None,
        scene: int | None = None,
    ) -> None:
        entity = self._light_entities.get(light)
        if entity is not None:
            entity.update_state()

    async def _on_group_change(
        self,
        group: Any,
        level: int | None = None,
        colour: Any | None = None,
        scene: int | None = None,
        discoordinated: bool | None = None,
    ) -> None:
        group_entity = self._group_entities.get(group)
        if group_entity is not None:
            group_entity.update_state()
        scene_entity = self._scene_entities.get(group)
        if scene_entity is not None:
            scene_entity.update_current_option()

    async def _on_button_press(self, button: Any) -> None:
        entity = self._button_entities.get(button)
        if entity is not None:
            entity.trigger_event("short_press")

    async def _on_button_long_press(self, button: Any) -> None:
        entity = self._button_entities.get(button)
        if entity is not None:
            entity.trigger_event("long_press")

    async def _on_motion_event(self, sensor: Any, occupied: bool) -> None:
        entity = self._motion_sensor_entities.get(sensor)
        if entity is not None:
            entity.update_occupied(occupied)

    async def _on_sv_change(
        self,
        system_variable: Any,
        value: int,
        changed: bool,
        by_me: bool,
    ) -> None:
        sensor_entity = self._sv_sensor_entities.get(system_variable)
        if sensor_entity is not None:
            sensor_entity.update_value(value)

        if by_me:
            return

        switch_entity = self._sv_switch_entities.get(system_variable)
        if switch_entity is not None:
            switch_entity.update_value(value)

    async def _on_profile_change(self, profile: Any) -> None:
        entity = self._profile_entities.get(profile.controller.name)
        if entity is not None:
            entity.update_current_option()


type ZencontrolTpiConfigEntry = ConfigEntry[ZenHub]
