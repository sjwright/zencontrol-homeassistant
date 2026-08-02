"""Constants for the zencontrol-tpi integration."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final, NotRequired, TypedDict, cast

from homeassistant.const import Platform

# Legacy HA domain - must remain "zencontrol_tpi" (and match manifest.json
# "domain" + custom_components/zencontrol_tpi/) so existing installs keep working.
DOMAIN: Final = "zencontrol_tpi"

# hass.data[DOMAIN] key for a manifest built during config-flow progress
DATA_PENDING_MANIFEST: Final = "pending_manifest"

DEFAULT_PORT: Final = 5108

# Controller boot can take 1-10 minutes after power-on / reboot. Setup and
# config-flow priming poll query_controller_startup_complete until this deadline.
CONTROLLER_READY_POLL_INTERVAL: Final = 10  # seconds between polls
CONTROLLER_READY_QUERY_TIMEOUT: Final = 10.0
CONTROLLER_READY_WAIT_MAX: Final = 600.0  # 10 minutes

# Diagnostic controller runtime status (HA has no native "rebooting" device state).
CONTROLLER_STATUS_ONLINE: Final = "online"
CONTROLLER_STATUS_STARTING: Final = "starting"
CONTROLLER_STATUS_UNREACHABLE: Final = "unreachable"
CONTROLLER_STATUS_OPTIONS: Final = (
    CONTROLLER_STATUS_ONLINE,
    CONTROLLER_STATUS_STARTING,
    CONTROLLER_STATUS_UNREACHABLE,
)

PLATFORMS: Final = [
    Platform.LIGHT,
    Platform.FAN,
    Platform.COVER,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SCENE,
    Platform.EVENT,
]

# Config entry keys
CONF_CONTROLLERS: Final = "controllers"
CONF_MAC: Final = "mac"
CONF_LABEL: Final = "label"
CONF_NAME: Final = "name"
CONF_UNICAST: Final = "unicast"
# Per-controller label-prefix sub-devices (see sub_devices.py)
CONF_SUB_DEVICES: Final = "sub_devices"

# Group scene select options
SCENE_OFF: Final = "Off"
SCENE_NONE: Final = "None"

# Logarithmic arc↔brightness constants
_LOG_A: Final = -59.53
_LOG_B: Final = 56.58

# Config entry version after one-controller-per-entry migration
CONFIG_VERSION: Final = 2


class SubDeviceConfig(TypedDict):
    """One label-prefix sub-device persisted under a controller."""

    id: str
    name: str
    prefixes: list[str]
    area_id: NotRequired[str]


class ControllerConfig(TypedDict):
    """Persisted per-controller config (one entry → one controller)."""

    host: str
    port: int
    mac: str
    name: str
    label: str
    sub_devices: NotRequired[list[SubDeviceConfig]]


class DiscoveredControllerInfo(TypedDict):
    """Multicast/runtime discovery hit before a unique controller name exists."""

    host: str
    port: int
    mac: str
    label: str


class EntryData(TypedDict):
    """Config entry data for a single-controller entry."""

    controllers: list[ControllerConfig]
    unicast: bool


def normalize_mac(mac: str) -> str:
    """Normalize MAC to uppercase colon-separated format."""
    return mac.upper().replace("-", ":").strip()


def normalize_mac_id(mac: str) -> str:
    """Return MAC without separators for unique-id comparisons."""
    return normalize_mac(mac).replace(":", "")


def controllers_from_entry_data(data: Mapping[str, Any]) -> list[ControllerConfig]:
    """Return controller configs from entry data (empty if missing/invalid)."""
    raw = data.get(CONF_CONTROLLERS)
    if not isinstance(raw, list):
        return []
    return [cast(ControllerConfig, item) for item in raw if isinstance(item, dict)]


def controller_from_entry_data(data: Mapping[str, Any]) -> ControllerConfig | None:
    """Return the single controller config from entry data.

    Accepts a Mapping because ConfigEntry.data is a read-only
    MappingProxyType.
    """
    controllers = controllers_from_entry_data(data)
    return controllers[0] if controllers else None


def entry_data_for_controller(ctrl_cfg: ControllerConfig, *, unicast: bool = False) -> EntryData:
    """Build persisted entry data for a single controller."""
    return {
        CONF_CONTROLLERS: [ctrl_cfg],
        CONF_UNICAST: unicast,
    }


def arc_to_brightness(arc: int) -> int:
    """Convert DALI arc level (0-254) to HA brightness (0-255)."""
    if arc <= 0:
        return 0
    return min(255, round(math.exp((arc - _LOG_A) / _LOG_B)))


def brightness_to_arc(brightness: int) -> int:
    """Convert HA brightness (0-255) to DALI arc level (0-254)."""
    if brightness <= 0:
        return 0
    return min(254, max(0, round(_LOG_A + _LOG_B * math.log(brightness))))
