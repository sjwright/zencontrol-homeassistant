"""Tests for zencontrol-tpi helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.light import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_XY_COLOR,
    ColorMode,
)
from zencontrol import ZenColourType  # type: ignore[import-untyped]

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
from custom_components.zencontrol_tpi.light import (
    _XY_MAX,
    _async_set_level_or_colour,
    _build_supported_modes,
    _colour_from_turn_on_kwargs,
    _xy_color,
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
        absolute_inputs=[],
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


def test_entry_title_uses_label() -> None:
    """Entry title is the controller label (or name)."""
    labeled = {CONF_LABEL: "House", CONF_NAME: "house", CONF_MAC: "AA:BB:CC:DD:EE:01"}
    named = {CONF_NAME: "garage", CONF_MAC: "AA:BB:CC:DD:EE:02"}
    assert entry_title(labeled) == "House"
    assert entry_title(named) == "garage"
    assert entry_title({}) == "zencontrol"


def test_colour_from_turn_on_kwargs() -> None:
    """turn_on colour kwargs map to the matching ZenColour type."""
    assert _colour_from_turn_on_kwargs({}) is None

    tc = _colour_from_turn_on_kwargs({ATTR_COLOR_TEMP_KELVIN: 3000})
    assert tc is not None
    assert tc.type == ZenColourType.TC
    assert tc.kelvin == 3000

    rgb = _colour_from_turn_on_kwargs({ATTR_RGB_COLOR: (1, 2, 3)})
    assert rgb is not None
    assert rgb.type == ZenColourType.RGBWAF
    assert (rgb.r, rgb.g, rgb.b, rgb.w, rgb.a) == (1, 2, 3, 0, 0)

    rgbw = _colour_from_turn_on_kwargs({ATTR_RGBW_COLOR: (1, 2, 3, 4)})
    assert rgbw is not None
    assert (rgbw.r, rgbw.g, rgbw.b, rgbw.w, rgbw.a) == (1, 2, 3, 4, 0)

    rgbww = _colour_from_turn_on_kwargs({ATTR_RGBWW_COLOR: (1, 2, 3, 4, 5)})
    assert rgbww is not None
    assert (rgbww.r, rgbww.g, rgbww.b, rgbww.w, rgbww.a) == (1, 2, 3, 4, 5)

    xy = _colour_from_turn_on_kwargs({ATTR_XY_COLOR: (0.25, 0.5)})
    assert xy is not None
    assert xy.type == ZenColourType.XY
    assert xy.x == round(0.25 * _XY_MAX)
    assert xy.y == round(0.5 * _XY_MAX)
    assert _xy_color(xy) == pytest.approx((0.25, 0.5), abs=1e-5)


def test_build_supported_modes_includes_xy() -> None:
    """XY feature flag maps to ColorMode.XY."""
    modes = _build_supported_modes({"brightness": True, "XY": True})
    assert modes == {ColorMode.XY}


@pytest.mark.asyncio
async def test_colour_only_uses_no_change_arc_level() -> None:
    """Colour-only turn_on must send level 255 (TPI no-arc-change), not 0/254."""
    calls: list[dict[str, object]] = []

    class _Target:
        async def set(self, **kwargs: object) -> None:
            calls.append(kwargs)

        async def on(self, **kwargs: object) -> None:
            raise AssertionError("on() should not be used for colour-only")

        async def off(self, **kwargs: object) -> None:
            raise AssertionError("off() should not be used for colour-only")

    colour = _colour_from_turn_on_kwargs({ATTR_XY_COLOR: (0.3, 0.4)})
    assert colour is not None
    await _async_set_level_or_colour(_Target(), brightness=None, colour=colour)
    assert len(calls) == 1
    assert calls[0]["level"] == 255
    assert calls[0]["colour"] is colour
    assert calls[0]["fade"] is True
