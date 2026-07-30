"""Tests for zencontrol-tpi helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.light import (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntityFeature,
)
from zencontrol import ZenColourType

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
    SCENE_OFF,
    arc_to_brightness,
    brightness_to_arc,
)
from custom_components.zencontrol_tpi.light import (
    _XY_MAX,
    _async_set_level_or_colour,
    _async_turn_off,
    _build_supported_modes,
    _colour_from_turn_on_kwargs,
    _supported_features,
    _transition_seconds,
    _xy_color,
)
from custom_components.zencontrol_tpi.manifest_store import build_manifest
from custom_components.zencontrol_tpi.rate_limiter import RateLimiter


def test_arc_brightness_roundtrip() -> None:
    """Arc and brightness conversions are inverse-ish in the working range."""
    arc = brightness_to_arc(128)
    assert arc > 0
    brightness = arc_to_brightness(arc)
    assert 100 <= brightness <= 160


def test_sysvar_label_classification() -> None:
    """Labels with sensor/switch substrings select HA exposure."""
    def classify(label: str | None) -> tuple[bool, bool]:
        lower = (label or "").casefold()
        return "sensor" in lower, "switch" in lower

    assert classify("Hallway Lux Sensor") == (True, False)
    assert classify("MVHR Boost Switch") == (False, True)
    assert classify("Garage Door Switch Sensor") == (True, True)
    assert classify("Internal Flag") == (False, False)
    assert classify(None) == (False, False)


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


def test_scene_select_constants() -> None:
    """Group scene select Off / None option labels."""
    assert SCENE_OFF == "Off"
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


def test_supported_features_transition_for_dimmable_modes() -> None:
    """Relay-only ONOFF lights do not advertise TRANSITION; dimmable ones do."""
    assert _supported_features({ColorMode.ONOFF}) == LightEntityFeature(0)
    assert _supported_features({ColorMode.BRIGHTNESS}) == LightEntityFeature.TRANSITION
    assert _supported_features({ColorMode.COLOR_TEMP}) == LightEntityFeature.TRANSITION
    assert (
        _supported_features({ColorMode.XY, ColorMode.COLOR_TEMP})
        == LightEntityFeature.TRANSITION
    )
    # Mixed relay + dimmable group members still advertise TRANSITION.
    assert (
        _supported_features({ColorMode.ONOFF, ColorMode.BRIGHTNESS})
        == LightEntityFeature.TRANSITION
    )


def test_transition_seconds_from_kwargs() -> None:
    """Explicit HA transition is rounded to int seconds; unset stays None."""
    assert _transition_seconds({}) is None
    assert _transition_seconds({ATTR_TRANSITION: 10}) == 10
    assert _transition_seconds({ATTR_TRANSITION: 2.9}) == 3
    assert _transition_seconds({ATTR_TRANSITION: 0.4}) == 0
    assert _transition_seconds({ATTR_TRANSITION: 0}) == 0


class _FadeTarget:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def set(self, **kwargs: object) -> None:
        self.calls.append(("set", (), kwargs))

    async def on(self, **kwargs: object) -> None:
        self.calls.append(("on", (), kwargs))

    async def off(self, **kwargs: object) -> None:
        self.calls.append(("off", (), kwargs))

    async def dali_custom_fade(self, level: int, duration: int) -> None:
        self.calls.append(("dali_custom_fade", (level, duration), {}))


@pytest.mark.asyncio
async def test_brightness_without_transition_uses_default_fade() -> None:
    """Unset transition keeps the fixture's configured DALI fade (fade=True)."""
    target = _FadeTarget()
    await _async_set_level_or_colour(target, brightness=128, colour=None)
    assert len(target.calls) == 1
    assert target.calls[0][0] == "set"
    assert target.calls[0][2]["fade"] is True
    assert "level" in target.calls[0][2]


@pytest.mark.asyncio
async def test_brightness_with_transition_uses_custom_fade() -> None:
    """Explicit transition fades brightness via dali_custom_fade()."""
    target = _FadeTarget()
    await _async_set_level_or_colour(
        target, brightness=128, colour=None, transition=5
    )
    assert len(target.calls) == 1
    name, args, _kwargs = target.calls[0]
    assert name == "dali_custom_fade"
    assert args[0] == brightness_to_arc(128)
    assert args[1] == 5


@pytest.mark.asyncio
async def test_brightness_zero_with_transition_uses_custom_fade() -> None:
    """Brightness 0 with transition fades to off via dali_custom_fade()."""
    target = _FadeTarget()
    await _async_set_level_or_colour(target, brightness=0, colour=None, transition=3)
    assert target.calls == [("dali_custom_fade", (0, 3), {})]


@pytest.mark.asyncio
async def test_brightness_zero_without_transition_uses_default_fade() -> None:
    """Brightness 0 without transition keeps the fixture's configured DALI fade."""
    target = _FadeTarget()
    await _async_set_level_or_colour(target, brightness=0, colour=None)
    assert target.calls == [("off", (), {"fade": True})]


@pytest.mark.asyncio
async def test_turn_off_with_transition_uses_custom_fade() -> None:
    """turn_off with transition uses dali_custom_fade; without keeps fade=True."""
    with_transition = _FadeTarget()
    await _async_turn_off(with_transition, transition=4)
    assert with_transition.calls == [("dali_custom_fade", (0, 4), {})]

    default = _FadeTarget()
    await _async_turn_off(default, transition=None)
    assert default.calls == [("off", (), {"fade": True})]


@pytest.mark.asyncio
async def test_colour_path_ignores_transition() -> None:
    """Colour commands keep default fade; custom fade is brightness-only."""
    target = _FadeTarget()
    colour = _colour_from_turn_on_kwargs({ATTR_XY_COLOR: (0.3, 0.4)})
    assert colour is not None
    await _async_set_level_or_colour(
        target, brightness=128, colour=colour, transition=8
    )
    assert len(target.calls) == 1
    assert target.calls[0][0] == "set"
    assert target.calls[0][2]["fade"] is True


@pytest.mark.asyncio
async def test_turn_on_without_brightness_ignores_transition() -> None:
    """Plain on (no brightness target) keeps default fade and ignores transition."""
    target = _FadeTarget()
    await _async_set_level_or_colour(
        target, brightness=None, colour=None, transition=9
    )
    assert target.calls == [("on", (), {"fade": True})]
