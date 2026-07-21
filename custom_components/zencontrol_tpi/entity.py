"""Shared entity helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


def controller_identifier(zen_ctrl: Any) -> tuple[str, str]:
    """Stable parent-device identifier for a controller."""
    return (DOMAIN, zen_ctrl.mac or zen_ctrl.name)


def controller_device_info(zen_ctrl: Any) -> DeviceInfo:
    """Build DeviceInfo for a Zen controller (hub / parent device)."""
    return DeviceInfo(
        identifiers={controller_identifier(zen_ctrl)},
        name=zen_ctrl.label,
        manufacturer="ZenControl",
        model="TPI Controller",
        sw_version=str(zen_ctrl.version) if zen_ctrl.version is not None else None,
    )


def sub_device_device_info(
    zen_ctrl: Any,
    *,
    sub_device_id: str,
    sub_device_name: str,
) -> DeviceInfo:
    """Build DeviceInfo for a label-prefix child device under a controller."""
    parent = controller_identifier(zen_ctrl)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{parent[1]}:sub:{sub_device_id}")},
        name=sub_device_name,
        manufacturer="ZenControl",
        model="Sub-device",
        via_device=parent,
    )


class ZenControllerEntity(Entity):
    """Base entity linked to a ZenHub and optionally a specific controller."""

    _attr_has_entity_name = True
    # State is pushed via ZenHub event callbacks; do not poll.
    _attr_should_poll = False

    def __init__(self, hub: Any, zen_ctrl: Any | None = None) -> None:
        self._hub = hub
        self._zen_ctrl = zen_ctrl

    @property
    def available(self) -> bool:
        """Return True when the hub listener and this controller are online."""
        return self._hub.is_controller_available(self._zen_ctrl)

    @property
    def suggested_object_id(self) -> str | None:
        """Return a stable suggested object id when provided by subclasses."""
        return getattr(self, "_suggested_object_id", None)
