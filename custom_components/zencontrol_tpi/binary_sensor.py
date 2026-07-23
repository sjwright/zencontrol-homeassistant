"""Binary sensor platform for zencontrol-tpi (motion/occupancy sensors)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZenControllerEntity
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import motion_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up motion sensor entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [ZenMotionSensorEntity(hub, s) for s in hub.motion_sensors]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenMotionSensorEntity(ZenControllerEntity, BinarySensorEntity):
    """HA entity wrapping a ZenMotionSensor (occupancy sensor)."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, hub: ZenHub, zen_sensor: Any) -> None:
        ctrl = zen_sensor.instance.address.controller
        super().__init__(hub, ctrl)
        self._sensor = zen_sensor
        addr = zen_sensor.instance.address.number
        inst = zen_sensor.instance.number

        self._attr_unique_id = f"{ctrl.name}_ecd{addr}_occ{inst}"
        self._suggested_object_id = zen_sensor.instance.entity_id_string()
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=motion_assignment_key(zen_sensor)
        )
        self._attr_name = (
            zen_sensor.instance_label
            if zen_sensor.instance_label
            and zen_sensor.instance_label != zen_sensor.label
            else zen_sensor.label or f"Motion {addr}"
        )

        # Occupied state; pushed by ZenHub via update_occupied().
        # Reading occupied directly is safe now that the library guards last_detect is None.
        self._attr_is_on = zen_sensor.occupied

        hub.register_motion_sensor_entity(zen_sensor, self)

    def update_occupied(self, occupied: bool) -> None:
        """Called by ZenHub when a motion event is received."""
        self._attr_is_on = occupied
        self.async_write_ha_state()
