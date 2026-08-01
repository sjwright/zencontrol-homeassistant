"""Shared controller ready-wait and bus discovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from zencontrol import (
    ZenAbsoluteInput,
    ZenBlind,
    ZenButton,
    ZenControl,
    ZenController,
    ZenFan,
    ZenGroup,
    ZenLight,
    ZenMotionSensor,
    ZenProfile,
    ZenSystemVariable,
)

from .const import (
    CONTROLLER_READY_POLL_INTERVAL,
    CONTROLLER_READY_QUERY_TIMEOUT,
    CONTROLLER_READY_WAIT_MAX,
)

_LOGGER = logging.getLogger(__name__)


class ControllerNotReadyError(Exception):
    """Controller did not become ready within the wait budget."""


@dataclass(slots=True)
class DiscoveredEntities:
    """Sorted, classified entities from a bus scan."""

    lights: list[ZenLight] = field(default_factory=list)
    fans: list[ZenFan] = field(default_factory=list)
    blinds: list[ZenBlind] = field(default_factory=list)
    groups: list[ZenGroup] = field(default_factory=list)
    buttons: list[ZenButton] = field(default_factory=list)
    motion_sensors: list[ZenMotionSensor] = field(default_factory=list)
    absolute_inputs: list[ZenAbsoluteInput] = field(default_factory=list)
    sv_switches: list[ZenSystemVariable] = field(default_factory=list)
    sv_sensors: list[ZenSystemVariable] = field(default_factory=list)
    profiles: list[ZenProfile] = field(default_factory=list)


async def wait_until_controller_ready(
    ctrl: ZenController,
    *,
    on_unreachable: Callable[[], None] | None = None,
    on_starting: Callable[[], None] | None = None,
) -> None:
    """Poll until ready, then interview. Raises ControllerNotReadyError."""
    deadline = asyncio.get_running_loop().time() + CONTROLLER_READY_WAIT_MAX
    while True:
        try:
            ready = await asyncio.wait_for(
                ctrl.commands.query_controller_startup_complete(ctrl),
                timeout=CONTROLLER_READY_QUERY_TIMEOUT,
            )
        except TimeoutError:
            ready = None

        if ready is True:
            break
        if ready is None:
            if on_unreachable is not None:
                on_unreachable()
            raise ControllerNotReadyError(
                f"Cannot reach controller {ctrl.label} ({ctrl.host})"
            )
        if on_starting is not None:
            on_starting()
        if asyncio.get_running_loop().time() >= deadline:
            raise ControllerNotReadyError(
                f"Controller {ctrl.label} ({ctrl.host}) still starting "
                f"after {CONTROLLER_READY_WAIT_MAX:.0f}s"
            )
        _LOGGER.info(
            "Controller %s still starting up, retrying in %ds…",
            ctrl.label,
            CONTROLLER_READY_POLL_INTERVAL,
        )
        await asyncio.sleep(CONTROLLER_READY_POLL_INTERVAL)

    await ctrl.interview()


async def discover_controller_entities(
    zen: ZenControl,
    controller: ZenController | None = None,
) -> DiscoveredEntities:
    """Scan the bus; scope to controller when given."""
    gear = await zen.get_control_gear(controller=controller)
    lights = [g for g in gear if isinstance(g, ZenLight)]
    fans = [g for g in gear if isinstance(g, ZenFan)]
    blinds = [g for g in gear if isinstance(g, ZenBlind)]

    instances = await zen.get_instances(controller=controller)
    buttons = [e for e in instances if isinstance(e, ZenButton)]
    motion_sensors = [e for e in instances if isinstance(e, ZenMotionSensor)]
    absolute_inputs = [e for e in instances if isinstance(e, ZenAbsoluteInput)]
    found = DiscoveredEntities(
        lights=sorted(lights, key=lambda lt: lt.address.number),
        fans=sorted(fans, key=lambda f: f.address.number),
        blinds=sorted(blinds, key=lambda b: b.address.number),
        groups=sorted(await zen.get_groups(controller=controller), key=lambda g: g.address.number),
        buttons=sorted(buttons, key=lambda b: (b.instance.address.number, b.instance.number)),
        motion_sensors=sorted(motion_sensors, key=lambda s: (s.instance.address.number, s.instance.number)),
        absolute_inputs=sorted(absolute_inputs, key=lambda a: (a.instance.address.number, a.instance.number)),
        profiles=sorted(await zen.get_profiles(controller=controller), key=lambda p: (p.controller.name, p.number)),
    )
    for sv in sorted(await zen.get_system_variables(controller=controller), key=lambda s: s.id):
        lower = (sv.label or "").casefold()
        as_sensor, as_switch = "sensor" in lower, "switch" in lower
        if as_switch:
            found.sv_switches.append(sv)
        if as_sensor:
            found.sv_sensors.append(sv)
    return found
