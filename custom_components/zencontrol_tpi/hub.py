"""ZenHub: per-entry controller slice over the shared ZenControl runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_NAME,
    DATA_PENDING_MANIFEST,
    DOMAIN,
    controller_from_entry_data,
)
from .entity import (
    controller_device_info,
    controller_identifier,
    sub_device_device_info,
)
from .manifest_store import (
    DiscoveryManifestStore,
    build_manifest,
    load_entities_from_manifest,
)
from .rate_limiter import RateLimiter
from .runtime import SharedZenRuntime
from .sub_devices import (
    SubDeviceDef,
    build_assignments,
    button_assignment_key,
    group_assignment_key,
    light_assignment_key,
    motion_assignment_key,
    sub_devices_from_controller,
    sysvar_assignment_key,
)
from .sysvar import classify_sysvar_entity

_LOGGER = logging.getLogger(__name__)

type DiscoveryCallback = Callable[[], Coroutine[Any, Any, None]]

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
    """Per-config-entry hub for one controller on the shared runtime."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ZencontrolTpiConfigEntry,
        runtime: SharedZenRuntime,
        *,
        force_full_discovery: bool = False,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self._force_full_discovery = force_full_discovery
        self._manifest_store = DiscoveryManifestStore(hass, entry.entry_id)
        self._rate_limiter = RateLimiter(max_concurrent=5, delay_between_batches=0.1)
        self._controller_online = False
        self._stopping = False
        self._attached = False

        self.controller: Any | None = None
        # Compatibility alias used by platforms/tests that iterate controllers.
        self.controllers: list[Any] = []

        self.lights: list[Any] = []
        self.groups: list[Any] = []
        self.buttons: list[Any] = []
        self.motion_sensors: list[Any] = []
        self.sv_switches: list[Any] = []
        self.sv_sensors: list[Any] = []
        self.profiles: list[Any] = []

        self._discovery_callbacks: list[DiscoveryCallback] = []
        self._discovery_complete = False

        self._light_entities: dict[Any, Any] = {}
        self._group_entities: dict[Any, Any] = {}
        self._button_entities: dict[Any, Any] = {}
        self._motion_sensor_entities: dict[Any, Any] = {}
        self._sv_sensor_entities: dict[Any, Any] = {}
        self._sv_switch_entities: dict[Any, Any] = {}
        self._profile_entities: dict[Any, Any] = {}
        self._scene_entities: dict[Any, Any] = {}

        self._sub_devices_by_controller: dict[str, list[SubDeviceDef]] = {}
        self._sub_device_assignments: dict[str, str] = {}

    @property
    def zen(self) -> Any:
        """Shared ZenControl client."""
        return self.runtime.zen

    @property
    def available(self) -> bool:
        """Return True when the listener is up and this controller is online."""
        return self.runtime.listener_up and self._controller_online

    def is_controller_available(self, zen_ctrl: Any | None = None) -> bool:
        """Return availability for this hub's controller."""
        if not self.runtime.listener_up:
            return False
        if zen_ctrl is None:
            return self._controller_online
        if zen_ctrl is self.controller:
            return self._controller_online
        if (
            self.controller is not None
            and getattr(zen_ctrl, "name", None) == self.controller.name
        ):
            return self._controller_online
        return False

    def device_info_for(
        self,
        zen_ctrl: Any,
        *,
        assignment_key: str | None = None,
    ) -> Any:
        """Return parent or sub-device DeviceInfo for an assignment key."""
        sub_id = (
            self._sub_device_assignments.get(assignment_key) if assignment_key else None
        )
        if not sub_id:
            return controller_device_info(zen_ctrl)
        devices = self._sub_devices_by_controller.get(zen_ctrl.name) or []
        device = next((d for d in devices if d.id == sub_id), None)
        if device is None:
            return controller_device_info(zen_ctrl)
        return sub_device_device_info(
            zen_ctrl, sub_device_id=device.id, sub_device_name=device.name
        )

    def sync_device_assignments(self) -> None:
        """Idempotently assign every entity to its controller or sub-device."""
        self._rebuild_sub_device_assignments()

        device_registry = dr.async_get(self.hass)
        entity_registry = er.async_get(self.hass)
        expected_identifiers = self._expected_device_identifiers()

        for zen_ctrl in self.controllers:
            self._ensure_registry_device(
                device_registry, controller_device_info(zen_ctrl)
            )

        for zen_ctrl in self.controllers:
            for device_def in self._sub_devices_by_controller.get(zen_ctrl.name) or []:
                device = self._ensure_registry_device(
                    device_registry,
                    sub_device_device_info(
                        zen_ctrl,
                        sub_device_id=device_def.id,
                        sub_device_name=device_def.name,
                    ),
                )
                if device.area_id != device_def.area_id:
                    device_registry.async_update_device(
                        device.id, area_id=device_def.area_id
                    )

        updated = 0
        for entity, zen_ctrl, key in self._iter_device_assignment_targets():
            info = self.device_info_for(zen_ctrl, assignment_key=key)
            entity._attr_device_info = info
            entity_id = getattr(entity, "entity_id", None)
            if not entity_id:
                continue

            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None:
                _LOGGER.debug(
                    "Skipping device assignment for %s; not in entity registry yet",
                    entity_id,
                )
                continue

            device = self._ensure_registry_device(device_registry, info)
            if registry_entry.device_id == device.id:
                continue
            try:
                entity_registry.async_update_entity(entity_id, device_id=device.id)
            except ValueError as err:
                _LOGGER.warning(
                    "Could not assign %s to device %s: %s",
                    entity_id,
                    device.id,
                    err,
                )
                continue
            updated += 1

        removed = self._prune_orphaned_devices(device_registry, expected_identifiers)

        _LOGGER.info(
            "Synced device assignments: %d entities updated, %d orphan devices "
            "removed (%d assignment keys)",
            updated,
            removed,
            len(self._sub_device_assignments),
        )

    def _ensure_registry_device(
        self,
        device_registry: dr.DeviceRegistry,
        info: Any,
    ) -> Any:
        """Create or update a registry device from DeviceInfo."""
        return device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=info["identifiers"],
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            name=info.get("name"),
            sw_version=info.get("sw_version"),
            via_device=info.get("via_device"),
        )

    def _rebuild_sub_device_assignments(self) -> None:
        """Recompute label-prefix sub-device assignments from config + discovery."""
        self._sub_devices_by_controller = {}
        ctrl_cfg = controller_from_entry_data(self.entry.data)
        if ctrl_cfg:
            name = ctrl_cfg.get(CONF_NAME)
            if name:
                self._sub_devices_by_controller[name] = sub_devices_from_controller(
                    ctrl_cfg
                )

        sysvars = list({*self.sv_switches, *self.sv_sensors})
        self._sub_device_assignments = build_assignments(
            controller_sub_devices=self._sub_devices_by_controller,
            lights=self.lights,
            groups=self.groups,
            buttons=self.buttons,
            motion_sensors=self.motion_sensors,
            sysvars=sysvars,
        )

    def _expected_device_identifiers(self) -> set[tuple[str, str]]:
        """Identifiers for controllers and sub-devices that should exist."""
        expected: set[tuple[str, str]] = set()
        for zen_ctrl in self.controllers:
            parent = controller_identifier(zen_ctrl)
            expected.add(parent)
            for device_def in self._sub_devices_by_controller.get(zen_ctrl.name) or []:
                expected.add((DOMAIN, f"{parent[1]}:sub:{device_def.id}"))
        return expected

    def _prune_orphaned_devices(
        self,
        device_registry: dr.DeviceRegistry,
        expected_identifiers: set[tuple[str, str]],
    ) -> int:
        """Remove config-entry devices whose identifiers are no longer expected."""
        if not expected_identifiers:
            return 0

        removed = 0
        for device in dr.async_entries_for_config_entry(
            device_registry, self.entry.entry_id
        ):
            domain_idents = {
                ident for ident in device.identifiers if ident[0] == DOMAIN
            }
            if not domain_idents:
                continue
            if domain_idents.isdisjoint(expected_identifiers):
                device_registry.async_remove_device(device.id)
                removed += 1
        return removed

    def _iter_device_assignment_targets(
        self,
    ) -> list[tuple[Any, Any, str | None]]:
        """Return (entity, controller, assignment_key) for every hub entity."""
        targets: list[tuple[Any, Any, str | None]] = []
        for zen_light, entity in self._light_entities.items():
            targets.append(
                (entity, zen_light.address.controller, light_assignment_key(zen_light))
            )
        for zen_group, entity in self._group_entities.items():
            targets.append(
                (entity, zen_group.address.controller, group_assignment_key(zen_group))
            )
        for zen_group, entity in self._scene_entities.items():
            targets.append(
                (entity, zen_group.address.controller, group_assignment_key(zen_group))
            )
        for zen_button, entity in self._button_entities.items():
            targets.append(
                (
                    entity,
                    zen_button.instance.address.controller,
                    button_assignment_key(zen_button),
                )
            )
        for zen_sensor, entity in self._motion_sensor_entities.items():
            targets.append(
                (
                    entity,
                    zen_sensor.instance.address.controller,
                    motion_assignment_key(zen_sensor),
                )
            )
        for zen_sv, entity in self._sv_sensor_entities.items():
            targets.append(
                (entity, zen_sv.controller, sysvar_assignment_key(zen_sv))
            )
        for zen_sv, entity in self._sv_switch_entities.items():
            targets.append(
                (entity, zen_sv.controller, sysvar_assignment_key(zen_sv))
            )
        for ctrl_name, entity in self._profile_entities.items():
            ctrl = next((c for c in self.controllers if c.name == ctrl_name), None)
            if ctrl is not None:
                targets.append((entity, ctrl, None))
        return targets

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
        self._profile_entities[zen_controller.name] = entity

    def register_scene_entity(self, zen_group: Any, entity: Any) -> None:
        self._scene_entities[zen_group] = entity

    def register_discovery_callback(self, callback: DiscoveryCallback) -> None:
        """Register a coroutine to call when discovery completes."""
        if self._discovery_complete:
            self.hass.async_create_task(callback())
        else:
            self._discovery_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Setup / Start / Stop
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Attach this entry's controller to the shared runtime."""
        ctrl_cfg = controller_from_entry_data(self.entry.data)
        if not ctrl_cfg:
            raise ConfigEntryNotReady("Config entry has no controller")

        self.controller = await self.runtime.async_attach(self, ctrl_cfg)
        self.controllers = [self.controller]
        self._attached = True
        self._controller_online = False

        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_hass_stop
            )
        )

    async def _async_hass_stop(self, _event: Event) -> None:
        """Close connections as soon as Home Assistant begins shutting down."""
        await self.async_stop()

    async def async_start(self) -> None:
        """Wait for this controller, discover entities, then ensure listener."""
        try:
            await self._wait_for_controller()
            await self._discover_entities()
            self.sync_device_assignments()
            await self._refresh_light_states()
            await self.runtime.async_ensure_started()
            self._controller_online = True
            await self._notify_discovery_complete()
            await self.hass.async_block_till_done()
            self.sync_device_assignments()
        except ConfigEntryNotReady:
            self._controller_online = False
            await self._notify_discovery_complete()
            raise
        except asyncio.CancelledError:
            _LOGGER.debug("ZenHub startup task cancelled")
            raise
        except Exception as err:
            self._controller_online = False
            await self._notify_discovery_complete()
            raise ConfigEntryNotReady(
                f"zencontrol setup failed: {err}"
            ) from err

    async def _wait_for_controller(self) -> None:
        """Poll until this controller is ready, then interview."""
        ctrl = self.controller
        assert ctrl is not None
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
        self._controller_online = True
        _LOGGER.info(
            "Controller %s ready (version %s)", ctrl.label, ctrl.version
        )

    async def _discover_entities(self) -> None:
        """Full bus discovery or cached manifest load for this controller."""
        from_pending = False
        if self._force_full_discovery:
            manifest = None
        else:
            pending = None
            domain_data = self.hass.data.get(DOMAIN, {})
            pending_map = domain_data.get(DATA_PENDING_MANIFEST)
            if isinstance(pending_map, dict):
                # New shape: mac_id → {"manifest": ...}
                if self.entry.unique_id in pending_map:
                    pending = pending_map.pop(self.entry.unique_id)
                    if not pending_map:
                        domain_data.pop(DATA_PENDING_MANIFEST, None)
                # Legacy single-blob shape (unique_id + manifest keys)
                elif (
                    pending_map.get("unique_id") == self.entry.unique_id
                    and isinstance(pending_map.get("manifest"), dict)
                ):
                    pending = domain_data.pop(DATA_PENDING_MANIFEST)

            if isinstance(pending, dict) and isinstance(pending.get("manifest"), dict):
                _LOGGER.info("Loading entities from config-flow discovery manifest")
                manifest = pending["manifest"]
                from_pending = True
            else:
                manifest = await self._manifest_store.async_load()

        if manifest:
            if not from_pending:
                _LOGGER.info("Loading entities from cached discovery manifest")
            try:
                needs_save = await load_entities_from_manifest(self, manifest)
                if needs_save or from_pending:
                    if needs_save:
                        _LOGGER.info(
                            "Cached manifest outdated; re-saving after hydrate failures"
                        )
                    await self._manifest_store.async_save(
                        build_manifest(self) if needs_save else manifest
                    )
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
        """Scan the bus for entity types on this controller only."""
        assert self.controller is not None
        zen = self.zen

        raw_lights = await zen.get_lights(controller=self.controller)
        raw_groups = await zen.get_groups(controller=self.controller)
        raw_buttons = await zen.get_buttons(controller=self.controller)
        raw_sensors = await zen.get_motion_sensors(controller=self.controller)
        raw_svars = await zen.get_system_variables(controller=self.controller)
        raw_profiles = await zen.get_profiles(controller=self.controller)

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
        """Batch refresh runtime state after discovery."""
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
        """Detach this entry from the shared runtime."""
        if self._stopping:
            return
        self._stopping = True
        self._controller_online = False
        if not self._attached:
            return
        self._attached = False
        await self.runtime.async_detach(self.entry.entry_id)

    # ------------------------------------------------------------------
    # Runtime → hub event handlers
    # ------------------------------------------------------------------

    async def handle_listener_connect(self) -> None:
        """Shared listener came up."""
        if self.controller is not None:
            self.controller.connected = True
        self._controller_online = True
        if self._discovery_complete and not self._stopping:
            await self._refresh_light_states()
        self._write_entity_states()

    def handle_listener_disconnect(self) -> None:
        """Shared listener went down."""
        if self.controller is not None:
            self.controller.connected = False
        self._controller_online = False
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
            if entity.entity_id:
                entity.async_write_ha_state()

    def handle_light_change(self, light: Any) -> None:
        if (entity := self._light_entities.get(light)) is not None:
            entity.update_state()

    def handle_group_change(self, group: Any) -> None:
        if (group_entity := self._group_entities.get(group)) is not None:
            group_entity.update_state()
        if (scene_entity := self._scene_entities.get(group)) is not None:
            scene_entity.update_current_option()

    def handle_button_press(self, button: Any) -> None:
        if (entity := self._button_entities.get(button)) is not None:
            entity.trigger_event("short_press")

    def handle_button_long_press(self, button: Any) -> None:
        if (entity := self._button_entities.get(button)) is not None:
            entity.trigger_event("long_press")

    def handle_motion_event(self, sensor: Any, occupied: bool) -> None:
        if (entity := self._motion_sensor_entities.get(sensor)) is not None:
            entity.update_occupied(occupied)

    def handle_sv_change(
        self,
        system_variable: Any,
        value: int,
        *,
        by_me: bool,
    ) -> None:
        if (sensor_entity := self._sv_sensor_entities.get(system_variable)) is not None:
            sensor_entity.update_value(value)
        if by_me:
            return
        if (switch_entity := self._sv_switch_entities.get(system_variable)) is not None:
            switch_entity.update_value(value)

    def handle_profile_change(self, profile: Any) -> None:
        if (entity := self._profile_entities.get(profile.controller.name)) is not None:
            entity.update_current_option()


type ZencontrolTpiConfigEntry = ConfigEntry[ZenHub]
