"""Fan platform for zencontrol smart fans (ZenFan)."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)
from homeassistant.util.scaling import int_states_in_range
from zencontrol import ZenFan

from .entity import ZenControllerEntity, as_zen_controller
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import fan_assignment_key

PARALLEL_UPDATES = 0

_SPEED_RANGE = (1, 4)
_PRESET_LOW = "Low"
_PRESET_MEDIUM = "Medium"
_PRESET_HIGH = "High"
_PRESET_MAX = "Max"
_PRESETS = (_PRESET_LOW, _PRESET_MEDIUM, _PRESET_HIGH, _PRESET_MAX)
_PRESET_TO_SPEED = {
    _PRESET_LOW: 1,
    _PRESET_MEDIUM: 2,
    _PRESET_HIGH: 3,
    _PRESET_MAX: 4,
}
_SPEED_TO_PRESET = {speed: name for name, speed in _PRESET_TO_SPEED.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up fan entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [ZenFanEntity(hub, fan) for fan in hub.fans]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenFanEntity(ZenControllerEntity, FanEntity):
    """HA fan wrapping a zencontrol smart fan ECG."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 4
    _attr_preset_modes: ClassVar[list[str]] = list(_PRESETS)

    def __init__(self, hub: ZenHub, zen_fan: ZenFan) -> None:
        ctrl = as_zen_controller(zen_fan.address.controller)
        super().__init__(hub, ctrl)
        self._fan = zen_fan
        number = zen_fan.address.number

        self._attr_unique_id = f"{ctrl.name}_fan_{number}"
        self._suggested_object_id = f"fan{number}"
        self._attr_device_info = hub.device_info_for(ctrl, assignment_key=fan_assignment_key(zen_fan))
        self._attr_name = zen_fan.label or f"Fan {number}"
        self._apply_state()
        hub.register_fan_entity(zen_fan, self)

    def _apply_state(self) -> None:
        level = self._fan.level
        if level is None:
            self._attr_is_on = None
            self._attr_percentage = None
            self._attr_preset_mode = None
            return
        speed = self._fan.speed
        self._attr_is_on = speed > 0
        if speed <= 0:
            self._attr_percentage = 0
            self._attr_preset_mode = None
        else:
            self._attr_percentage = ranged_value_to_percentage(_SPEED_RANGE, speed)
            self._attr_preset_mode = _SPEED_TO_PRESET.get(speed)

    def update_state(self) -> None:
        """Called by ZenHub when fan level changes."""
        self._apply_state()
        self.async_write_ha_state()

    @property
    def percentage_step(self) -> float:
        return 100 / int_states_in_range(_SPEED_RANGE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fan = self._fan
        return {
            "arc": fan.level,
            "label": fan.label,
            "address": fan.address.number,
            "ean": fan.ean,
            "operating_mode": fan.operating_mode,
            "bus_unit": fan.bus_unit,
        }

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            if preset_mode is not None:
                await self.async_set_preset_mode(preset_mode)
            elif percentage is not None:
                await self.async_set_percentage(percentage)
            else:
                await self._fan.on()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn on fan: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._fan.off()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn off fan: {err}") from err

    async def async_set_percentage(self, percentage: int) -> None:
        try:
            if percentage <= 0:
                await self._fan.set_speed(0)
                return
            speed = max(1, min(4, round(percentage_to_ranged_value(_SPEED_RANGE, percentage))))
            await self._fan.set_speed(speed)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to set fan speed: {err}") from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        speed = _PRESET_TO_SPEED.get(preset_mode)
        if speed is None:
            raise HomeAssistantError(f"Unknown fan preset mode: {preset_mode}")
        try:
            await self._fan.set_speed(speed)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to set fan preset: {err}") from err
