"""Typed test doubles for zencontrol-python / hub boundaries.

Prefer these over bare MagicMock when asserting runtime attach/detach and
callback wiring. Signature drift fails at call time instead of silently
accepting unknown attributes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

from zencontrol import DiscoveredController, ZenController


@dataclass
class FakeCallbacks:
    """Assignable callback slots matching ZenCallbacks used by SharedZenRuntime."""

    on_connect: Callable[[], Any] | None = None
    on_disconnect: Callable[[], Any] | None = None
    on_resync: Callable[[], Any] | None = None
    light_change: Callable[..., Any] | None = None
    fan_change: Callable[..., Any] | None = None
    blind_change: Callable[..., Any] | None = None
    group_change: Callable[..., Any] | None = None
    button_press: Callable[..., Any] | None = None
    button_long_press: Callable[..., Any] | None = None
    motion_event: Callable[..., Any] | None = None
    absolute_input_change: Callable[..., Any] | None = None
    system_variable_change: Callable[..., Any] | None = None
    profile_change: Callable[..., Any] | None = None
    controller_discovered: Callable[..., Any] | None = None
    controller_status_change: Callable[..., Any] | None = None


@dataclass
class FakeController:
    """Minimal controller returned by FakeZenControl.add_controller."""

    name: str
    label: str
    mac: str
    filtering: bool = False
    host: str = "10.0.0.1"
    port: int = 5108
    version: str | None = "1.0"


@dataclass
class FakeZenControl:
    """ZenControl stand-in for SharedZenRuntime unit tests."""

    callbacks: FakeCallbacks = field(default_factory=FakeCallbacks)
    discovered_controllers: list[DiscoveredController] = field(default_factory=list)
    controllers: list[FakeController] = field(default_factory=list)
    _by_name: dict[str, FakeController] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.add_controller_calls: list[dict[str, Any]] = []
        self.remove_controller = AsyncMock()
        self.configure_controller_events = AsyncMock(return_value=True)
        self.start = AsyncMock()
        self.aclose = AsyncMock()
        self.enrich_discovered = AsyncMock(side_effect=self._enrich_discovered)

    def add_controller(
        self,
        *,
        id: int,
        name: str,
        label: str,
        host: str,
        port: int,
        mac: str | None = None,
    ) -> FakeController:
        del id  # runtime allocates ids; fake ignores them
        ctrl = FakeController(
            name=name,
            label=label,
            mac=str(mac or ""),
            host=host,
            port=port,
        )
        self.add_controller_calls.append(
            {"name": name, "label": label, "host": host, "port": port, "mac": mac}
        )
        self._by_name[name] = ctrl
        self.controllers.append(ctrl)
        return ctrl

    async def _enrich_discovered(self, discovered: DiscoveredController) -> DiscoveredController:
        return discovered


@dataclass
class FakeHub:
    """Hub stand-in with the methods SharedZenRuntime invokes."""

    entry_id: str = "entry-1"
    controller: FakeController | ZenController | None = None

    def __post_init__(self) -> None:
        self.entry = type("Entry", (), {"entry_id": self.entry_id})()
        self.handle_listener_connect = AsyncMock()
        self.handle_listener_disconnect = AsyncMock()
        self.handle_listener_resync = AsyncMock()
        self.handle_light_change = AsyncMock()
        self.handle_fan_change = AsyncMock()
        self.handle_blind_change = AsyncMock()
        self.handle_group_change = AsyncMock()
        self.handle_button_press = AsyncMock()
        self.handle_button_long_press = AsyncMock()
        self.handle_motion_event = AsyncMock()
        self.handle_absolute_input_change = AsyncMock()
        self.handle_sv_change = AsyncMock()
        self.handle_profile_change = AsyncMock()
