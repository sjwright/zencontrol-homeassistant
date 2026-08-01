"""Tests for shared bus-discovery helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from zencontrol import ZenAbsoluteInput, ZenButton, ZenMotionSensor

from custom_components.zencontrol_tpi.discovery import (
    ControllerNotReadyError,
    discover_controller_entities,
    wait_until_controller_ready,
)


@pytest.mark.asyncio
async def test_wait_until_controller_ready_interviews() -> None:
    zen = MagicMock()
    zen.commands.query_controller_startup_complete = AsyncMock(return_value=True)
    ctrl = MagicMock()
    ctrl.label = "House"
    ctrl.host = "10.0.0.1"
    ctrl.interview = AsyncMock()

    await wait_until_controller_ready(zen, ctrl)

    zen.commands.query_controller_startup_complete.assert_awaited_once_with(ctrl)
    ctrl.interview.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_until_controller_ready_unreachable_callback() -> None:
    zen = MagicMock()
    zen.commands.query_controller_startup_complete = AsyncMock(return_value=None)
    ctrl = MagicMock()
    ctrl.label = "House"
    ctrl.host = "10.0.0.1"
    ctrl.interview = AsyncMock()
    seen: list[str] = []

    with pytest.raises(ControllerNotReadyError, match="Cannot reach"):
        await wait_until_controller_ready(
            zen, ctrl, on_unreachable=lambda: seen.append("unreachable")
        )

    assert seen == ["unreachable"]
    ctrl.interview.assert_not_awaited()


@pytest.mark.asyncio
async def test_discover_controller_entities_scopes_and_classifies() -> None:
    from zencontrol import ZenBlind, ZenFan, ZenLight

    ctrl = SimpleNamespace(name="house")
    light_obj = object.__new__(ZenLight)
    light_obj.address = SimpleNamespace(number=2, ctrl=ctrl)
    fan_obj = object.__new__(ZenFan)
    fan_obj.address = SimpleNamespace(number=12, ctrl=ctrl)
    blind_obj = object.__new__(ZenBlind)
    blind_obj.address = SimpleNamespace(number=13, ctrl=ctrl)

    group = SimpleNamespace(address=SimpleNamespace(number=1, ctrl=ctrl))
    button = object.__new__(ZenButton)
    button.instance = SimpleNamespace(
        address=SimpleNamespace(number=4, ctrl=ctrl), number=0
    )
    motion = object.__new__(ZenMotionSensor)
    motion.instance = SimpleNamespace(
        address=SimpleNamespace(number=5, ctrl=ctrl), number=1
    )
    absolute = object.__new__(ZenAbsoluteInput)
    absolute.instance = SimpleNamespace(
        address=SimpleNamespace(number=6, ctrl=ctrl), number=2
    )
    profile = SimpleNamespace(ctrl=ctrl, number=3)
    sv_lux = SimpleNamespace(id=1, label="Hall Lux Sensor", ctrl=ctrl)
    sv_switch = SimpleNamespace(id=2, label="Boost Switch", ctrl=ctrl)
    sv_both = SimpleNamespace(id=3, label="Door Switch Sensor", ctrl=ctrl)

    zen = MagicMock()
    zen.get_control_gear = AsyncMock(return_value=[light_obj, fan_obj, blind_obj])
    zen.get_groups = AsyncMock(return_value=[group])
    zen.get_instances = AsyncMock(return_value=[button, motion, absolute])
    zen.get_profiles = AsyncMock(return_value=[profile])
    zen.get_system_variables = AsyncMock(
        return_value=[sv_lux, sv_switch, sv_both]
    )

    found = await discover_controller_entities(zen, ctrl)

    zen.get_control_gear.assert_awaited_once_with(ctrl=ctrl)
    zen.get_instances.assert_awaited_once_with(ctrl=ctrl)
    assert found.lights == [light_obj]
    assert found.fans == [fan_obj]
    assert found.blinds == [blind_obj]
    assert found.groups == [group]
    assert found.buttons == [button]
    assert found.motion_sensors == [motion]
    assert found.absolute_inputs == [absolute]
    assert found.sv_sensors == [sv_lux, sv_both]
    assert found.sv_switches == [sv_switch, sv_both]
