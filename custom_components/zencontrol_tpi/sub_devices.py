"""Label-prefix sub-devices for HA child devices under a controller.

Lifecycle (owned by ZenHub.sync_device_assignments):

1. Config: per-controller sub_devices list in the config entry
2. Assign: group-first, then longest label-prefix match (build_assignments)
3. Sync: create/update devices + areas, move entities, prune orphans

Sub-device CRUD in options persists config and calls sync without reload.
Controller add/remove reloads, then rediscovery + sync.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from zencontrol import (
    ZenAbsoluteInput,
    ZenBlind,
    ZenButton,
    ZenFan,
    ZenGroup,
    ZenLight,
    ZenMotionSensor,
    ZenSystemVariable,
)

from .const import CONF_SUB_DEVICES

_LOGGER = logging.getLogger(__name__)

# Sub-device definition keys inside each controller's sub_devices list
CONF_SUB_DEVICE_ID = "id"
CONF_SUB_DEVICE_NAME = "name"
CONF_SUB_DEVICE_PREFIXES = "prefixes"
CONF_SUB_DEVICE_AREA_ID = "area_id"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

type SubDevicePrefixError = Literal["empty_prefixes", "duplicate_prefix"]

# DALI ECD instance entities, which all carry a device label plus an
# instance label.
type ZenInstanceEntity = ZenButton | ZenMotionSensor | ZenAbsoluteInput


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
) -> SubDevicePrefixError | None:
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


def match_sub_device(
    label: str, sub_devices: list[SubDeviceDef]
) -> SubDeviceDef | None:
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


def instance_display_label(entity: ZenInstanceEntity) -> str:
    """Display label for an ECD instance entity.

    Prefer the instance label, which is what users set per button or sensor,
    and fall back to the device label. Entity display names and sub-device
    prefix matching must agree, so both use this.
    """
    if entity.instance_label and entity.instance_label != entity.label:
        return entity.instance_label.strip()
    return (entity.label or "").strip()


def match_label_for_light(light: ZenLight) -> str:
    """Label used for light name matching (sub_label preferred)."""
    return (light.sub_label or light.label or "").strip()


def match_label_for_fan(fan: ZenFan) -> str:
    """Label used for fan name matching."""
    return (fan.label or "").strip()


def match_label_for_blind(blind: ZenBlind) -> str:
    """Label used for blind name matching."""
    return (blind.label or "").strip()


def match_label_for_group(group: ZenGroup) -> str:
    """Label used for group matching."""
    return (group.label or "").strip()


def match_label_for_button(button: ZenButton) -> str:
    """Same preference as the event entity display name."""
    return instance_display_label(button)


def match_label_for_motion(sensor: ZenMotionSensor) -> str:
    """Same preference as the motion entity display name."""
    return instance_display_label(sensor)


def match_label_for_absolute_input(absolute_input: ZenAbsoluteInput) -> str:
    """Same preference as the absolute-input sensor display name."""
    return instance_display_label(absolute_input)


def match_label_for_sysvar(sv: ZenSystemVariable) -> str:
    """Label used for system variable matching."""
    return (sv.label or "").strip()


def light_assignment_key(light: ZenLight) -> str:
    ctrl = light.address.ctrl
    return f"light:{ctrl.name}:{light.address.number}"


def fan_assignment_key(fan: ZenFan) -> str:
    ctrl = fan.address.ctrl
    return f"fan:{ctrl.name}:{fan.address.number}"


def blind_assignment_key(blind: ZenBlind) -> str:
    ctrl = blind.address.ctrl
    return f"blind:{ctrl.name}:{blind.address.number}"


def group_assignment_key(group: ZenGroup) -> str:
    ctrl = group.address.ctrl
    return f"group:{ctrl.name}:{group.address.number}"


def button_assignment_key(button: ZenButton) -> str:
    ctrl = button.instance.address.ctrl
    addr = button.instance.address.number
    inst = button.instance.number
    return f"button:{ctrl.name}:{addr}:{inst}"


def motion_assignment_key(sensor: ZenMotionSensor) -> str:
    ctrl = sensor.instance.address.ctrl
    addr = sensor.instance.address.number
    inst = sensor.instance.number
    return f"motion:{ctrl.name}:{addr}:{inst}"


def absolute_input_assignment_key(absolute_input: ZenAbsoluteInput) -> str:
    ctrl = absolute_input.instance.address.ctrl
    addr = absolute_input.instance.address.number
    inst = absolute_input.instance.number
    return f"absolute:{ctrl.name}:{addr}:{inst}"


def sysvar_assignment_key(sv: ZenSystemVariable) -> str:
    return f"sv:{sv.ctrl.name}:{sv.id}"


def build_assignments(
    *,
    controller_sub_devices: dict[str, list[SubDeviceDef]],
    lights: list[ZenLight],
    fans: list[ZenFan] | None = None,
    blinds: list[ZenBlind] | None = None,
    groups: list[ZenGroup],
    buttons: list[ZenButton],
    motion_sensors: list[ZenMotionSensor],
    absolute_inputs: list[ZenAbsoluteInput],
    sysvars: list[ZenSystemVariable],
) -> dict[str, str]:
    """Compute assignment key → sub-device id.

    Groups are matched first (lowest address number wins when a member sits in
    multiple matched groups); member lights/fans/blinds inherit that sub-device
    and are not name-matched. Remaining ECGs and other entities use
    longest-prefix name matching. Profile entities are never assigned here
    (always parent).
    """
    fans = fans or []
    blinds = blinds or []
    assignments: dict[str, str] = {}
    lights_claimed: set[str] = set()
    fans_claimed: set[str] = set()
    blinds_claimed: set[str] = set()

    # Groups: lowest address number first so "first matched group" is stable
    # when a member sits in multiple matched groups.
    sorted_groups = sorted(
        groups,
        key=lambda g: (g.address.ctrl.name, g.address.number),
    )
    for group in sorted_groups:
        ctrl_name = group.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        if not devices:
            continue
        matched = match_sub_device(match_label_for_group(group), devices)
        if matched is None:
            continue
        gkey = group_assignment_key(group)
        assignments[gkey] = matched.id
        for light in group.lights:
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
        for fan in getattr(group, "fans", ()) or ():
            fkey = fan_assignment_key(fan)
            if fkey in fans_claimed:
                continue
            assignments[fkey] = matched.id
            fans_claimed.add(fkey)
        for blind in getattr(group, "blinds", ()) or ():
            bkey = blind_assignment_key(blind)
            if bkey in blinds_claimed:
                continue
            assignments[bkey] = matched.id
            blinds_claimed.add(bkey)

    for light in lights:
        lkey = light_assignment_key(light)
        if lkey in lights_claimed:
            continue
        ctrl_name = light.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_light(light), devices)
        if matched is not None:
            assignments[lkey] = matched.id

    for fan in fans:
        fkey = fan_assignment_key(fan)
        if fkey in fans_claimed:
            continue
        ctrl_name = fan.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_fan(fan), devices)
        if matched is not None:
            assignments[fkey] = matched.id

    for blind in blinds:
        bkey = blind_assignment_key(blind)
        if bkey in blinds_claimed:
            continue
        ctrl_name = blind.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_blind(blind), devices)
        if matched is not None:
            assignments[bkey] = matched.id

    for button in buttons:
        ctrl_name = button.instance.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_button(button), devices)
        if matched is not None:
            assignments[button_assignment_key(button)] = matched.id

    for sensor in motion_sensors:
        ctrl_name = sensor.instance.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_motion(sensor), devices)
        if matched is not None:
            assignments[motion_assignment_key(sensor)] = matched.id

    for absolute_input in absolute_inputs:
        ctrl_name = absolute_input.instance.address.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(
            match_label_for_absolute_input(absolute_input), devices
        )
        if matched is not None:
            assignments[absolute_input_assignment_key(absolute_input)] = matched.id

    for sv in sysvars:
        ctrl_name = sv.ctrl.name
        devices = controller_sub_devices.get(ctrl_name) or []
        matched = match_sub_device(match_label_for_sysvar(sv), devices)
        if matched is not None:
            assignments[sysvar_assignment_key(sv)] = matched.id

    return assignments
