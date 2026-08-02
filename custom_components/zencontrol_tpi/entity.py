"""Shared entity helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from zencontrol import ZenController

from .const import DOMAIN, normalize_mac

if TYPE_CHECKING:
    from .hub import ZenHub


def as_zen_controller(controller: object) -> ZenController:
    """Narrow address.ctrl to the interface ZenController.

    ZenAddress.ctrl is typed as ControllerRef so the API layer
    does not import the interface. Registered controllers are always the
    interface subclass at runtime.
    """
    return cast(ZenController, controller)


def controller_identifier(zen_ctrl: ZenController) -> tuple[str, str]:
    """Stable parent-device identifier for a controller.

    Prefer MAC, which the config flow always records. ZenController.mac is
    optional, so fall back to the controller name.
    """
    if zen_ctrl.mac:
        return (DOMAIN, normalize_mac(str(zen_ctrl.mac)))
    return (DOMAIN, zen_ctrl.name)


def controller_device_info(zen_ctrl: ZenController) -> DeviceInfo:
    """Build DeviceInfo for a Zen controller (hub / parent device)."""
    return DeviceInfo(
        identifiers={controller_identifier(zen_ctrl)},
        name=zen_ctrl.label,
        manufacturer="ZenControl",
        model="Controller",
        sw_version=str(zen_ctrl.version) if zen_ctrl.version is not None else None,
    )


def sub_device_device_info(
    zen_ctrl: ZenController,
    *,
    sub_device_id: str,
    sub_device_name: str,
) -> DeviceInfo:
    """Build DeviceInfo for a label-prefix child device under a controller.

    Parent linkage is not set here. Callers that create registry devices must
    pass via_device_id after resolving the parent DeviceEntry (HA 2026.8+
    deprecates DeviceInfo via_device because identifiers are per config entry).
    """
    parent = controller_identifier(zen_ctrl)
    controller_name = zen_ctrl.label or zen_ctrl.name
    return DeviceInfo(
        identifiers={(DOMAIN, f"{parent[1]}:sub:{sub_device_id}")},
        name=f"{controller_name} → {sub_device_name}",
        manufacturer="ZenControl",
        model=f"Virtual sub-device of {controller_name}",
    )


class ZenControllerEntity(Entity):
    """Base entity linked to a ZenHub and optionally a specific controller."""

    _attr_has_entity_name = True
    # State is pushed via ZenHub event callbacks; do not poll.
    _attr_should_poll = False
    # Subclasses set this to request a stable entity object id.
    _suggested_object_id: str | None = None

    def __init__(
        self,
        hub: ZenHub,
        zen_ctrl: ZenController | None = None,
        *,
        assignment_key: str | None = None,
    ) -> None:
        self._hub = hub
        self._zen_ctrl = zen_ctrl
        self._assignment_key = assignment_key

    @property
    def available(self) -> bool:
        """Return True when the hub listener and this controller are online."""
        return self._hub.is_controller_available(self._zen_ctrl)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Parent or sub-device info from current hub assignments."""
        if self._zen_ctrl is None:
            return None
        return self._hub.device_info_for(self._zen_ctrl, assignment_key=self._assignment_key)

    @property
    def suggested_object_id(self) -> str | None:
        """Return a stable suggested object id when provided by subclasses."""
        return self._suggested_object_id
