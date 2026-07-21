"""Event platform for zencontrol-tpi (physical button press events)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZenControllerEntity, controller_device_info
from .hub import ZenHub, ZencontrolTpiConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up button event entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [ZenButtonEntity(hub, btn) for btn in hub.buttons]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenButtonEntity(ZenControllerEntity, EventEntity):
    """HA event entity wrapping a ZenButton (physical push button)."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types: list[str] = ["short_press", "long_press"]

    def __init__(self, hub: ZenHub, zen_button: Any) -> None:
        ctrl = zen_button.instance.address.controller
        super().__init__(hub, ctrl)
        self._button = zen_button
        addr = zen_button.instance.address.number
        inst = zen_button.instance.number

        self._attr_unique_id = f"{ctrl.name}_ecd{addr}_btn{inst}"
        self._suggested_object_id = zen_button.instance.entity_id_string()
        self._attr_device_info = controller_device_info(ctrl)
        self._attr_name = (
            zen_button.instance_label
            if zen_button.instance_label
            and zen_button.instance_label != zen_button.label
            else zen_button.label or f"Button {addr}"
        )

        hub.register_button_entity(zen_button, self)

    def trigger_event(self, event_type: str) -> None:
        """Called by ZenHub when a button press event is received."""
        self._trigger_event(event_type)
        self.async_write_ha_state()
