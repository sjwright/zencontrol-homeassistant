"""Migration must preserve controller ownership and user registry customisations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zencontrol_tpi import async_migrate_entry
from custom_components.zencontrol_tpi.const import CONFIG_VERSION, DOMAIN, normalize_mac_id

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "mock_setup_entry")


@pytest.fixture(autouse=True)
async def migration_environment(hass, enable_custom_integrations):
    # Load the integration before adding legacy entries so imports cannot trigger
    # a second migration through component setup in the middle of the test.
    assert await async_setup_component(hass, DOMAIN, {})
    with patch.object(hass.config_entries, "async_setup", return_value=True):
        yield


def _controller(number: int) -> dict:
    return {
        "host": f"10.0.0.{number}", "port": 5108,
        "mac": f"AA:BB:CC:DD:EE:{number:02X}",
        "name": f"controller{number}", "label": f"Controller {number}",
        "tcp": number == 2,
        "sub_devices": [{"id": "kitchen", "name": "Kitchen", "prefixes": ["Kitchen"]}],
    }


async def test_split_migration_preserves_registries(hass: HomeAssistant) -> None:
    controllers = [_controller(1), _controller(2)]
    entry = MockConfigEntry(domain=DOMAIN, version=1, data={"controllers": controllers, "unicast": True})
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    primary = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, controllers[0]["mac"])})
    moved = []
    for suffix in ("", ":sub:kitchen"):
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, controllers[1]["mac"] + suffix)},
        )
        devices.async_update_device(device.id, name_by_user="My kitchen", area_id=area.id)
        entity = entities.async_get_or_create(
            "light", DOMAIN, f"controller2_{suffix or 'main'}", config_entry=entry, device_id=device.id,
        )
        entity = entities.async_update_entity(
            entity.entity_id, new_entity_id=f"light.custom_{len(moved)}",
            name="Counter lights", disabled_by=er.RegistryEntryDisabler.USER,
        )
        moved.append((device.id, entity.entity_id, entity.unique_id))

    assert await async_migrate_entry(hass, entry)
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    imported = next(e for e in entries if e.entry_id != entry.entry_id)
    assert entry.version == imported.version == CONFIG_VERSION
    assert entry.unique_id == normalize_mac_id(controllers[0]["mac"])
    assert imported.unique_id == normalize_mac_id(controllers[1]["mac"])
    for migrated, original in [(entry, controllers[0]), (imported, controllers[1])]:
        assert migrated.data == {"controllers": [{**original, "unicast": True}]}
    assert devices.async_get(primary.id).config_entries == {entry.entry_id}
    for device_id, entity_id, unique_id in moved:
        device = devices.async_get(device_id)
        assert device.config_entries == {imported.entry_id}
        assert device.name_by_user == "My kitchen"
        assert device.area_id == area.id
        entity = entities.async_get(entity_id)
        assert entity.config_entry_id == imported.entry_id
        assert entity.device_id == device_id
        assert entity.unique_id == unique_id
        assert entity.name == "Counter lights"
        assert entity.disabled_by is er.RegistryEntryDisabler.USER


@pytest.mark.parametrize("failure", ["exception", "cancel", "abort"])
async def test_interrupted_migration_retries_without_losing_or_duplicating_controllers(hass, failure):
    original = {"controllers": [_controller(n) for n in (1, 2, 3)], "unicast": True}
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=deepcopy(original))
    entry.add_to_hass(hass)
    real_init = hass.config_entries.flow.async_init

    async def fail_third(domain, *, context, data):
        if data["controllers"][0]["name"] == "controller3":
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "exception":
                raise RuntimeError("import interrupted")
            return {"type": "abort", "reason": "cannot_connect"}
        return await real_init(domain, context=context, data=data)

    with patch.object(hass.config_entries.flow, "async_init", side_effect=fail_third):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await async_migrate_entry(hass, entry)
        elif failure == "exception":
            with pytest.raises(RuntimeError, match="import interrupted"):
                await async_migrate_entry(hass, entry)
        else:
            assert await async_migrate_entry(hass, entry) is False
    assert entry.version == 1
    assert entry.data == original
    assert await async_migrate_entry(hass, entry)
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 3
    assert {e.unique_id for e in entries} == {normalize_mac_id(c["mac"]) for c in original["controllers"]}
    assert {c["name"] for e in entries for c in e.data["controllers"]} == {"controller1", "controller2", "controller3"}
    assert await async_migrate_entry(hass, entry)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 3


async def test_migration_retains_unidentifiable_controller_for_repair(hass):
    unidentified = _controller(2)
    del unidentified["mac"]
    data = {"controllers": [_controller(1), unidentified], "unicast": True}
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=deepcopy(data))
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False
    assert entry.version == 1
    assert entry.data == data
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
