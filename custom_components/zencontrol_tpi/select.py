"""Select platform for zencontrol-tpi (profiles and group scenes)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .sub_devices import group_assignment_key
from .const import SCENE_NONE
from .entity import ZenControllerEntity
from .hub import ZenHub, ZencontrolTpiConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up select entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities: list[SelectEntity] = []

        # One profile select per controller
        for ctrl in hub.controllers:
            ctrl_profiles = [p for p in hub.profiles if p.controller is ctrl]
            if ctrl_profiles:
                entities.append(ZenProfileSelectEntity(hub, ctrl, ctrl_profiles))

        # One scene select per group (only if the group has labelled scenes)
        for group in hub.groups:
            if not group.lights:
                continue
            scene_labels = group.get_scene_labels(exclude_none=True)
            if scene_labels:
                entities.append(ZenGroupSceneSelectEntity(hub, group))

        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenProfileSelectEntity(ZenControllerEntity, SelectEntity):
    """Select entity to switch between controller profiles."""

    _attr_translation_key = "profile"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: ZenHub, zen_ctrl: Any, profiles: list[Any]) -> None:
        super().__init__(hub, zen_ctrl)
        self._ctrl = zen_ctrl
        self._profiles = {p.label: p for p in profiles if p.label}

        self._attr_unique_id = f"{zen_ctrl.name}_profile"
        self._suggested_object_id = "profile"
        self._attr_device_info = hub.device_info_for(zen_ctrl)
        self._attr_options = list(self._profiles.keys())
        self._attr_current_option = self._current_option_from_ctrl()

        hub.register_profile_entity(zen_ctrl, self)

    def _current_option_from_ctrl(self) -> str | None:
        profile = self._ctrl.profile
        if profile is None:
            return None
        return profile.label

    def update_current_option(self) -> None:
        """Called by ZenHub when the controller profile changes."""
        self._attr_current_option = self._current_option_from_ctrl()
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in self._profiles:
            raise ServiceValidationError(f"Unknown profile: {option}")
        try:
            await self._ctrl.switch_to_profile(option)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to switch profile: {err}") from err
        self._attr_current_option = option
        self.async_write_ha_state()


class ZenGroupSceneSelectEntity(ZenControllerEntity, SelectEntity):
    """Select entity to recall a named scene on a group."""

    _attr_translation_key = "group_scene"
    # Entity category is no category (controls).
    
    def __init__(self, hub: ZenHub, zen_group: Any) -> None:
        ctrl = zen_group.address.controller
        super().__init__(hub, ctrl)
        self._group = zen_group

        self._attr_unique_id = f"{ctrl.name}_group_{zen_group.address.number}_scene"
        self._suggested_object_id = f"{zen_group.address.entity_id_string()}_scene"
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=group_assignment_key(zen_group)
        )
        scene_labels = zen_group.get_scene_labels(exclude_none=True)
        self._attr_options = [SCENE_NONE, *scene_labels]
        group_label = zen_group.label or f"Group {zen_group.address.number}"
        self._attr_translation_placeholders = {"group": group_label}
        self._attr_current_option = self._current_option_from_group()

        hub.register_scene_entity(zen_group, self)

    def _current_option_from_group(self) -> str | None:
        level = self._group.level
        colour = self._group.colour
        scene = self._group.scene
        if level is None and colour is None and scene is None:
            return SCENE_NONE
        if scene is None:
            return None
        label = self._group.get_scene_label_from_number(scene)
        return label if label is not None else SCENE_NONE

    def update_current_option(self) -> None:
        """Called by ZenHub when the group scene/level changes."""
        self._attr_current_option = self._current_option_from_group()
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option == SCENE_NONE:
            return
        if option not in self._attr_options:
            raise ServiceValidationError(f"Unknown scene: {option}")
        try:
            await self._group.set_scene(option, fade=True)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to recall scene: {err}") from err
        self._attr_current_option = option
        self.async_write_ha_state()
