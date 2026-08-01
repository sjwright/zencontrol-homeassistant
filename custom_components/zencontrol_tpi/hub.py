"""ZenHub: per-entry controller slice over the shared ZenControl runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Container, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from zencontrol import (
    ZenAbsoluteInput,
    ZenBlind,
    ZenButton,
    ZenControl,
    ZenController,
    ZenFan,
    ZenGroup,
    ZenLight,
    ZenMotionSensor,
    ZenProfile,
    ZenSystemVariable,
)

from .const import (
    CONF_NAME,
    CONTROLLER_READY_QUERY_TIMEOUT,
    CONTROLLER_STATUS_ONLINE,
    CONTROLLER_STATUS_STARTING,
    CONTROLLER_STATUS_UNREACHABLE,
    DATA_PENDING_MANIFEST,
    DOMAIN,
    controller_from_entry_data,
)
from .discovery import (
    ControllerNotReadyError,
    discover_controller_entities,
    wait_until_controller_ready,
)
from .entity import (
    as_zen_controller,
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
    absolute_input_assignment_key,
    blind_assignment_key,
    build_assignments,
    button_assignment_key,
    fan_assignment_key,
    group_assignment_key,
    light_assignment_key,
    motion_assignment_key,
    sub_devices_from_controller,
    sysvar_assignment_key,
)

if TYPE_CHECKING:
    from .binary_sensor import ZenMotionSensorEntity
    from .cover import ZenBlindEntity
    from .event import ZenButtonEntity
    from .fan import ZenFanEntity
    from .light import ZenGroupEntity, ZenLightEntity
    from .scene import ZenGroupSceneEntity
    from .select import ZenGroupSceneSelectEntity, ZenProfileSelectEntity
    from .sensor import (
        ZenAbsoluteInputSensorEntity,
        ZenControllerStatusSensor,
        ZenSystemVariableSensorEntity,
    )
    from .switch import ZenSystemVariableSwitchEntity

_LOGGER = logging.getLogger(__name__)

type DiscoveryCallback = Callable[[], Coroutine[Any, Any, None]]

# Platform async_add_entities schedules work via ConfigEntry.async_create_task.
# Bound how long startup will wait for those tasks (not all of hass).
_ENTITY_ADD_TIMEOUT = 60.0

# Entry IDs that should force full bus discovery on the next setup (reload).
_FORCE_FULL_DISCOVERY: set[str] = set()


@dataclass(slots=True)
class _BoundEntity:
    """One HA entity registered against this hub, with device-assignment info."""

    entity: Entity
    controller: ZenController
    assignment_key: str | None


def _scene_select_key(group: ZenGroup) -> str:
    return f"scene_select:{group.address.controller.name}:{group.address.number}"


def _scene_key(group: ZenGroup, scene_number: int) -> str:
    return f"scene:{group.address.controller.name}:{group.address.number}:{scene_number}"


def _profile_key(controller_name: str) -> str:
    return f"profile:{controller_name}"


def _sv_sensor_key(sv: ZenSystemVariable) -> str:
    return f"sensor:{sysvar_assignment_key(sv)}"


def _sv_switch_key(sv: ZenSystemVariable) -> str:
    return f"switch:{sysvar_assignment_key(sv)}"


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
        self._controller_status: str = CONTROLLER_STATUS_UNREACHABLE
        self._stopping = False
        self._attached = False

        self.controller: ZenController | None = None

        self.lights: list[ZenLight] = []
        self.fans: list[ZenFan] = []
        self.blinds: list[ZenBlind] = []
        self.groups: list[ZenGroup] = []
        self.buttons: list[ZenButton] = []
        self.motion_sensors: list[ZenMotionSensor] = []
        self.absolute_inputs: list[ZenAbsoluteInput] = []
        self.sv_switches: list[ZenSystemVariable] = []
        self.sv_sensors: list[ZenSystemVariable] = []
        self.profiles: list[ZenProfile] = []

        self._discovery_callbacks: list[DiscoveryCallback] = []
        self._discovery_complete = False
        self._discovery_notified = False
        # True only after a successful async_start (events configured).
        self._setup_complete = False

        # All platform entities except the diagnostic status sensor.
        self._entities: dict[str, _BoundEntity] = {}
        self._status_entity: ZenControllerStatusSensor | None = None

        self._sub_devices_by_controller: dict[str, list[SubDeviceDef]] = {}
        self._sub_device_assignments: dict[str, str] = {}

    @property
    def zen(self) -> ZenControl:
        """Shared ZenControl client."""
        return self.runtime.zen

    @property
    def controllers(self) -> list[ZenController]:
        """Compatibility: platforms/tests that iterate controllers."""
        return [self.controller] if self.controller is not None else []

    @property
    def stopping(self) -> bool:
        """True while this hub is detaching from the shared runtime."""
        return self._stopping

    @property
    def controller_status(self) -> str:
        """Return online / starting / unreachable for this controller."""
        return self._controller_status

    @property
    def available(self) -> bool:
        """Return True when the listener is up and this controller is online."""
        return self.runtime.listener_up and self._controller_status == CONTROLLER_STATUS_ONLINE

    def is_controller_available(self, zen_ctrl: ZenController | None = None) -> bool:
        """Return availability for this hub's controller.

        Entities are unavailable while the controller reports not-ready
        (starting) as well as when it is unreachable.
        """
        if not self.runtime.listener_up:
            return False
        if zen_ctrl is None:
            return self._controller_status == CONTROLLER_STATUS_ONLINE
        if zen_ctrl is self.controller:
            return self._controller_status == CONTROLLER_STATUS_ONLINE
        if self.controller is not None and zen_ctrl.name == self.controller.name:
            return self._controller_status == CONTROLLER_STATUS_ONLINE
        return False

    def set_controller_status(self, status: str) -> None:
        """Update controller runtime status and push entity availability."""
        if status == self._controller_status:
            return
        previous = self._controller_status
        self._controller_status = status
        _LOGGER.info(
            "Controller %s status %s → %s",
            self.controller.label if self.controller else self.entry.entry_id,
            previous,
            status,
        )
        if self._status_entity is not None and self._status_entity.entity_id:
            self._status_entity.update_status(status)
        self._write_entity_states()

    def register_status_entity(self, entity: ZenControllerStatusSensor) -> None:
        """Register the diagnostic controller-status sensor."""
        self._status_entity = entity

    def device_info_for(
        self,
        zen_ctrl: ZenController,
        *,
        assignment_key: str | None = None,
    ) -> DeviceInfo:
        """Return parent or sub-device DeviceInfo for an assignment key."""
        sub_id = self._sub_device_assignments.get(assignment_key) if assignment_key else None
        if not sub_id:
            return controller_device_info(zen_ctrl)
        devices = self._sub_devices_by_controller.get(zen_ctrl.name) or []
        device = next((d for d in devices if d.id == sub_id), None)
        if device is None:
            return controller_device_info(zen_ctrl)
        return sub_device_device_info(zen_ctrl, sub_device_id=device.id, sub_device_name=device.name)

    def sync_device_assignments(self) -> None:
        """Idempotently assign every entity to its controller or sub-device."""
        self._rebuild_sub_device_assignments()

        device_registry = dr.async_get(self.hass)
        entity_registry = er.async_get(self.hass)
        expected_identifiers = self._expected_device_identifiers()

        if self.controller is not None:
            self._ensure_registry_device(device_registry, controller_device_info(self.controller))
            for device_def in self._sub_devices_by_controller.get(self.controller.name) or []:
                device = self._ensure_registry_device(
                    device_registry,
                    sub_device_device_info(
                        self.controller,
                        sub_device_id=device_def.id,
                        sub_device_name=device_def.name,
                    ),
                )
                if device.area_id != device_def.area_id:
                    device_registry.async_update_device(device.id, area_id=device_def.area_id)

        updated = 0
        for entity, zen_ctrl, key in self._iter_device_assignment_targets():
            info = self.device_info_for(zen_ctrl, assignment_key=key)
            entity._attr_device_info = info  # noqa: SLF001
            # entity_id is unset until HA has added the entity.
            entity_id = entity.entity_id
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
            "Synced device assignments: %d entities updated, %d orphan devices removed (%d assignment keys)",
            updated,
            removed,
            len(self._sub_device_assignments),
        )

    def _ensure_registry_device(
        self,
        device_registry: dr.DeviceRegistry,
        info: DeviceInfo,
    ) -> dr.DeviceEntry:
        """Create or update a registry device from DeviceInfo."""
        return device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=info.get("identifiers"),
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
                self._sub_devices_by_controller[name] = sub_devices_from_controller(ctrl_cfg)

        sysvars = list({*self.sv_switches, *self.sv_sensors})
        self._sub_device_assignments = build_assignments(
            controller_sub_devices=self._sub_devices_by_controller,
            lights=self.lights,
            fans=self.fans,
            blinds=self.blinds,
            groups=self.groups,
            buttons=self.buttons,
            motion_sensors=self.motion_sensors,
            absolute_inputs=self.absolute_inputs,
            sysvars=sysvars,
        )

    def _expected_device_identifiers(self) -> set[tuple[str, str]]:
        """Identifiers for controllers and sub-devices that should exist."""
        expected: set[tuple[str, str]] = set()
        if self.controller is None:
            return expected
        parent = controller_identifier(self.controller)
        expected.add(parent)
        for device_def in self._sub_devices_by_controller.get(self.controller.name) or []:
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
        for device in dr.async_entries_for_config_entry(device_registry, self.entry.entry_id):
            domain_idents = {ident for ident in device.identifiers if ident[0] == DOMAIN}
            if not domain_idents:
                continue
            if domain_idents.isdisjoint(expected_identifiers):
                device_registry.async_remove_device(device.id)
                removed += 1
        return removed

    def _iter_device_assignment_targets(
        self,
    ) -> list[tuple[Entity, ZenController, str | None]]:
        """Return (entity, controller, assignment_key) for every hub entity."""
        return [(bound.entity, bound.controller, bound.assignment_key) for bound in self._entities.values()]

    # ------------------------------------------------------------------
    # Entity registration
    # ------------------------------------------------------------------

    def _bind(
        self,
        key: str,
        entity: Entity,
        controller: ZenController,
        assignment_key: str | None,
    ) -> None:
        self._entities[key] = _BoundEntity(entity, controller, assignment_key)

    def _entity(self, key: str) -> Entity | None:
        bound = self._entities.get(key)
        return bound.entity if bound is not None else None

    def register_light_entity(self, zen_light: ZenLight, entity: ZenLightEntity) -> None:
        key = light_assignment_key(zen_light)
        self._bind(key, entity, as_zen_controller(zen_light.address.controller), key)

    def register_fan_entity(self, zen_fan: ZenFan, entity: ZenFanEntity) -> None:
        key = fan_assignment_key(zen_fan)
        self._bind(key, entity, as_zen_controller(zen_fan.address.controller), key)

    def register_cover_entity(self, zen_blind: ZenBlind, entity: ZenBlindEntity) -> None:
        key = blind_assignment_key(zen_blind)
        self._bind(key, entity, as_zen_controller(zen_blind.address.controller), key)

    def register_group_entity(self, zen_group: ZenGroup, entity: ZenGroupEntity) -> None:
        key = group_assignment_key(zen_group)
        self._bind(key, entity, as_zen_controller(zen_group.address.controller), key)

    def register_button_entity(self, zen_button: ZenButton, entity: ZenButtonEntity) -> None:
        key = button_assignment_key(zen_button)
        self._bind(key, entity, as_zen_controller(zen_button.instance.address.controller), key)

    def register_motion_sensor_entity(self, zen_sensor: ZenMotionSensor, entity: ZenMotionSensorEntity) -> None:
        key = motion_assignment_key(zen_sensor)
        self._bind(key, entity, as_zen_controller(zen_sensor.instance.address.controller), key)

    def register_absolute_input_entity(self, zen_input: ZenAbsoluteInput, entity: ZenAbsoluteInputSensorEntity) -> None:
        key = absolute_input_assignment_key(zen_input)
        self._bind(key, entity, as_zen_controller(zen_input.instance.address.controller), key)

    def register_sv_sensor_entity(self, zen_sv: ZenSystemVariable, entity: ZenSystemVariableSensorEntity) -> None:
        self._bind(_sv_sensor_key(zen_sv), entity, zen_sv.controller, sysvar_assignment_key(zen_sv))

    def register_sv_switch_entity(self, zen_sv: ZenSystemVariable, entity: ZenSystemVariableSwitchEntity) -> None:
        self._bind(_sv_switch_key(zen_sv), entity, zen_sv.controller, sysvar_assignment_key(zen_sv))

    def register_profile_entity(self, zen_controller: ZenController, entity: ZenProfileSelectEntity) -> None:
        self._bind(_profile_key(zen_controller.name), entity, zen_controller, None)

    def register_scene_select_entity(self, zen_group: ZenGroup, entity: ZenGroupSceneSelectEntity) -> None:
        self._bind(
            _scene_select_key(zen_group),
            entity,
            as_zen_controller(zen_group.address.controller),
            group_assignment_key(zen_group),
        )

    def register_scene_entity(self, zen_group: ZenGroup, scene_number: int, entity: ZenGroupSceneEntity) -> None:
        self._bind(
            _scene_key(zen_group, scene_number),
            entity,
            as_zen_controller(zen_group.address.controller),
            group_assignment_key(zen_group),
        )

    def register_discovery_callback(self, callback: DiscoveryCallback) -> None:
        """Register a coroutine to call when discovery completes."""
        if self._discovery_notified:
            # Discovery already finished (unusual race); run under this entry.
            self.entry.async_create_task(
                self.hass,
                self._async_run_discovery_callback(callback),
                f"zencontrol late discovery {self.entry.entry_id}",
            )
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
        self._attached = True
        self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)

        self.entry.async_on_unload(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_hass_stop))

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
            # Events must not be enabled until the controller is ready. If the
            # shared listener is already up (another entry), configure now;
            # otherwise zen.start() configures all controllers on first attach.
            already_started = self.runtime.started
            await self.runtime.async_ensure_started()
            if already_started:
                await self.runtime.async_configure_controller_events(self.controller)
            # Platforms may register during notify; only then mark online so
            # keepalive/on_connect cannot race entities into "available" early.
            await self._notify_discovery_complete()
            self._setup_complete = True
            self.set_controller_status(CONTROLLER_STATUS_ONLINE)
            self.sync_device_assignments()
        except ConfigEntryNotReady:
            # Keep starting/unreachable status set by _wait_for_controller.
            await self._async_notify_discovery_best_effort()
            raise
        except asyncio.CancelledError:
            _LOGGER.debug("ZenHub startup task cancelled")
            raise
        except Exception as err:
            self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)
            await self._async_notify_discovery_best_effort()
            raise ConfigEntryNotReady(f"zencontrol setup failed: {err}") from err

    def _entry_tracked_tasks(self) -> set[asyncio.Future[Any]]:
        """Return tasks created via ConfigEntry.async_create_task.

        EntityPlatform uses that API when integrations call the sync
        async_add_entities callback. There is no public accessor.
        """
        return self.entry._tasks  # noqa: SLF001

    async def _async_await_new_entry_tasks(
        self,
        before: Container[asyncio.Future[Any]],
        *,
        what: str,
    ) -> None:
        """Await entry tasks scheduled after before was snapshotted.

        Unlike hass.async_block_till_done(), this never waits on unrelated
        hass tasks (which deadlocks when CREATE_ENTRY is awaiting setup).
        """
        pending = [task for task in self._entry_tracked_tasks() if task not in before and not task.done()]
        if not pending:
            return

        _LOGGER.debug(
            "Waiting for %d %s task(s) for entry %s",
            len(pending),
            what,
            self.entry.entry_id,
        )
        done, not_done = await asyncio.wait(pending, timeout=_ENTITY_ADD_TIMEOUT)
        if not_done:
            for task in not_done:
                task.cancel()
            raise ConfigEntryNotReady(f"Timed out after {_ENTITY_ADD_TIMEOUT:.0f}s waiting for {what}")
        for task in done:
            if task.cancelled():
                raise asyncio.CancelledError
            exc = task.exception()
            if exc is not None:
                raise ConfigEntryNotReady(f"{what} failed: {exc}") from exc

    async def _async_run_discovery_callback(self, callback: DiscoveryCallback) -> None:
        """Run one platform callback and await entity-adds it schedules."""
        before = set(self._entry_tracked_tasks())
        await callback()
        await self._async_await_new_entry_tasks(before, what="platform entity add")
        if not self.stopping:
            self.sync_device_assignments()

    async def _wait_for_controller(self) -> None:
        """Poll until this controller is ready, then interview.

        Never proceeds to discovery/events while query_controller_startup_complete() is
        false. Controllers commonly take 1-10 minutes after reboot. While
        waiting, entities are unavailable and the status sensor shows
        starting / unreachable.
        """
        ctrl = self.controller
        assert ctrl is not None
        _LOGGER.info("Waiting for controller %s to be ready…", ctrl.label)
        self.set_controller_status(CONTROLLER_STATUS_STARTING)
        try:
            await wait_until_controller_ready(
                ctrl,
                on_unreachable=lambda: self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE),
                on_starting=lambda: self.set_controller_status(CONTROLLER_STATUS_STARTING),
            )
        except ControllerNotReadyError as err:
            raise ConfigEntryNotReady(str(err)) from err
        # Stay "starting" until async_start finishes listener/event setup.
        # Marking online here made the status sensor lie (and briefly marked
        # entities available) before the shared listener was up.
        _LOGGER.info(
            "Controller %s ready (version %s); finishing setup…",
            ctrl.label,
            ctrl.version,
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
            if isinstance(pending_map, dict) and self.entry.unique_id in pending_map:
                pending = pending_map.pop(self.entry.unique_id)
                if not pending_map:
                    domain_data.pop(DATA_PENDING_MANIFEST, None)

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
                        _LOGGER.info("Cached manifest outdated; re-saving after hydrate failures")
                    await self._manifest_store.async_save(build_manifest(self) if needs_save else manifest)
                self._prune_kind_changed_entities()
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Cached manifest invalid (%s), running full discovery", err)
                manifest = None

        if not manifest:
            if self._force_full_discovery:
                _LOGGER.info("Running full entity discovery (reload requested)")
            else:
                _LOGGER.info("Running full entity discovery")
            await self._run_full_discovery()
            await self._manifest_store.async_save(build_manifest(self))

        _LOGGER.info(
            "Discovery complete: %d lights, %d fans, %d blinds, %d groups, %d buttons, "
            "%d motion sensors, %d absolute inputs, %d sv_switches, "
            "%d sv_sensors, %d profiles",
            len(self.lights),
            len(self.fans),
            len(self.blinds),
            len(self.groups),
            len(self.buttons),
            len(self.motion_sensors),
            len(self.absolute_inputs),
            len(self.sv_switches),
            len(self.sv_sensors),
            len(self.profiles),
        )

    async def _run_full_discovery(self) -> None:
        """Scan the bus for entity types on this controller only."""
        assert self.controller is not None
        found = await discover_controller_entities(self.zen, self.controller)
        self.lights = found.lights
        self.fans = found.fans
        self.blinds = found.blinds
        self.groups = found.groups
        self.buttons = found.buttons
        self.motion_sensors = found.motion_sensors
        self.absolute_inputs = found.absolute_inputs
        self.sv_switches = found.sv_switches
        self.sv_sensors = found.sv_sensors
        self.profiles = found.profiles
        self._prune_kind_changed_entities()

    async def _refresh_light_states(self) -> None:
        """Batch refresh runtime state after discovery."""
        coros: list[Coroutine[Any, Any, Any]] = [light.refresh_state_from_controller() for light in self.lights]
        coros.extend(fan.refresh_state_from_controller() for fan in self.fans)
        coros.extend(blind.refresh_state_from_controller() for blind in self.blinds)
        coros.extend(
            group.refresh_state_from_controller()
            for group in self.groups
            if group.lights or group.fans or group.blinds
        )
        coros.extend(sensor.refresh_state_from_controller() for sensor in self.motion_sensors)
        seen_sv: set[tuple[str, int]] = set()
        for sv in (*self.sv_switches, *self.sv_sensors):
            key = (sv.controller.name, sv.id)
            if key in seen_sv:
                continue
            seen_sv.add(key)
            coros.append(sv.refresh_state_from_controller())
        if coros:
            _LOGGER.debug("Refreshing state for %d lights/fans/blinds/groups/sysvars", len(coros))
            results = await self._rate_limiter.execute_batch(coros, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    _LOGGER.warning("State refresh failed: %s", result)

    def _prune_kind_changed_entities(self) -> None:
        """Remove registry entities whose ECG kind no longer matches discovery.

        When a labeled fan/blind replaces a former light (or vice versa), the
        old unique_id would otherwise linger. Never clears user disabled_by.
        """
        if self.controller is None:
            return
        entity_registry = er.async_get(self.hass)
        ctrl = self.controller.name
        fan_numbers = {fan.address.number for fan in self.fans}
        blind_numbers = {blind.address.number for blind in self.blinds}
        light_numbers = {light.address.number for light in self.lights}
        kind_for_number = {
            **{n: "light" for n in light_numbers},
            **{n: "fan" for n in fan_numbers},
            **{n: "blind" for n in blind_numbers},
        }
        for registry_entry in er.async_entries_for_config_entry(entity_registry, self.entry.entry_id):
            unique_id = registry_entry.unique_id or ""
            for kind, prefix in (("light", f"{ctrl}_ecg_"), ("fan", f"{ctrl}_fan_"), ("blind", f"{ctrl}_blind_")):
                if not unique_id.startswith(prefix):
                    continue
                try:
                    number = int(unique_id[len(prefix) :])
                except ValueError:
                    break
                current = kind_for_number.get(number)
                if current is not None and current != kind:
                    entity_registry.async_remove(registry_entry.entity_id)
                break


    async def _async_notify_discovery_best_effort(self) -> None:
        """Notify platforms after a failed start without masking the error."""
        try:
            await self._notify_discovery_complete()
        except Exception:
            _LOGGER.debug(
                "Discovery notify after setup failure failed",
                exc_info=True,
            )

    async def _notify_discovery_complete(self) -> None:
        """Run platform discovery callbacks and await entity-adds they schedule.

        Platform async_add_entities is synchronous and only schedules work
        via ConfigEntry.async_create_task. We await those new entry tasks
        only — never hass.async_block_till_done(), which deadlocks when
        CREATE_ENTRY is awaiting setup.
        """
        if self._discovery_notified:
            return
        self._discovery_notified = True
        self._discovery_complete = True

        callbacks = self._discovery_callbacks
        self._discovery_callbacks = []
        if not callbacks:
            return

        before = set(self._entry_tracked_tasks())
        for callback in callbacks:
            await callback()
        await self._async_await_new_entry_tasks(before, what="platform entity add")

    async def async_stop(self) -> None:
        """Detach this entry from the shared runtime."""
        if self.stopping:
            return
        self._stopping = True
        self._setup_complete = False
        self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)
        if not self._attached:
            return
        self._attached = False
        await self.runtime.async_detach(self.entry.entry_id)

    # ------------------------------------------------------------------
    # Runtime → hub event handlers
    # ------------------------------------------------------------------

    async def handle_listener_connect(self) -> None:
        """Shared listener came up — probe ready; do not assume online."""
        if self.controller is not None:
            try:
                ready = await asyncio.wait_for(
                    self.controller.commands.query_controller_startup_complete(self.controller),
                    timeout=CONTROLLER_READY_QUERY_TIMEOUT,
                )
            except TimeoutError:
                ready = None
            if ready is True:
                # async_start owns the first online transition so entities stay
                # unavailable until discovery + event configure finish.
                if self._setup_complete:
                    self.set_controller_status(CONTROLLER_STATUS_ONLINE)
                    if not self.stopping:
                        await self._refresh_light_states()
                else:
                    self.set_controller_status(CONTROLLER_STATUS_STARTING)
            elif ready is False:
                self.set_controller_status(CONTROLLER_STATUS_STARTING)
            else:
                self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)
        else:
            self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)

    def handle_listener_disconnect(self) -> None:
        """Shared listener went down."""
        self.set_controller_status(CONTROLLER_STATUS_UNREACHABLE)

    async def handle_listener_resync(self) -> None:
        """Shared listener restored after a recoverable gap — refresh entity state."""
        if not self._setup_complete or self.stopping:
            return
        if self._controller_status != CONTROLLER_STATUS_ONLINE:
            return
        await self._refresh_light_states()

    async def handle_controller_status(self, status: str) -> None:
        """Keepalive / library reported online, starting, or unreachable."""
        # Ignore premature "online" until successful async_start finishes.
        if status == CONTROLLER_STATUS_ONLINE and not self._setup_complete:
            return
        was_online = self._controller_status == CONTROLLER_STATUS_ONLINE
        self.set_controller_status(status)
        if status == CONTROLLER_STATUS_ONLINE and not was_online and self._setup_complete and not self.stopping:
            await self._refresh_light_states()

    def _write_entity_states(self) -> None:
        """Push current state (including availability) for all registered entities."""
        for bound in self._entities.values():
            if bound.entity.entity_id:
                bound.entity.async_write_ha_state()

    def handle_light_change(self, light: ZenLight) -> None:
        entity = self._entity(light_assignment_key(light))
        if entity is not None:
            cast(Any, entity).update_state()

    def handle_fan_change(self, fan: ZenFan) -> None:
        entity = self._entity(fan_assignment_key(fan))
        if entity is not None:
            cast(Any, entity).update_state()

    def handle_blind_change(self, blind: ZenBlind) -> None:
        entity = self._entity(blind_assignment_key(blind))
        if entity is not None:
            cast(Any, entity).update_state()

    def handle_group_change(self, group: ZenGroup) -> None:
        group_entity = self._entity(group_assignment_key(group))
        if group_entity is not None:
            cast(Any, group_entity).update_state()
        scene_select = self._entity(_scene_select_key(group))
        if scene_select is not None:
            cast(Any, scene_select).update_current_option()

    def handle_button_press(self, button: ZenButton) -> None:
        entity = self._entity(button_assignment_key(button))
        if entity is not None:
            cast(Any, entity).trigger_event("short_press")

    def handle_button_long_press(self, button: ZenButton) -> None:
        entity = self._entity(button_assignment_key(button))
        if entity is not None:
            cast(Any, entity).trigger_event("long_press")

    def handle_motion_event(self, sensor: ZenMotionSensor) -> None:
        entity = self._entity(motion_assignment_key(sensor))
        if entity is not None:
            cast(Any, entity).update_occupied()

    def handle_absolute_input_change(self, absolute_input: ZenAbsoluteInput) -> None:
        entity = self._entity(absolute_input_assignment_key(absolute_input))
        if entity is not None:
            cast(Any, entity).update_value()

    def handle_sv_change(
        self,
        system_variable: ZenSystemVariable,
        *,
        by_me: bool,
    ) -> None:
        sensor_entity = self._entity(_sv_sensor_key(system_variable))
        if sensor_entity is not None:
            cast(Any, sensor_entity).update_value()
        if by_me:
            return
        switch_entity = self._entity(_sv_switch_key(system_variable))
        if switch_entity is not None:
            cast(Any, switch_entity).update_value()

    def handle_profile_change(self, profile: ZenProfile) -> None:
        entity = self._entity(_profile_key(profile.controller.name))
        if entity is not None:
            cast(Any, entity).update_current_option()


type ZencontrolTpiConfigEntry = ConfigEntry[ZenHub]
