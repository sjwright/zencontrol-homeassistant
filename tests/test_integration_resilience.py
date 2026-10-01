"""Persistence, actuator control and shared-runtime recovery against a real bus."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zencontrol.exceptions import ZenTimeoutError

from custom_components.zencontrol_tpi import async_unload_entry
from custom_components.zencontrol_tpi.const import CONFIG_VERSION, DOMAIN, normalize_mac, normalize_mac_id
from custom_components.zencontrol_tpi.hub import ZenHub, pop_force_full_discovery
from custom_components.zencontrol_tpi.runtime import DATA_RUNTIME
from tests.conftest import LiveSimulator

pytestmark = [pytest.mark.simulator, pytest.mark.enable_socket, pytest.mark.usefixtures("enable_custom_integrations")]


def _entry(hass, live, name="sim", mac=None):
    mac = normalize_mac(mac or live.mac)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=normalize_mac_id(mac), version=CONFIG_VERSION,
        data={"controllers": [{"host": "127.0.0.1", "port": live.port, "mac": mac,
                               "name": name, "label": name, "unicast": True}]},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def loaded(hass, live_sim, enable_custom_integrations):
    entry = _entry(hass, live_sim)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry, live_sim
    finally:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def second_sim(live_sim):
    world = deepcopy(live_sim.world)
    world.mac = bytes.fromhex("020000000002")
    world.bind_port = 0
    world.unicast_ip, world.unicast_port = None, 0
    sim = type(live_sim.sim)(world)
    await sim.start()
    try:
        yield LiveSimulator(world=world, sim=sim)
    finally:
        await sim.stop()


def _registered(hass, entry):
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {e.unique_id: (e.entity_id, e.device_id) for e in entries}


def _entity_id(hass, domain, unique_id):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _service(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "cache",
    ["valid", "old-version", "unknown-controller", "broken-item", "bad-interview", "missing-section", "wrong-section-type"],
)
async def test_restart_restores_entities_or_recovers_corrupt_cache(hass, loaded, cache):
    entry, live = loaded
    old_hub = entry.runtime_data
    old_runtime = old_hub.runtime
    original_ids = _registered(hass, entry)
    manifest = deepcopy(await old_hub._manifest_store.async_load())
    assert manifest["lights"] and manifest["groups"] and manifest["fans"] and manifest["blinds"]
    if cache == "old-version":
        manifest["version"] -= 1
    elif cache == "unknown-controller":
        # Fail after one entity has hydrated, exercising partial-cache recovery.
        manifest["lights"][1]["controller"] = "not-this-controller"
    elif cache == "broken-item":
        del manifest["lights"][1]["number"]
    elif cache == "bad-interview":
        manifest["lights"][0]["interview"]["group_membership"] = None
    elif cache == "missing-section":
        del manifest["lights"]
    elif cache == "wrong-section-type":
        manifest["lights"] = None
    await old_hub._manifest_store.async_save(manifest)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DATA_RUNTIME not in hass.data.get(DOMAIN, {})
    # An explicit HA reload deliberately ignores the cache. Clear that marker
    # to model a process restart, retaining only persisted storage/registries.
    pop_force_full_discovery(entry.entry_id)
    live.world.lights[1].set_level(83)
    discoveries = []
    original_discover = ZenHub._run_full_discovery

    async def record_discovery(hub):
        discoveries.append(hub)
        await original_discover(hub)

    with patch.object(ZenHub, "_run_full_discovery", record_discovery):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub is not old_hub and hub.runtime is not old_runtime
    assert _registered(hass, entry) == original_ids
    assert len(discoveries) == (0 if cache in {"valid", "bad-interview"} else 1)
    assert next(light for light in hub.lights if light.address.number == 1).level == 83
    group = next(group for group in hub.groups if group.address.number == 0)
    assert {light.address.number for light in group.lights} == {0, 1}
    assert all(any(member is light for light in hub.lights) for member in group.lights)
    light_id = _entity_id(hass, "light", "sim_ecg_1")
    await _service(hass, "light", "turn_off", light_id)
    assert live.world.lights[1].level == 0
    saved = await hub._manifest_store.async_load()
    assert all(item["controller"] == "sim" for item in saved["lights"])
    assert saved["version"] == manifest["version"] + (cache == "old-version")


@pytest.mark.parametrize(("service", "data", "level"), [
    ("open_cover", {}, 254), ("close_cover", {}, 0),
    ("set_cover_position", {"position": 0}, 0),
    ("set_cover_position", {"position": 37}, 94),
    ("set_cover_position", {"position": 100}, 254),
])
async def test_cover_service_controls_only_its_actuator(hass, loaded, service, data, level):
    _, live = loaded
    before = {n: light.level for n, light in live.world.lights.items()}
    cover = _entity_id(hass, "cover", "sim_blind_13")
    await _service(hass, "cover", service, cover, **data)
    assert live.world.lights[13].level == level
    assert {n: light.level for n, light in live.world.lights.items() if n != 13} == {n: v for n, v in before.items() if n != 13}


async def test_cover_stop_freezes_an_active_fade(hass, loaded, monkeypatch):
    import zencontrol_simulator.world as world_module

    _, live = loaded
    clock = [2_000_000_000.0]
    # Replace only the simulator's clock, leaving HA's scheduling clock intact.
    class Clock:
        @staticmethod
        def time():
            return clock[0]

    monkeypatch.setattr(world_module, "time", Clock)
    blind = live.world.lights[13]
    blind.set_level(0)
    blind.set_level(254, fading_seconds=10, fade_origin=13)
    clock[0] += 4
    await _service(hass, "cover", "stop_cover", _entity_id(hass, "cover", "sim_blind_13"))
    assert 100 <= blind.level <= 103
    assert blind.fading_until is None
    frozen = blind.level
    clock[0] += 20
    assert blind.visible_level() == frozen


@pytest.mark.parametrize(("percentage", "arc"), [(0, 0), (1, 32), (25, 32), (50, 95), (75, 159), (100, 254)])
async def test_fan_percentage_controls_only_its_actuator(hass, loaded, percentage, arc):
    _, live = loaded
    before = {n: light.level for n, light in live.world.lights.items()}
    fan = _entity_id(hass, "fan", "sim_fan_12")
    await _service(hass, "fan", "set_percentage", fan, percentage=percentage)
    assert live.world.lights[12].level == arc
    assert {n: light.level for n, light in live.world.lights.items() if n != 12} == {n: v for n, v in before.items() if n != 12}


@pytest.mark.parametrize(("domain", "service", "unique_id"), [
    ("cover", "open_cover", "sim_blind_13"), ("cover", "stop_cover", "sim_blind_13"),
    ("fan", "turn_off", "sim_fan_12"),
])
async def test_actuator_transport_failure_reaches_service_caller(hass, loaded, domain, service, unique_id):
    entry, live = loaded
    before = {n: light.level for n, light in live.world.lights.items()}
    with patch.object(entry.runtime_data.zen.commands, "_send_packet", side_effect=ZenTimeoutError("controller offline")):
        with pytest.raises(HomeAssistantError):
            await _service(hass, domain, service, _entity_id(hass, domain, unique_id))
    assert {n: light.level for n, light in live.world.lights.items()} == before


@pytest.mark.parametrize("failure", ["cancel", "error", "retryable"])
async def test_failed_second_entry_keeps_first_running_and_can_retry(hass, loaded, second_sim, failure):
    first, live = loaded
    runtime = first.runtime_data.runtime
    second = _entry(hass, second_sim, "second")
    reached = asyncio.Event()
    original_wait = ZenHub._wait_for_controller

    async def fail_start(hub):
        if hub.entry is second:
            reached.set()
            if failure == "cancel":
                await asyncio.Future()
            if failure == "retryable":
                raise ConfigEntryNotReady("controller booting")
            raise RuntimeError("startup failed")
        await original_wait(hub)

    with patch.object(ZenHub, "_wait_for_controller", fail_start):
        task = asyncio.create_task(hass.config_entries.async_setup(second.entry_id))
        await asyncio.wait_for(reached.wait(), 2)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is False
    second.async_cancel_retry_setup()
    assert first.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][DATA_RUNTIME] is runtime
    assert [c.name for c in runtime.zen.controllers] == ["sim"]
    for entity in er.async_entries_for_config_entry(er.async_get(hass), second.entry_id):
        state = hass.states.get(entity.entity_id)
        assert state is None or state.state == "unavailable"
    light_id = _entity_id(hass, "light", "sim_ecg_1")
    await _service(hass, "light", "turn_on", light_id, brightness=128)
    assert live.world.lights[1].level > 0
    # Prove the surviving listener still updates the original hub after cleanup.
    updated = asyncio.Event()

    @callback
    def state_changed(event):
        state = event.data.get("new_state")
        if event.data["entity_id"] == light_id and state is not None and state.state == "off":
            updated.set()

    remove_listener = hass.bus.async_listen("state_changed", state_changed)
    try:
        live.sim.inject_level(1, 0)
        await asyncio.wait_for(updated.wait(), 2)
    finally:
        remove_listener()
    assert await hass.config_entries.async_reload(second.entry_id)
    assert second.runtime_data.runtime is runtime
    await _service(hass, "light", "turn_on", _entity_id(hass, "light", "second_ecg_1"), brightness=128)
    assert second_sim.world.lights[1].level > 0
    assert live.world.lights[1].level == 0
    assert await hass.config_entries.async_unload(second.entry_id)
    assert first.runtime_data.available


async def test_failed_platform_unload_keeps_entry_attached_and_usable(hass, loaded):
    entry, live = loaded
    hub = entry.runtime_data
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert await async_unload_entry(hass, entry) is False
    assert entry.runtime_data is hub
    assert not hub.stopping
    assert hub.controller in hub.zen.controllers
    await _service(hass, "light", "turn_on", _entity_id(hass, "light", "sim_ecg_1"), brightness=128)
    assert live.world.lights[1].level > 0
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DATA_RUNTIME not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize(("preset", "arc"), [("Low", 32), ("Medium", 95), ("High", 159), ("Max", 254)])
async def test_fan_preset_reaches_controller(hass, loaded, preset, arc):
    _, live = loaded
    fan = _entity_id(hass, "fan", "sim_fan_12")
    await _service(hass, "fan", "set_preset_mode", fan, preset_mode=preset)
    assert live.world.lights[12].level == arc
    assert hass.states.get(fan).attributes["preset_mode"] == preset


async def test_fan_on_restores_speed_after_off(hass, loaded):
    _, live = loaded
    fan = _entity_id(hass, "fan", "sim_fan_12")
    await _service(hass, "fan", "set_percentage", fan, percentage=75)
    await _service(hass, "fan", "turn_off", fan)
    assert live.world.lights[12].level == 0
    assert hass.states.get(fan).state == "off"
    await _service(hass, "fan", "turn_on", fan)
    assert live.world.lights[12].level == 159
    assert hass.states.get(fan).state == "on"
    assert hass.states.get(fan).attributes["percentage"] == 75
