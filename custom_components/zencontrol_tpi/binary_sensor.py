"""Binary sensor platform for zencontrol-tpi (motion/occupancy sensors)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from zencontrol import ZenMotionSensor

from .entity import ZenControllerEntity, as_zen_controller
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import instance_display_label, motion_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up motion sensor entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [ZenMotionSensorEntity(hub, sensor) for sensor in hub.motion_sensors]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenMotionSensorEntity(ZenControllerEntity, BinarySensorEntity):
    """HA entity wrapping a ZenMotionSensor (occupancy sensor)."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, hub: ZenHub, zen_sensor: ZenMotionSensor) -> None:
        ctrl = as_zen_controller(zen_sensor.instance.address.controller)
        super().__init__(hub, ctrl)
        self._sensor = zen_sensor
        addr = zen_sensor.instance.address.number
        inst = zen_sensor.instance.number

        self._attr_unique_id = f"{ctrl.name}_ecd{addr}_occ{inst}"
        self._suggested_object_id = zen_sensor.instance.entity_id_string()
        self._attr_device_info = hub.device_info_for(ctrl, assignment_key=motion_assignment_key(zen_sensor))
        self._attr_name = instance_display_label(zen_sensor) or f"Motion {addr}"

        # Occupied state; pushed by ZenHub via update_occupied(). Reading
        # occupied directly is safe now that the library guards last_detect
        # is None.
        self._attr_is_on = zen_sensor.occupied

        hub.register_motion_sensor_entity(zen_sensor, self)

    def update_occupied(self) -> None:
        """Called by ZenHub when a motion event is received."""
        self._attr_is_on = self._sensor.occupied
        self.async_write_ha_state()
