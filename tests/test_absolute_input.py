"""Tests for absolute-input assignment keys, matching, and sensor updates."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.zencontrol_tpi.sensor import ZenAbsoluteInputSensorEntity
from custom_components.zencontrol_tpi.sub_devices import (
    SubDeviceDef,
    absolute_input_assignment_key,
    build_assignments,
    match_label_for_absolute_input,
)


def _absolute_input(
    *,
    ctrl_name: str = "house",
    addr: int = 4,
    inst: int = 1,
    label: str | None = "Kitchen panel",
    instance_label: str | None = "Kitchen dial",
    value: int | None = None,
) -> SimpleNamespace:
    ctrl = SimpleNamespace(name=ctrl_name)
    instance = SimpleNamespace(
        address=SimpleNamespace(controller=ctrl, number=addr),
        number=inst,
        entity_id_string=lambda: f"ecd{addr}_{inst}",
    )
    return SimpleNamespace(
        instance=instance,
        label=label,
        instance_label=instance_label,
        value=value,
    )


def test_absolute_input_assignment_key() -> None:
    inp = _absolute_input(ctrl_name="house", addr=4, inst=2)
    assert absolute_input_assignment_key(inp) == "absolute:house:4:2"


def test_match_label_for_absolute_input_prefers_instance_label() -> None:
    inp = _absolute_input(
        label="Kitchen panel",
        instance_label="Kitchen dial",
    )
    assert match_label_for_absolute_input(inp) == "Kitchen dial"

    same = _absolute_input(label="Kitchen", instance_label="Kitchen")
    assert match_label_for_absolute_input(same) == "Kitchen"


def test_build_assignments_matches_absolute_input() -> None:
    kitchen = SubDeviceDef("kitchen", "Kitchen", ("Kitchen",))
    inp = _absolute_input(
        label="Kitchen panel",
        instance_label="Kitchen dial",
    )
    assignments = build_assignments(
        controller_sub_devices={"house": [kitchen]},
        lights=[],
        groups=[],
        buttons=[],
        motion_sensors=[],
        absolute_inputs=[inp],
        sysvars=[],
    )
    assert assignments["absolute:house:4:1"] == "kitchen"


def test_absolute_input_sensor_update_value() -> None:
    hub = MagicMock()
    hub.device_info_for.return_value = None
    zen_input = _absolute_input(
        addr=7,
        inst=3,
        label="Panel",
        instance_label="Volume",
        value=None,
    )

    entity = ZenAbsoluteInputSensorEntity(hub, zen_input)
    assert entity.unique_id == "house_ecd7_abs3"
    assert entity.name == "Volume"
    assert entity.native_value is None
    hub.register_absolute_input_entity.assert_called_once_with(zen_input, entity)

    entity.entity_id = "sensor.kitchen_dial"
    entity.async_write_ha_state = MagicMock()
    entity.update_value(0x1234)
    assert entity.native_value == 0x1234
    entity.async_write_ha_state.assert_called_once()
