"""Switch platform for zencontrol-tpi (ZenSystemVariable switch type)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ZenControllerEntity
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import sysvar_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switch entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [
            ZenSystemVariableSwitchEntity(hub, sv) for sv in hub.sv_switches
        ]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenSystemVariableSwitchEntity(ZenControllerEntity, SwitchEntity):
    """HA switch entity wrapping a boolean ZenSystemVariable."""

    def __init__(self, hub: ZenHub, zen_sv: Any) -> None:
        ctrl = zen_sv.controller
        super().__init__(hub, ctrl)
        self._sv = zen_sv

        self._attr_unique_id = f"{ctrl.name}_sv{zen_sv.id}_switch"
        self._suggested_object_id = f"sv{zen_sv.id}"
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=sysvar_assignment_key(zen_sv)
        )
        self._attr_name = zen_sv.label or f"System Variable {zen_sv.id}"
        self._attr_is_on = (zen_sv.value or 0) != 0

        hub.register_sv_switch_entity(zen_sv, self)

    def update_value(self, value: int) -> None:
        """Called by ZenHub when the system variable changes."""
        self._attr_is_on = value != 0
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._sv.set_value(1)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn on switch: {err}") from err
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._sv.set_value(0)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn off switch: {err}") from err
        self._attr_is_on = False
        self.async_write_ha_state()
