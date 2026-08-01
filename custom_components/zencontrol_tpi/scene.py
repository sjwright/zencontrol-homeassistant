"""Scene platform for zencontrol-tpi (group scene recall)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from zencontrol import ZenGroup

from .entity import ZenControllerEntity, as_zen_controller
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import group_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up scene entities after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities = [
            ZenGroupSceneEntity(hub, group, number, label)
            for group in hub.groups
            if group.lights
            for number, label in enumerate(group.get_scene_labels(exclude_none=False))
            if label is not None
        ]
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


class ZenGroupSceneEntity(ZenControllerEntity, Scene):
    """HA scene that recalls a labelled DALI group scene on the controller.

    Activation sends a recall command; levels stay controller-side. Entity state
    is last activation time only, not whether the scene is still active.
    """

    _attr_translation_key = "group_scene"

    def __init__(
        self,
        hub: ZenHub,
        zen_group: ZenGroup,
        scene_number: int,
        scene_label: str,
    ) -> None:
        ctrl = as_zen_controller(zen_group.address.ctrl)
        super().__init__(hub, ctrl)
        self._group = zen_group
        self._scene_number = scene_number

        self._attr_unique_id = f"{ctrl.name}_group_{zen_group.address.number}_scene_{scene_number}"
        self._suggested_object_id = f"{zen_group.address.entity_id_string()}_scene{scene_number}"
        self._attr_device_info = hub.device_info_for(ctrl, assignment_key=group_assignment_key(zen_group))
        group_label = zen_group.label or f"Group {zen_group.address.number}"
        self._attr_translation_placeholders = {
            "group": group_label,
            "scene": scene_label,
        }

        hub.register_scene_entity(zen_group, scene_number, self)

    async def async_activate(self, **kwargs: Any) -> None:
        """Recall this scene on the DALI group."""
        try:
            await self._group.set_scene(self._scene_number, fade=True)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to recall scene: {err}") from err
