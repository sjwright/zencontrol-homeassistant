"""Cover platform for zencontrol smart blinds (ZenBlind)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from zencontrol import ZenBlind

from .entity import (
    ZenControllerEntity,
    as_zen_controller,
    raise_command_failed,
    raise_cover_position_required,
)
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import blind_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cover entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [ZenBlindEntity(hub, blind) for blind in hub.blinds]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenBlindEntity(ZenControllerEntity, CoverEntity):
    """HA cover wrapping a zencontrol smart blind ECG."""

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, hub: ZenHub, zen_blind: ZenBlind) -> None:
        ctrl = as_zen_controller(zen_blind.address.ctrl)
        super().__init__(hub, ctrl, assignment_key=blind_assignment_key(zen_blind))
        self._blind = zen_blind
        number = zen_blind.address.number

        self._attr_unique_id = f"{ctrl.name}_blind_{number}"
        self._suggested_object_id = f"cover{number}"
        self._attr_name = zen_blind.label or f"Blind {number}"
        self._apply_state()
        hub.register_cover_entity(zen_blind, self)

    def _apply_state(self) -> None:
        position = self._blind.position
        self._attr_current_cover_position = position
        self._attr_is_closed = None if position is None else position == 0

    def update_state(self) -> None:
        """Called by ZenHub when blind level changes."""
        self._apply_state()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        blind = self._blind
        return {
            "arc": blind.level,
            "label": blind.label,
            "address": blind.address.number,
            "ean": blind.ean,
            "operating_mode": blind.operating_mode,
            "bus_unit": blind.bus_unit,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        try:
            await self._blind.open()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise_command_failed("open blind", err)

    async def async_close_cover(self, **kwargs: Any) -> None:
        try:
            await self._blind.close()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise_command_failed("close blind", err)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        try:
            await self._blind.stop()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise_command_failed("stop blind", err)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = kwargs.get(ATTR_POSITION)
        if position is None:
            raise_cover_position_required()
        try:
            await self._blind.set_position(int(position))
        except HomeAssistantError:
            raise
        except Exception as err:
            raise_command_failed("set blind position", err)
