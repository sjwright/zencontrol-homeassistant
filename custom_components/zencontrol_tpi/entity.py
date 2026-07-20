"""Shared entity helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


def controller_device_info(zen_ctrl: Any) -> DeviceInfo:
    """Build DeviceInfo for a Zen controller."""
    return DeviceInfo(
        identifiers={(DOMAIN, zen_ctrl.mac or zen_ctrl.name)},
        name=zen_ctrl.label,
        manufacturer="ZenControl",
        model="TPI Controller",
        sw_version=str(zen_ctrl.version) if zen_ctrl.version is not None else None,
    )


class ZenControllerEntity(Entity):
    """Base entity linked to a ZenHub."""

    _attr_has_entity_name = True
    # State is pushed via ZenHub event callbacks; do not poll.
    _attr_should_poll = False

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self._attr_available = hub.available

    @property
    def suggested_object_id(self) -> str | None:
        """Return a stable suggested object id when provided by subclasses."""
        return getattr(self, "_suggested_object_id", None)
