"""Persisted entity manifest for fast restarts without full bus discovery."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from zencontrol import (  # type: ignore[import-untyped]
    ZenAddress,
    ZenAddressType,
    ZenButton,
    ZenGroup,
    ZenInstance,
    ZenInstanceType,
    ZenLight,
    ZenMotionSensor,
    ZenSystemVariable,
)

# ZenProfile in the top-level package is the api.models dataclass (no
# create()). The interface-layer singleton class must be imported directly.
from zencontrol.interface.interface import ZenProfile  # type: ignore[import-untyped]

from .const import DOMAIN
from .sysvar import classify_sysvar

_LOGGER = logging.getLogger(__name__)

# HA Store internal version for `helpers.storage.Store`.
# Keep this at 1 unless you also implement an explicit migration function.
STORE_VERSION = 1

# Schema version embedded into the manifest payload we store.
# Bump this when the structure of `manifest["interview"]` changes.
MANIFEST_VERSION = 2


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
            _LOGGER.warning(
                "Cached manifest store migration not implemented; ignoring cache"
            )
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


def _interview_blob(obj: Any) -> dict[str, Any]:
    """Return interview_serialize() parsed as a dict for Store JSON."""
    return json.loads(obj.interview_serialize())


async def _hydrate_or_interview(obj: Any, interview: Any) -> bool:
    """Apply interview_hydrate; fall back to a live interview on failure.

    Returns True when we had to run `interview()` (so the manifest is now
    stale and should be re-saved).
    """
    if interview is not None and obj.interview_hydrate(interview):
        return False
    _LOGGER.debug("Interview hydrate failed for %s; interviewing", obj)
    await obj.interview()
    return True


def build_manifest(hub: Any) -> dict[str, Any]:
    """Serialize discovered entities from a ZenHub after full discovery."""
    lights = [
        {
            "controller": lt.address.controller.name,
            "number": lt.address.number,
            "interview": _interview_blob(lt),
        }
        for lt in hub.lights
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
    sysvars: list[dict[str, Any]] = []
    seen_sv: set[tuple[str, int]] = set()
    for sv in (*hub.sv_switches, *hub.sv_sensors):
        key = (sv.controller.name, sv.id)
        if key in seen_sv:
            continue
        seen_sv.add(key)
        as_sensor, as_switch = classify_sysvar(sv.label)
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
        "groups": groups,
        "buttons": buttons,
        "motion_sensors": motion_sensors,
        "sysvars": sysvars,
        "profiles": profiles,
    }


async def load_entities_from_manifest(hub: Any, manifest: dict[str, Any]) -> bool:
    """Rebuild hub entity lists from a saved interview manifest.

    Lights must be hydrated before groups so ZenLight.interview_hydrate() can
    populate group.lights membership on the group singletons via
    group_membership. Controllers are already interviewed by the hub.
    """
    ctrl_by_name = {c.name: c for c in hub.controllers}
    protocol = hub.zen.protocol
    needs_save = False

    def _ctrl(name: str) -> Any:
        if name not in ctrl_by_name:
            raise KeyError(f"Manifest references unknown controller {name!r}")
        return ctrl_by_name[name]

    # Lights first: hydrate rebuilds light.groups and group.lights links.
    hub.lights = []
    for item in manifest.get("lights", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(
            controller=ctrl, type=ZenAddressType.ECG, number=item["number"]
        )
        light = ZenLight(protocol, addr)
        if await _hydrate_or_interview(light, item.get("interview")):
            needs_save = True
        hub.lights.append(light)

    hub.groups = []
    for item in manifest.get("groups", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(
            controller=ctrl, type=ZenAddressType.GROUP, number=item["number"]
        )
        group = ZenGroup(protocol, addr)
        if await _hydrate_or_interview(group, item.get("interview")):
            needs_save = True
        hub.groups.append(group)

    hub.buttons = []
    for item in manifest.get("buttons", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(
            controller=ctrl, type=ZenAddressType.ECD, number=item["address"]
        )
        instance = ZenInstance(
            address=addr,
            type=ZenInstanceType.PUSH_BUTTON,
            number=item["instance"],
        )
        button = ZenButton(protocol, instance)
        if await _hydrate_or_interview(button, item.get("interview")):
            needs_save = True
        hub.buttons.append(button)

    hub.motion_sensors = []
    for item in manifest.get("motion_sensors", []):
        ctrl = _ctrl(item["controller"])
        addr = ZenAddress(
            controller=ctrl, type=ZenAddressType.ECD, number=item["address"]
        )
        instance = ZenInstance(
            address=addr,
            type=ZenInstanceType.OCCUPANCY_SENSOR,
            number=item["instance"],
        )
        sensor = ZenMotionSensor(protocol, instance)
        if await _hydrate_or_interview(sensor, item.get("interview")):
            needs_save = True
        hub.motion_sensors.append(sensor)

    hub.sv_switches = []
    hub.sv_sensors = []
    for item in manifest.get("sysvars", []):
        ctrl = _ctrl(item["controller"])
        sv = ZenSystemVariable(protocol, ctrl, item["id"])
        if await _hydrate_or_interview(sv, item.get("interview")):
            needs_save = True
        as_sensor = item.get("as_sensor")
        as_switch = item.get("as_switch")
        if as_sensor is None or as_switch is None:
            as_sensor, as_switch = classify_sysvar(sv.label)
        if as_switch:
            hub.sv_switches.append(sv)
        if as_sensor:
            hub.sv_sensors.append(sv)

    hub.profiles = []
    for item in manifest.get("profiles", []):
        ctrl = _ctrl(item["controller"])
        profile = ZenProfile(protocol, ctrl, item["number"])
        if await _hydrate_or_interview(profile, item.get("interview")):
            needs_save = True
        hub.profiles.append(profile)

    hub.lights.sort(key=lambda lt: lt.address.number)
    hub.groups.sort(key=lambda g: g.address.number)
    hub.buttons.sort(
        key=lambda b: (b.instance.address.number, b.instance.number)
    )
    hub.motion_sensors.sort(
        key=lambda s: (s.instance.address.number, s.instance.number)
    )
    hub.profiles.sort(key=lambda p: (p.controller.name, p.number))
    return needs_save
