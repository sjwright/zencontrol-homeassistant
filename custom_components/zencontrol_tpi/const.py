"""Constants for the zencontrol-tpi integration."""

from __future__ import annotations

import math
from typing import Any, Final

from homeassistant.const import Platform

# Legacy HA domain — must remain "zencontrol_tpi" (and match manifest.json
# "domain" + custom_components/zencontrol_tpi/) so existing installs keep working.
DOMAIN: Final = "zencontrol_tpi"

# hass.data[DOMAIN] key for a manifest built during config-flow progress
DATA_PENDING_MANIFEST: Final = "pending_manifest"

DEFAULT_PORT: Final = 5108

# Controller boot can take 1–10 minutes after power-on / reboot. Setup and
# config-flow priming poll is_controller_ready() until this deadline.
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
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
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

# Group scene select when members are discoordinated (mqtt_bridge convention)
SCENE_NONE: Final = "None"

# Logarithmic arc↔brightness constants (from mqtt_bridge)
_LOG_A: Final = -59.53
_LOG_B: Final = 56.58

# Config entry version after one-controller-per-entry migration
CONFIG_VERSION: Final = 2


def normalize_mac(mac: str) -> str:
    """Normalize MAC to uppercase colon-separated format."""
    return mac.upper().replace("-", ":").strip()


def normalize_mac_id(mac: str) -> str:
    """Return MAC without separators for unique-id comparisons."""
    return normalize_mac(mac).replace(":", "")


def controller_from_entry_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single controller config from entry data."""
    controllers = data.get(CONF_CONTROLLERS)
    if isinstance(controllers, list) and controllers:
        first = controllers[0]
        return first if isinstance(first, dict) else None
    if data.get(CONF_MAC) and data.get("host"):
        return data
    return None


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
