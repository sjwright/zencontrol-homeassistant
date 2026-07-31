"""Persisted entity manifest for fast restarts without full bus discovery."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from zencontrol import (
    ZenAbsoluteInput,
    ZenAddress,
    ZenAddressType,
    ZenBlind,
    ZenButton,
    ZenController,
    ZenFan,
    ZenGroup,
    ZenInstance,
    ZenInstanceType,
    ZenLight,
    ZenMotionSensor,
    ZenProfile,
    ZenSystemVariable,
)

from .const import DOMAIN

if TYPE_CHECKING:
    from .hub import ZenHub

_LOGGER = logging.getLogger(__name__)

# HA Store internal version for `helpers.storage.Store`.
# Keep this at 1 unless you also implement an explicit migration function.
STORE_VERSION = 1

# Schema version embedded into the manifest payload we store.
# Bump this when the structure of `manifest["interview"]` changes.
MANIFEST_VERSION = 7


class Interviewable(Protocol):
    """A Zen entity that can round-trip its interview state."""

    def interview_serialize(self) -> str: ...

    def interview_hydrate(self, data: str | dict[str, Any]) -> bool: ...

    async def interview(self) -> bool: ...


class ManifestEntitySource(Protocol):
    """Entity lists accepted by build_manifest (ZenHub or discovery result)."""

    lights: list[ZenLight]
    fans: list[ZenFan]
    blinds: list[ZenBlind]
    groups: list[ZenGroup]
    buttons: list[ZenButton]
    motion_sensors: list[ZenMotionSensor]
    absolute_inputs: list[ZenAbsoluteInput]
    sv_switches: list[ZenSystemVariable]
    sv_sensors: list[ZenSystemVariable]
    profiles: list[ZenProfile]


class DiscoveryManifestStore:
    """Load/save discovered entity keys per config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry_id}.manifest",
        )

    async def async_load(self) -> dict[str, Any] | None:
        """Return saved manifest or None.

        Returns None if the manifest is missing, corrupt, or was written by a
        different schema version so the caller falls back to full discovery.
        """
        try:
            data = await self._store.async_load()
        except NotImplementedError:
            # HA's Store base class raises NotImplementedError when it wants to
            # migrate storage between versions but no migration function is
            # implemented. Treat this as "no cached manifest" so we can fall
            # back to full discovery.
            _LOGGER.warning("Cached manifest store migration not implemented; ignoring cache")
            return None
        if not isinstance(data, dict):
            return None
        if data.get("version") != MANIFEST_VERSION:
            return None
        return data

    async def async_save(self, manifest: dict[str, Any]) -> None:
        """Persist manifest."""
        await self._store.async_save(manifest)

    async def async_remove(self) -> None:
        """Delete persisted manifest."""
        await self._store.async_remove()


def _interview_blob(obj: Interviewable) -> dict[str, Any]:
    """Return interview_serialize() parsed as a dict for Store JSON."""
    return json.loads(obj.interview_serialize())


async def _hydrate_or_interview(obj: Interviewable, interview: str | dict[str, Any] | None) -> bool:
    """Apply interview_hydrate; fall back to a live interview on failure.

    Returns True when we had to run `interview()` (so the manifest is now
    stale and should be re-saved).
    """
    if interview is not None and obj.interview_hydrate(interview):
        return False
    _LOGGER.debug("Interview hydrate failed for %s; interviewing", obj)
    await obj.interview()
    return True


def build_manifest(source: ManifestEntitySource) -> dict[str, Any]:
    """Serialize discovered entities after full discovery."""
    hub = source
    lights = [
        {
            "controller": lt.address.controller.name,
            "number": lt.address.number,
            "interview": _interview_blob(lt),
        }
        for lt in hub.lights
    ]
    fans = [
        {
            "controller": f.address.controller.name,
            "number": f.address.number,
            "interview": _interview_blob(f),
        }
        for f in hub.fans
    ]
    blinds = [
        {
            "controller": b.address.controller.name,
            "number": b.address.number,
            "interview": _interview_blob(b),
        }
        for b in hub.blinds
    ]
    groups = [
        {
            "controller": g.address.controller.name,
            "number": g.address.number,
            "interview": _interview_blob(g),
        }
        for g in hub.groups
    ]
    buttons = [
        {
            "controller": b.instance.address.controller.name,
            "address": b.instance.address.number,
            "instance": b.instance.number,
            "interview": _interview_blob(b),
        }
        for b in hub.buttons
    ]
    motion_sensors = [
        {
            "controller": s.instance.address.controller.name,
            "address": s.instance.address.number,
            "instance": s.instance.number,
            "interview": _interview_blob(s),
        }
        for s in hub.motion_sensors
    ]
    absolute_inputs = [
        {
            "controller": a.instance.address.controller.name,
            "address": a.instance.address.number,
            "instance": a.instance.number,
            "interview": _interview_blob(a),
        }
        for a in hub.absolute_inputs
    ]
    sysvars: list[dict[str, Any]] = []
    seen_sv: set[tuple[str, int]] = set()
    for sv in (*hub.sv_switches, *hub.sv_sensors):
        key = (sv.controller.name, sv.id)
        if key in seen_sv:
            continue
        seen_sv.add(key)
        lower = (sv.label or "").casefold()
        as_sensor, as_switch = "sensor" in lower, "switch" in lower
        sysvars.append(
            {
                "controller": sv.controller.name,
                "id": sv.id,
                "as_sensor": as_sensor,
                "as_switch": as_switch,
                "interview": _interview_blob(sv),
            }
        )
    profiles = [
        {
            "controller": p.controller.name,
            "number": p.number,
            "interview": _interview_blob(p),
        }
        for p in hub.profiles
    ]

    return {
        "version": MANIFEST_VERSION,
        "lights": lights,
        "fans": fans,
        "blinds": blinds,
        "groups": groups,
        "buttons": buttons,
        "motion_sensors": motion_sensors,
        "absolute_inputs": absolute_inputs,
        "sysvars": sysvars,
        "profiles": profiles,
    }


