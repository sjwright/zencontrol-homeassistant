"""Label-prefix sub-devices for HA child devices under a controller.

Lifecycle (owned by ZenHub.sync_device_assignments):

1. Config: per-controller ``sub_devices`` list in the config entry
2. Assign: group-first, then longest label-prefix match (build_assignments)
3. Sync: create/update devices + areas, move entities, prune orphans

Sub-device CRUD in options persists config and calls sync without reload.
Controller add/remove reloads, then rediscovery + sync.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .const import CONF_SUB_DEVICES

_LOGGER = logging.getLogger(__name__)

# Sub-device definition keys inside each controller's sub_devices list
CONF_SUB_DEVICE_ID = "id"
CONF_SUB_DEVICE_NAME = "name"
CONF_SUB_DEVICE_PREFIXES = "prefixes"
CONF_SUB_DEVICE_AREA_ID = "area_id"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SubDeviceDef:
    """One user-defined sub-device on a controller."""

    id: str
    name: str
    prefixes: tuple[str, ...]
    area_id: str | None = None


def slugify_sub_device_id(name: str) -> str:
    """Stable id from the sub-device display name (first alias)."""
    slug = _SLUG_RE.sub("_", name.casefold()).strip("_")
    return slug or "sub_device"


def parse_sub_device_prefixes(raw: str) -> list[str]:
    """Split a comma-separated prefix list; strip whitespace around each part."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def sub_device_from_prefixes(prefixes: list[str]) -> SubDeviceDef | None:
    """Build a sub-device from aliases; name/id come from the first alias."""
    if not prefixes:
        return None
    name = prefixes[0]
    return SubDeviceDef(
        id=slugify_sub_device_id(name),
        name=name,
        prefixes=tuple(prefixes),
    )


