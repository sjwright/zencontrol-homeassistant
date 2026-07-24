"""Sensor platform for zencontrol-tpi."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, LIGHT_LUX
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONTROLLER_STATUS_OPTIONS,
    CONTROLLER_STATUS_UNREACHABLE,
)
from .entity import ZenControllerEntity, controller_device_info
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import absolute_input_assignment_key, sysvar_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up controller status immediately; other sensors after discovery."""
    hub = entry.runtime_data
    async_add_entities([ZenControllerStatusSensor(hub)])

    async def on_discovery() -> None:
        entities: list[SensorEntity] = [
            ZenSystemVariableSensorEntity(hub, sv) for sv in hub.sv_sensors
        ]
        entities.extend(
            ZenAbsoluteInputSensorEntity(hub, inp) for inp in hub.absolute_inputs
        )
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenControllerStatusSensor(ZenControllerEntity, SensorEntity):
    """Diagnostic enum: online / starting / unreachable.

    Stays available while the integration is loaded so a rebooting controller
    still shows ``starting`` instead of disappearing as unavailable.
    """

    _attr_translation_key = "controller_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CONTROLLER_STATUS_OPTIONS)
    _attr_name = "Status"
    _attr_has_entity_name = True

    def __init__(self, hub: ZenHub) -> None:
        ctrl = hub.controller
        super().__init__(hub, ctrl)
        name = getattr(ctrl, "name", None) or hub.entry.entry_id
        self._attr_unique_id = f"{name}_controller_status"
        self._suggested_object_id = "status"
        if ctrl is not None:
            self._attr_device_info = controller_device_info(ctrl)
        hub.register_status_entity(self)

    @property
    def available(self) -> bool:
        """Keep status visible during starting / unreachable."""
        return not self._hub._stopping  # noqa: SLF001

    @property
    def native_value(self) -> str:
        """Always mirror hub status (avoids stale value if updates raced entity add)."""
        return self._hub.controller_status or CONTROLLER_STATUS_UNREACHABLE

    def update_status(self, status: str) -> None:
        """Called by ZenHub when controller runtime status changes."""
        del status  # value is read live from the hub
        if self.entity_id:
            self.async_write_ha_state()


class ZenSystemVariableSensorEntity(ZenControllerEntity, SensorEntity):
    """HA sensor entity wrapping a read-only ZenSystemVariable."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: ZenHub, zen_sv: Any) -> None:
        ctrl = zen_sv.controller
        super().__init__(hub, ctrl)
        self._sv = zen_sv

        self._attr_unique_id = f"{ctrl.name}_sv{zen_sv.id}_sensor"
        self._suggested_object_id = f"sv{zen_sv.id}"
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=sysvar_assignment_key(zen_sv)
        )
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


class ZenAbsoluteInputSensorEntity(ZenControllerEntity, SensorEntity):
    """HA sensor entity wrapping a ZenAbsoluteInput (dial/slider value)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: ZenHub, zen_input: Any) -> None:
        ctrl = zen_input.instance.address.controller
        super().__init__(hub, ctrl)
        self._input = zen_input
        addr = zen_input.instance.address.number
        inst = zen_input.instance.number

        self._attr_unique_id = f"{ctrl.name}_ecd{addr}_abs{inst}"
        self._suggested_object_id = zen_input.instance.entity_id_string()
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=absolute_input_assignment_key(zen_input)
        )
        self._attr_name = (
            zen_input.instance_label
            if zen_input.instance_label
            and zen_input.instance_label != zen_input.label
            else zen_input.label or f"Absolute Input {addr}"
        )
        # Controllers push value-change events only; None until first event.
        self._attr_native_value = zen_input.value

        hub.register_absolute_input_entity(zen_input, self)

    def update_value(self, value: int) -> None:
        """Called by ZenHub when an absolute-input event is received."""
        self._attr_native_value = value
        self.async_write_ha_state()