async def load_entities_from_manifest(hub: ZenHub, manifest: dict[str, Any]) -> bool:
    """Rebuild hub entity lists from a saved interview manifest.

    Lights/fans/blinds must be hydrated before groups so interview_hydrate can
    populate group membership on the group singletons. Controllers are already
    interviewed by the hub.
    """
    ctrl_by_name = {hub.controller.name: hub.controller} if hub.controller is not None else {}
    ctx = hub.zen.context
    needs_save = False

    def _ctrl(name: str) -> ZenController:
        if name not in ctrl_by_name:
            raise KeyError(f"Manifest references unknown controller {name!r}")
        return ctrl_by_name[name]

    # ECG kinds first: hydrate rebuilds group membership links.
    hub.lights = []
    for item in manifest.get("lights", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECG, number=item["number"])
        light = ctx.light(addr)
        if await _hydrate_or_interview(light, item.get("interview")):
            needs_save = True
        hub.lights.append(light)

    hub.fans = []
    for item in manifest.get("fans", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECG, number=item["number"])
        fan = ctx.fan(addr)
        if await _hydrate_or_interview(fan, item.get("interview")):
            needs_save = True
        hub.fans.append(fan)

    hub.blinds = []
    for item in manifest.get("blinds", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECG, number=item["number"])
        blind = ctx.blind(addr)
        if await _hydrate_or_interview(blind, item.get("interview")):
            needs_save = True
        hub.blinds.append(blind)

    hub.groups = []
    for item in manifest.get("groups", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.GROUP, number=item["number"])
        group = ctx.group(addr)
        if await _hydrate_or_interview(group, item.get("interview")):
            needs_save = True
        hub.groups.append(group)

    hub.buttons = []
    for item in manifest.get("buttons", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECD, number=item["address"])
        instance = ZenInstance(
            address=addr,
            type=ZenInstanceType.PUSH_BUTTON,
            number=item["instance"],
        )
        button = ctx.button(instance)
        if await _hydrate_or_interview(button, item.get("interview")):
            needs_save = True
        hub.buttons.append(button)

    hub.motion_sensors = []
    for item in manifest.get("motion_sensors", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECD, number=item["address"])
        instance = ZenInstance(
            address=addr,
            type=ZenInstanceType.OCCUPANCY_SENSOR,
            number=item["instance"],
        )
        sensor = ctx.motion_sensor(instance)
        if await _hydrate_or_interview(sensor, item.get("interview")):
            needs_save = True
        hub.motion_sensors.append(sensor)

    hub.absolute_inputs = []
    for item in manifest.get("absolute_inputs", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(controller=ctrl, type=ZenAddressType.ECD, number=item["address"])
        instance = ZenInstance(
            address=addr,
            type=ZenInstanceType.ABSOLUTE_INPUT,
            number=item["instance"],
        )
        absolute_input = ctx.absolute_input(instance)
        if await _hydrate_or_interview(absolute_input, item.get("interview")):
            needs_save = True
        hub.absolute_inputs.append(absolute_input)

    hub.sv_switches = []
    hub.sv_sensors = []
    for item in manifest.get("sysvars", []):
        ctrl = _ctrl(item["controller"])
        sv = ctx.system_variable(ctrl, item["id"])
        if await _hydrate_or_interview(sv, item.get("interview")):
            needs_save = True
        as_sensor = item.get("as_sensor")
        as_switch = item.get("as_switch")
        if as_sensor is None or as_switch is None:
            lower = (sv.label or "").casefold()
            as_sensor, as_switch = "sensor" in lower, "switch" in lower
        if as_switch:
            hub.sv_switches.append(sv)
        if as_sensor:
            hub.sv_sensors.append(sv)

    hub.profiles = []
    for item in manifest.get("profiles", []):
        ctrl = _ctrl(item["controller"])
        profile = ctx.profile(ctrl, item["number"])
        if await _hydrate_or_interview(profile, item.get("interview")):
            needs_save = True
        hub.profiles.append(profile)

    hub.lights.sort(key=lambda lt: lt.address.number)
    hub.fans.sort(key=lambda f: f.address.number)
    hub.blinds.sort(key=lambda b: b.address.number)
    hub.groups.sort(key=lambda g: g.address.number)
    hub.buttons.sort(key=lambda b: (b.instance.address.number, b.instance.number))
    hub.motion_sensors.sort(key=lambda s: (s.instance.address.number, s.instance.number))
    hub.absolute_inputs.sort(key=lambda a: (a.instance.address.number, a.instance.number))
    hub.profiles.sort(key=lambda p: (p.controller.name, p.number))
    return needs_save
