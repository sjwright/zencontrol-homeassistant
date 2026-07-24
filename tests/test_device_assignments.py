"""Tests for the single idempotent device-assignment routine."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.zencontrol_tpi.const import DOMAIN
from custom_components.zencontrol_tpi.hub import ZenHub
from custom_components.zencontrol_tpi.sub_devices import CONF_SUB_DEVICES


class _Light:
    """Hashable light stub for entity maps."""

    def __init__(self, controller: Any, number: int, label: str) -> None:
        self.address = SimpleNamespace(controller=controller, number=number)
        self.sub_label = label
        self.label = label

    def __hash__(self) -> int:
        return hash((self.address.controller.name, self.address.number))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Light) and hash(self) == hash(other)


def _make_hub(
    *, controllers_cfg: list[dict[str, Any]], controllers: list[Any]
) -> ZenHub:
    hass = MagicMock()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"controllers": controllers_cfg},
        unique_id="uid-1",
    )
    with patch.object(ZenHub, "__init__", lambda self, *a, **k: None):
        hub = ZenHub.__new__(ZenHub)
    hub.hass = hass
    hub.entry = entry
    hub.controllers = controllers
    hub.lights = []
    hub.groups = []
    hub.buttons = []
    hub.motion_sensors = []
    hub.absolute_inputs = []
    hub.sv_switches = []
    hub.sv_sensors = []
    hub.profiles = []
    hub._light_entities = {}
    hub._group_entities = {}
    hub._button_entities = {}
    hub._motion_sensor_entities = {}
    hub._absolute_input_entities = {}
    hub._sv_sensor_entities = {}
    hub._sv_switch_entities = {}
    hub._profile_entities = {}
    hub._scene_entities = {}
    hub._sub_devices_by_controller = {}
    hub._sub_device_assignments = {}
    return hub


@contextmanager
def _patched_registries(
    device_registry: MagicMock,
    entity_registry: MagicMock,
    *,
    devices: list[Any] | None = None,
) -> Iterator[None]:
    with (
        patch(
            "custom_components.zencontrol_tpi.hub.dr.async_get",
            return_value=device_registry,
        ),
        patch(
            "custom_components.zencontrol_tpi.hub.er.async_get",
            return_value=entity_registry,
        ),
        patch(
            "custom_components.zencontrol_tpi.hub.dr.async_entries_for_config_entry",
            return_value=devices or [],
        ),
    ):
        yield


def test_expected_identifiers_include_controller_and_sub_devices() -> None:
    ctrl = SimpleNamespace(name="house", mac="AA:BB:CC:DD:EE:FF", label="House")
    hub = _make_hub(
        controllers_cfg=[
            {
                "name": "house",
                CONF_SUB_DEVICES: [
                    {
                        "id": "kitchen",
                        "name": "Kitchen",
                        "prefixes": ["Kitchen"],
                    }
                ],
            }
        ],
        controllers=[ctrl],
    )
    hub._rebuild_sub_device_assignments()
    expected = hub._expected_device_identifiers()
    assert (DOMAIN, "AA:BB:CC:DD:EE:FF") in expected
    assert (DOMAIN, "AA:BB:CC:DD:EE:FF:sub:kitchen") in expected


def test_sync_device_assignments_moves_entity_and_prunes_orphan() -> None:
    ctrl = SimpleNamespace(
        name="house",
        mac="AA:BB:CC:DD:EE:FF",
        label="House",
        version="1.0",
    )
    hub = _make_hub(
        controllers_cfg=[
            {
                "name": "house",
                CONF_SUB_DEVICES: [
                    {
                        "id": "kitchen",
                        "name": "Kitchen",
                        "prefixes": ["Kitchen"],
                    }
                ],
            }
        ],
        controllers=[ctrl],
    )

    light = _Light(ctrl, 3, "Kitchen spot")
    hub.lights = [light]
    entity = SimpleNamespace(entity_id="light.kitchen_spot", _attr_device_info=None)
    hub._light_entities = {light: entity}

    created: dict[frozenset[tuple[str, str]], SimpleNamespace] = {}

    def get_or_create(**kwargs: Any) -> SimpleNamespace:
        idents = frozenset(kwargs["identifiers"])
        device = created.get(idents)
        if device is None:
            device = SimpleNamespace(
                id=f"dev-{len(created)}",
                area_id=None,
                identifiers=set(idents),
            )
            created[idents] = device
        return device

    orphan = SimpleNamespace(
        id="orphan-sub",
        identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF:sub:old_room")},
    )
    kept = SimpleNamespace(
        id="kept-kitchen",
        identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF:sub:kitchen")},
    )

    device_registry = MagicMock()
    device_registry.async_get_or_create.side_effect = get_or_create
    entity_registry = MagicMock()
    entity_registry.async_get.return_value = SimpleNamespace(
        device_id="controller-device"
    )

    with _patched_registries(
        device_registry, entity_registry, devices=[orphan, kept]
    ):
        hub.sync_device_assignments()

    kitchen_idents = frozenset({(DOMAIN, "AA:BB:CC:DD:EE:FF:sub:kitchen")})
    kitchen_id = created[kitchen_idents].id
    assert entity._attr_device_info is not None
    assert entity._attr_device_info["identifiers"] == {
        (DOMAIN, "AA:BB:CC:DD:EE:FF:sub:kitchen")
    }
    entity_registry.async_update_entity.assert_called_once_with(
        "light.kitchen_spot",
        device_id=kitchen_id,
    )
    device_registry.async_remove_device.assert_called_once_with("orphan-sub")

    # Already on the correct device — second pass is a no-op for entity moves.
    entity_registry.async_get.return_value = SimpleNamespace(device_id=kitchen_id)
    entity_registry.async_update_entity.reset_mock()
    device_registry.async_remove_device.reset_mock()
    with _patched_registries(device_registry, entity_registry, devices=[kept]):
        hub.sync_device_assignments()
    entity_registry.async_update_entity.assert_not_called()
    device_registry.async_remove_device.assert_not_called()


def test_sync_skips_entities_without_entity_id() -> None:
    ctrl = SimpleNamespace(
        name="house", mac="AA:BB:CC:DD:EE:FF", label="House", version=None
    )
    hub = _make_hub(
        controllers_cfg=[{"name": "house", CONF_SUB_DEVICES: []}],
        controllers=[ctrl],
    )
    light = _Light(ctrl, 1, "Lone")
    hub.lights = [light]
    entity = SimpleNamespace(entity_id=None, _attr_device_info=None)
    hub._light_entities = {light: entity}

    device_registry = MagicMock()
    device_registry.async_get_or_create.side_effect = lambda **kwargs: SimpleNamespace(
        id="dev-ctrl", area_id=None, identifiers=kwargs["identifiers"]
    )
    entity_registry = MagicMock()

    with _patched_registries(device_registry, entity_registry):
        hub.sync_device_assignments()

    entity_registry.async_update_entity.assert_not_called()
    assert entity._attr_device_info is not None


def test_sync_skips_missing_registry_entry() -> None:
    ctrl = SimpleNamespace(
        name="house", mac="AA:BB:CC:DD:EE:FF", label="House", version=None
    )
    hub = _make_hub(
        controllers_cfg=[{"name": "house", CONF_SUB_DEVICES: []}],
        controllers=[ctrl],
    )
    light = _Light(ctrl, 1, "Lone")
    hub.lights = [light]
    entity = SimpleNamespace(entity_id="light.lone", _attr_device_info=None)
    hub._light_entities = {light: entity}

    device_registry = MagicMock()
    device_registry.async_get_or_create.side_effect = lambda **kwargs: SimpleNamespace(
        id="dev-ctrl", area_id=None, identifiers=kwargs["identifiers"]
    )
    entity_registry = MagicMock()
    entity_registry.async_get.return_value = None

    with _patched_registries(device_registry, entity_registry):
        hub.sync_device_assignments()

    entity_registry.async_update_entity.assert_not_called()


def test_prune_refuses_empty_expected_set() -> None:
    hub = _make_hub(controllers_cfg=[], controllers=[])
    device_registry = MagicMock()
    orphan = SimpleNamespace(
        id="orphan",
        identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF")},
    )
    with patch(
        "custom_components.zencontrol_tpi.hub.dr.async_entries_for_config_entry",
        return_value=[orphan],
    ):
        removed = hub._prune_orphaned_devices(device_registry, set())
    assert removed == 0
    device_registry.async_remove_device.assert_not_called()


def test_sync_continues_when_one_entity_update_fails() -> None:
    ctrl = SimpleNamespace(
        name="house", mac="AA:BB:CC:DD:EE:FF", label="House", version=None
    )
    hub = _make_hub(
        controllers_cfg=[
            {
                "name": "house",
                CONF_SUB_DEVICES: [
                    {"id": "kitchen", "name": "Kitchen", "prefixes": ["Kitchen"]}
                ],
            }
        ],
        controllers=[ctrl],
    )
    light_a = _Light(ctrl, 1, "Kitchen A")
    light_b = _Light(ctrl, 2, "Kitchen B")
    hub.lights = [light_a, light_b]
    entity_a = SimpleNamespace(entity_id="light.a", _attr_device_info=None)
    entity_b = SimpleNamespace(entity_id="light.b", _attr_device_info=None)
    hub._light_entities = {light_a: entity_a, light_b: entity_b}

    created: dict[frozenset[tuple[str, str]], SimpleNamespace] = {}

    def get_or_create(**kwargs: Any) -> SimpleNamespace:
        idents = frozenset(kwargs["identifiers"])
        device = created.get(idents)
        if device is None:
            device = SimpleNamespace(
                id=f"dev-{len(created)}",
                area_id=None,
                identifiers=set(idents),
            )
            created[idents] = device
        return device

    device_registry = MagicMock()
    device_registry.async_get_or_create.side_effect = get_or_create
    entity_registry = MagicMock()
    entity_registry.async_get.return_value = SimpleNamespace(device_id="old")

    def update_entity(entity_id: str, **kwargs: Any) -> None:
        if entity_id == "light.a":
            raise ValueError("Entity id not found")
        return None

    entity_registry.async_update_entity.side_effect = update_entity

    with _patched_registries(device_registry, entity_registry):
        hub.sync_device_assignments()

    assert entity_registry.async_update_entity.call_count == 2
    assert entity_b._attr_device_info is not None
