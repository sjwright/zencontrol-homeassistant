"""Tests for native group scene entities."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import Platform

from custom_components.zencontrol_tpi.const import PLATFORMS
from custom_components.zencontrol_tpi.scene import ZenGroupSceneEntity


def _make_group(controller: Any, number: int, *, label: str) -> Any:
    group = SimpleNamespace(
        address=SimpleNamespace(
            ctrl=controller,
            number=number,
            entity_id_string=lambda: f"group{number}",
        ),
        label=label,
        set_scene=AsyncMock(return_value=True),
    )
    return group


def test_platforms_include_scene() -> None:
    assert Platform.SCENE in PLATFORMS


def test_scene_entity_ids_and_labels() -> None:
    ctrl = SimpleNamespace(name="zen1", label="House", mac="AA:BB:CC:DD:EE:FF")
    group = _make_group(ctrl, 14, label="Kitchen")
    hub = MagicMock()
    hub.device_info_for.return_value = {"identifiers": {("zencontrol_tpi", "x")}}

    entity = ZenGroupSceneEntity(hub, group, 2, "Relax")

    assert entity.unique_id == "zen1_group_14_scene_2"
    assert entity.suggested_object_id == "group14_scene2"
    assert entity.translation_key == "group_scene"
    assert entity.translation_placeholders == {
        "group": "Kitchen",
        "scene": "Relax",
    }
    hub.register_scene_entity.assert_called_once_with(group, 2, entity)


@pytest.mark.asyncio
async def test_scene_activate_recalls_controller_scene() -> None:
    ctrl = SimpleNamespace(name="zen1", label="House", mac="AA:BB:CC:DD:EE:FF")
    group = _make_group(ctrl, 14, label="Kitchen")
    hub = MagicMock()
    hub.device_info_for.return_value = {"identifiers": {("zencontrol_tpi", "x")}}

    entity = ZenGroupSceneEntity(hub, group, 2, "Relax")
    await entity.async_activate()

    group.set_scene.assert_awaited_once_with(2, fade=True)
