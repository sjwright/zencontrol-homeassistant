"""Tests for config entry data helpers and migrations."""

from __future__ import annotations

from custom_components.zencontrol_tpi.const import (
    CONF_CONTROLLERS,
    CONF_TCP,
    CONF_UNICAST,
    controller_tcp,
    controller_unicast,
    entry_data_for_controller,
    migrate_entry_data_to_v3,
)


def test_migrate_entry_data_moves_unicast_onto_controllers() -> None:
    """Entry-level unicast becomes a per-controller flag."""
    data = {
        CONF_CONTROLLERS: [
            {
                "host": "10.0.0.1",
                "port": 5108,
                "mac": "AA:BB:CC:DD:EE:01",
                "name": "a",
                "label": "A",
            }
        ],
        CONF_UNICAST: True,
    }
    migrated = migrate_entry_data_to_v3(data)
    assert CONF_UNICAST not in migrated
    assert migrated[CONF_CONTROLLERS][0][CONF_UNICAST] is True


def test_migrate_entry_level_overrides_controller() -> None:
    """Legacy entry-level unicast applies to every controller."""
    data = {
        CONF_CONTROLLERS: [
            {
                "host": "10.0.0.1",
                "port": 5108,
                "mac": "AA:BB:CC:DD:EE:01",
                "name": "a",
                "label": "A",
                CONF_UNICAST: False,
            }
        ],
        CONF_UNICAST: True,
    }
    migrated = migrate_entry_data_to_v3(data)
    assert migrated[CONF_CONTROLLERS][0][CONF_UNICAST] is True


def test_migrate_preserves_controller_when_no_entry_flag() -> None:
    """Without an entry-level key, controller unicast is left alone."""
    data = {
        CONF_CONTROLLERS: [
            {
                "host": "10.0.0.1",
                "port": 5108,
                "mac": "AA:BB:CC:DD:EE:01",
                "name": "a",
                "label": "A",
                CONF_UNICAST: True,
            }
        ],
    }
    migrated = migrate_entry_data_to_v3(data)
    assert migrated[CONF_CONTROLLERS][0][CONF_UNICAST] is True


def test_entry_data_for_controller_omits_entry_unicast() -> None:
    """Persisted entry data is controllers-only."""
    ctrl = {
        "host": "10.0.0.1",
        "port": 5108,
        "mac": "AA:BB:CC:DD:EE:01",
        "name": "a",
        "label": "A",
        CONF_UNICAST: True,
        CONF_TCP: True,
    }
    data = entry_data_for_controller(ctrl)  # type: ignore[arg-type]
    assert list(data) == [CONF_CONTROLLERS]
    assert controller_unicast(data[CONF_CONTROLLERS][0]) is True
    assert controller_tcp(data[CONF_CONTROLLERS][0]) is True
