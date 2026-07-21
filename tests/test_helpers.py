"""Tests for zencontrol-tpi helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.zencontrol_tpi.config_flow import (
    build_controller_dict,
    entry_title,
    unique_controller_name,
)
from custom_components.zencontrol_tpi.const import (
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    SCENE_NONE,
    arc_to_brightness,
    brightness_to_arc,
)
from custom_components.zencontrol_tpi.manifest_store import build_manifest
from custom_components.zencontrol_tpi.rate_limiter import RateLimiter
from custom_components.zencontrol_tpi.sysvar import classify_sysvar


def test_arc_brightness_roundtrip() -> None:
    """Arc and brightness conversions are inverse-ish in the working range."""
    arc = brightness_to_arc(128)
    assert arc > 0
    brightness = arc_to_brightness(arc)
    assert 100 <= brightness <= 160


def test_classify_sysvar() -> None:
    """Labels classify to sensor, switch, both, or neither."""
    assert classify_sysvar("Hallway Lux Sensor") == (True, False)
    assert classify_sysvar("MVHR Boost Switch") == (False, True)
    assert classify_sysvar("Garage Door Switch Sensor") == (True, True)
    assert classify_sysvar("Internal Flag") == (False, False)
    assert classify_sysvar(None) == (False, False)


def test_build_manifest_dedupes_sysvars() -> None:
    """Manifest stores one sysvar record with both exposure flags."""
    ctrl = SimpleNamespace(name="zen1")
    sv = SimpleNamespace(
        controller=ctrl,
        id=2,
        label="Lux Sensor Switch",
        interview_serialize=lambda: '{"id": 2}',
    )
    hub = SimpleNamespace(
        lights=[],
        groups=[],
        buttons=[],
        motion_sensors=[],
        sv_switches=[sv],
        sv_sensors=[sv],
        profiles=[],
    )
    manifest = build_manifest(hub)
    assert len(manifest["sysvars"]) == 1
    assert manifest["sysvars"][0]["as_sensor"] is True
    assert manifest["sysvars"][0]["as_switch"] is True


@pytest.mark.asyncio
async def test_rate_limiter_execute_batch() -> None:
    """Rate limiter runs all coroutines."""
    limiter = RateLimiter(max_concurrent=2, delay_between_batches=0)
    calls: list[int] = []

    async def work(n: int) -> int:
        calls.append(n)
        return n

    results = await limiter.execute_batch([work(1), work(2), work(3)])
    assert results == [1, 2, 3]
    assert calls == [1, 2, 3]


def test_scene_none_constant() -> None:
    """Scene none label matches mqtt_bridge."""
    assert SCENE_NONE == "None"


def test_unique_controller_name_avoids_collisions() -> None:
    """Controller names stay unique when hosts collide."""
    existing = [
        build_controller_dict(
            "10.0.0.1", 5108, "AA:BB:CC:DD:EE:01", "One", "10001"
        )
    ]
    name = unique_controller_name("10.0.0.1", "AA:BB:CC:DD:EE:FF", existing)
    assert name != "10001"
    assert name not in {c[CONF_NAME] for c in existing}


def test_entry_title_single_and_multi() -> None:
    """Entry title shows +N when multiple controllers are present."""
    one = [{CONF_LABEL: "House", CONF_NAME: "house", CONF_MAC: "AA:BB:CC:DD:EE:01"}]
    two = one + [
        {CONF_LABEL: "Garage", CONF_NAME: "garage", CONF_MAC: "AA:BB:CC:DD:EE:02"}
    ]
    assert entry_title(one) == "House"
    assert entry_title(two) == "House (+1)"