def sub_devices_from_config(raw: Any) -> list[SubDeviceDef]:
    """Load sub-device defs from persisted controller config."""
    if not raw or not isinstance(raw, list):
        return []
    out: list[SubDeviceDef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        prefixes = item.get(CONF_SUB_DEVICE_PREFIXES) or []
        if isinstance(prefixes, str):
            prefixes = parse_sub_device_prefixes(prefixes)
        if not prefixes:
            continue
        cid = item.get(CONF_SUB_DEVICE_ID) or slugify_sub_device_id(str(prefixes[0]))
        name = item.get(CONF_SUB_DEVICE_NAME) or prefixes[0]
        area_raw = item.get(CONF_SUB_DEVICE_AREA_ID)
        area_id = str(area_raw) if area_raw else None
        out.append(
            SubDeviceDef(
                id=str(cid),
                name=str(name),
                prefixes=tuple(str(p) for p in prefixes),
                area_id=area_id,
            )
        )
    return out


def sub_devices_from_controller(ctrl_cfg: dict[str, Any]) -> list[SubDeviceDef]:
    """Load sub-devices from a controller dict."""
    return sub_devices_from_config(ctrl_cfg.get(CONF_SUB_DEVICES))


def sub_devices_to_config(sub_devices: list[SubDeviceDef]) -> list[dict[str, Any]]:
    """Serialize sub-devices for config entry storage."""
    result: list[dict[str, Any]] = []
    for d in sub_devices:
        item: dict[str, Any] = {
            CONF_SUB_DEVICE_ID: d.id,
            CONF_SUB_DEVICE_NAME: d.name,
            CONF_SUB_DEVICE_PREFIXES: list(d.prefixes),
        }
        if d.area_id:
            item[CONF_SUB_DEVICE_AREA_ID] = d.area_id
        result.append(item)
    return result


def validate_sub_device_prefixes(
    existing: list[SubDeviceDef],
    new_prefixes: list[str],
    *,
    replacing_id: str | None = None,
) -> str | None:
    """Return an error key if new prefixes conflict; else None.

    Conflicts: empty list, or a prefix (casefold) already used by another sub-device.
    """
    if not new_prefixes:
        return "empty_prefixes"

    claimed: dict[str, str] = {}
    for device in existing:
        if replacing_id is not None and device.id == replacing_id:
            continue
        for prefix in device.prefixes:
            claimed[prefix.casefold()] = device.id

    for prefix in new_prefixes:
        key = prefix.casefold()
        if key in claimed:
            return "duplicate_prefix"
    return None


def prefix_matches(label: str, prefix: str) -> bool:
    """Case-insensitive prefix match with a word-boundary after the prefix.

    Matches when the label equals the prefix, or the next character is not
    alphanumeric (space, colon, and other word separators).
    """
    if not prefix or not label:
        return False
    folded_label = label.casefold()
    folded_prefix = prefix.casefold()
    if not folded_label.startswith(folded_prefix):
        return False
    if len(folded_label) == len(folded_prefix):
        return True
    return not folded_label[len(folded_prefix)].isalnum()


def match_sub_device(label: str, sub_devices: list[SubDeviceDef]) -> SubDeviceDef | None:
    """Return the sub-device with the longest matching prefix, or None.

    Tie-break when two prefixes share the same length: the first match in
    config order (sub-device list, then that device's prefix tuple) wins.
    """
    label = (label or "").strip()
    if not label or not sub_devices:
        return None

    best: SubDeviceDef | None = None
    best_len = -1
    for device in sub_devices:
        for prefix in device.prefixes:
            if prefix_matches(label, prefix) and len(prefix) > best_len:
                best = device
                best_len = len(prefix)
    return best


def match_label_for_light(light: Any) -> str:
    """Label used for light name matching (sub_label preferred)."""
    return (getattr(light, "sub_label", None) or getattr(light, "label", None) or "").strip()


def match_label_for_group(group: Any) -> str:
    """Label used for group matching."""
    return (getattr(group, "label", None) or "").strip()


def match_label_for_button(button: Any) -> str:
    """Same preference as the event entity display name."""
    instance_label = getattr(button, "instance_label", None)
    label = getattr(button, "label", None)
    if instance_label and instance_label != label:
        return str(instance_label).strip()
    return (label or "").strip()


def match_label_for_motion(sensor: Any) -> str:
    """Same preference as the motion entity display name."""
    instance_label = getattr(sensor, "instance_label", None)
    label = getattr(sensor, "label", None)
    if instance_label and instance_label != label:
        return str(instance_label).strip()
    return (label or "").strip()


def match_label_for_sysvar(sv: Any) -> str:
    """Label used for system variable matching."""
    return (getattr(sv, "label", None) or "").strip()


def light_assignment_key(light: Any) -> str:
    ctrl = light.address.controller
    return f"light:{ctrl.name}:{light.address.number}"


def group_assignment_key(group: Any) -> str:
    ctrl = group.address.controller
    return f"group:{ctrl.name}:{group.address.number}"


def button_assignment_key(button: Any) -> str:
    ctrl = button.instance.address.controller
    addr = button.instance.address.number
    inst = button.instance.number
    return f"button:{ctrl.name}:{addr}:{inst}"


def motion_assignment_key(sensor: Any) -> str:
    ctrl = sensor.instance.address.controller
    addr = sensor.instance.address.number
    inst = sensor.instance.number
    return f"motion:{ctrl.name}:{addr}:{inst}"


def sysvar_assignment_key(sv: Any) -> str:
    return f"sv:{sv.controller.name}:{sv.id}"


def build_assignments(
    *,
    controller_sub_devices: dict[str, list[SubDeviceDef]],
    lights: list[Any],
    groups: list[Any],
    buttons: list[Any],
    motion_sensors: list[Any],
    sysvars: list[Any],
) -> dict[str, str]:
    """Compute assignment key → sub-device id.

    Groups are matched first (lowest address number wins when a light sits in
    multiple matched groups); member lights inherit that sub-device and are not
    name-matched. Remaining lights and other entities use longest-prefix name
    matching. Profile entities are never assigned here (always parent).
    """
    assignments: dict[str, str] = {}
    lights_claimed: set[str] = set()

    # Groups: lowest address number first so "first matched group" is stable
    # when a light sits in multiple matched groups.
    sorted_groups = sorted(
        groups,
        key=lambda g: (g.address.controller.name, g.address.number),
    )
    for group in sorted_groups:
        ctrl_name = group.address.controller.name
        devices = controller_sub_devices.get(ctrl_name) or []
        if not devices:
            continue
        matched = match_sub_device(match_label_for_group(group), devices)
        if matched is None:
            continue
        gkey = group_assignment_key(group)
        assignments[gkey] = matched.id
        for light in getattr(group, "lights", None) or []:
            lkey = light_assignment_key(light)
            if lkey in lights_claimed:
                _LOGGER.debug(
                    "Light %s is in multiple matched groups; "
                    "keeping sub-device from first group",
                    lkey,
                )
                continue
            assignments[lkey] = matched.id
            lights_claimed.add(lkey)

    for light in lights:
        lkey = light_assignment_key(light)
        if lkey in lights_claimed:
            continue
        ctrl_name = light.address.controller.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_light(light), devices)
        if matched is not None:
            assignments[lkey] = matched.id

    for button in buttons:
        ctrl_name = button.instance.address.controller.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_button(button), devices)
        if matched is not None:
            assignments[button_assignment_key(button)] = matched.id

    for sensor in motion_sensors:
        ctrl_name = sensor.instance.address.controller.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_motion(sensor), devices)
        if matched is not None:
            assignments[motion_assignment_key(sensor)] = matched.id

    for sv in sysvars:
        ctrl_name = sv.controller.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_sysvar(sv), devices)
        if matched is not None:
            assignments[sysvar_assignment_key(sv)] = matched.id

    return assignments
