"""Sensor platform for zencontrol-tpi (ZenSystemVariable sensor type)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import LIGHT_LUX
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
    """Set up sensor entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [
            ZenSystemVariableSensorEntity(hub, sv) for sv in hub.sv_sensors
        ]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenSystemVariableSensorEntity(ZenControllerEntity, SensorEntity):
    """HA sensor entity wrapping a read-only ZenSystemVariable."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: ZenHub, zen_sv: Any) -> None:
        ctrl = zen_sv.controller
        super().__init__(hub, ctrl)
        self._sv = zen_sv

        self._attr_unique_id = f"{ctrl.name}_sv{zen_sv.id}_sensor"
        self._suggested_object_id = f"sv{zen_sv.id}"
        self._attr_device_info = controller_device_info(ctrl)
        self._attr_name = zen_sv.label or f"System Variable {zen_sv.id}"
        lower_label = (zen_sv.label or "").casefold()
        if lower_label.endswith("lux sensor"):
            self._attr_device_class = SensorDeviceClass.ILLUMINANCE
            self._attr_native_unit_of_measurement = LIGHT_LUX
        self._attr_native_value = zen_sv.value

        hub.register_sv_sensor_entity(zen_sv, self)

    def update_value(self, value: int) -> None:
        """Called by ZenHub when the system variable changes."""
        self._attr_native_value = value
        self.async_write_ha_state()
