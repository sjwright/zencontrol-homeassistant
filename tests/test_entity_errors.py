"""Tests for entity command error mapping."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError
from zencontrol.exceptions import ZenConnectionError, ZenTimeoutError

from custom_components.zencontrol_tpi.entity import raise_command_failed


def test_raise_command_failed_maps_connection_errors() -> None:
    with pytest.raises(HomeAssistantError) as exc_info:
        raise_command_failed("turn on light", ZenConnectionError("offline"))
    err = exc_info.value
    assert err.translation_key == "not_connected"
    assert err.translation_placeholders == {"error": "offline"}


def test_raise_command_failed_maps_timeout_errors() -> None:
    with pytest.raises(HomeAssistantError) as exc_info:
        raise_command_failed("set fan speed", ZenTimeoutError("no reply"))
    assert exc_info.value.translation_key == "not_connected"


def test_raise_command_failed_maps_closed_client() -> None:
    with pytest.raises(HomeAssistantError) as exc_info:
        raise_command_failed("turn off light", RuntimeError("Client is closed"))
    assert exc_info.value.translation_key == "not_connected"


def test_raise_command_failed_keeps_generic_errors() -> None:
    with pytest.raises(HomeAssistantError) as exc_info:
        raise_command_failed("recall scene", ValueError("bad scene"))
    err = exc_info.value
    assert err.translation_key == "command_failed"
    assert err.translation_placeholders == {
        "action": "recall scene",
        "error": "bad scene",
    }
