"""Constants for the zencontrol-tpi integration."""

from __future__ import annotations

import math
from typing import Final

from homeassistant.const import Platform

# Legacy HA domain — must remain "zencontrol_tpi" (and match manifest.json
# "domain" + custom_components/zencontrol_tpi/) so existing installs keep working.
DOMAIN: Final = "zencontrol_tpi"

# hass.data[DOMAIN] key for a manifest built during config-flow progress
DATA_PENDING_MANIFEST: Final = "pending_manifest"

DEFAULT_PORT: Final = 5108

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
