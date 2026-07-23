"""Light platform for zencontrol-tpi (ZenLight and ZenGroup entities)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from zencontrol import ZenColour, ZenColourType  # type: ignore[import-untyped]

from .const import arc_to_brightness, brightness_to_arc
from .entity import ZenControllerEntity
from .hub import ZencontrolTpiConfigEntry, ZenHub
from .sub_devices import group_assignment_key, light_assignment_key

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZencontrolTpiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up light entities; entities are added after discovery completes."""
    hub = entry.runtime_data

    async def on_discovery() -> None:
        entities: list[LightEntity] = [
            ZenLightEntity(hub, light) for light in hub.lights
        ]
        entities.extend(
            ZenGroupEntity(hub, group) for group in hub.groups if group.lights
        )
        if entities:
            async_add_entities(entities)

    hub.register_discovery_callback(on_discovery)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_supported_modes(features: dict[str, bool]) -> set[ColorMode]:
    modes: set[ColorMode] = set()
    if features.get("RGBWW"):
        modes.add(ColorMode.RGBWW)
    if features.get("RGBW"):
        modes.add(ColorMode.RGBW)
    if features.get("RGB"):
        modes.add(ColorMode.RGB)
    if features.get("temperature"):
        modes.add(ColorMode.COLOR_TEMP)
    # BRIGHTNESS must not coexist with any richer mode — those already imply
    # brightness control. Only add it when the light supports dimming but
    # nothing else.
    if features.get("brightness") and not modes:
        modes.add(ColorMode.BRIGHTNESS)
    return modes or {ColorMode.ONOFF}


def _current_color_mode(
    supported: set[ColorMode], colour: Any | None
) -> ColorMode:
    """Determine the active color mode from the current colour object."""
    if colour is not None:
        match colour.type:
            case ZenColourType.TC if ColorMode.COLOR_TEMP in supported:
                return ColorMode.COLOR_TEMP
            case ZenColourType.RGBWAF:
                for mode in (ColorMode.RGBWW, ColorMode.RGBW, ColorMode.RGB):
                    if mode in supported:
                        return mode
            case _:
                pass
    for mode in (
        ColorMode.RGBWW,
        ColorMode.RGBW,
        ColorMode.RGB,
        ColorMode.COLOR_TEMP,
        ColorMode.BRIGHTNESS,
    ):
        if mode in supported:
            return mode
    return ColorMode.ONOFF


def _color_temp_kelvin(colour: Any | None) -> int | None:
    match colour:
        case object(type=ZenColourType.TC):
            return colour.kelvin
        case _:
            return None


def _rgb_color(colour: Any | None) -> tuple[int, int, int] | None:
    match colour:
        case object(type=ZenColourType.RGBWAF):
            return (colour.r or 0, colour.g or 0, colour.b or 0)
        case _:
            return None


def _rgbw_color(colour: Any | None) -> tuple[int, int, int, int] | None:
    match colour:
        case object(type=ZenColourType.RGBWAF):
            return (colour.r or 0, colour.g or 0, colour.b or 0, colour.w or 0)
        case _:
            return None


def _rgbww_color(colour: Any | None) -> tuple[int, int, int, int, int] | None:
    match colour:
        case object(type=ZenColourType.RGBWAF):
            return (
                colour.r or 0,
                colour.g or 0,
                colour.b or 0,
                colour.w or 0,
                colour.a or 0,
            )
        case _:
            return None


def _colour_from_turn_on_kwargs(kwargs: dict[str, Any]) -> ZenColour | None:
    """Build a ZenColour from HA turn_on colour kwargs, if any."""
    match (
        kwargs.get(ATTR_COLOR_TEMP_KELVIN),
        kwargs.get(ATTR_RGB_COLOR),
        kwargs.get(ATTR_RGBW_COLOR),
        kwargs.get(ATTR_RGBWW_COLOR),
    ):
        case (kelvin, None, None, None) if kelvin is not None:
            return ZenColour(type=ZenColourType.TC, kelvin=kelvin)
        case (None, (r, g, b), None, None):
            return ZenColour(type=ZenColourType.RGBWAF, r=r, g=g, b=b, w=0, a=0, f=0)
        case (None, None, (r, g, b, w), None):
            return ZenColour(type=ZenColourType.RGBWAF, r=r, g=g, b=b, w=w, a=0, f=0)
        case (None, None, None, (r, g, b, w, a)):
            return ZenColour(type=ZenColourType.RGBWAF, r=r, g=g, b=b, w=w, a=a, f=0)
        case _:
            return None


async def _async_set_level_or_colour(
    target: Any,
    *,
    brightness: int | None,
    colour: ZenColour | None,
    current_level: int | None,
) -> None:
    """Apply brightness/colour to a ZenLight or ZenGroup, or just turn on."""
    if brightness == 0:
        await target.off(fade=True)
        return

    arc = brightness_to_arc(brightness) if brightness is not None else None
    if arc is not None or colour is not None:
        # Preserve current level when only colour is changing
        if arc is None:
            arc = current_level if current_level is not None else 254
        await target.set(level=arc, colour=colour, fade=True)
    else:
        await target.on(fade=True)


# ---------------------------------------------------------------------------
# ZenLightEntity
# ---------------------------------------------------------------------------

class ZenLightEntity(ZenControllerEntity, LightEntity):
    """HA entity wrapping a single DALI control gear (ZenLight)."""

    def __init__(self, hub: ZenHub, zen_light: Any) -> None:
        ctrl = zen_light.address.controller
        super().__init__(hub, ctrl)
        self._light = zen_light

        self._attr_unique_id = f"{ctrl.name}_ecg_{zen_light.address.number}"
        self._suggested_object_id = zen_light.address.entity_id_string()
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=light_assignment_key(zen_light)
        )
        self._attr_name = zen_light.sub_label or zen_light.label or f"Light {zen_light.address.number}"

        self._supported_modes = _build_supported_modes(zen_light.features)
        self._attr_supported_color_modes = self._supported_modes

        if (min_k := zen_light.properties.get("min_kelvin")):
            self._attr_min_color_temp_kelvin = min_k
        if (max_k := zen_light.properties.get("max_kelvin")):
            self._attr_max_color_temp_kelvin = max_k

        self._apply_state()
        hub.register_light_entity(zen_light, self)

    def _apply_state(self) -> None:
        """Copy current ZenLight state into HA _attr_* fields."""
        level = self._light.level
        colour = self._light.colour
        self._attr_is_on = None if level is None else level > 0
        self._attr_brightness = (
            None if level is None else arc_to_brightness(level)
        )
        self._attr_color_mode = _current_color_mode(self._supported_modes, colour)
        self._attr_color_temp_kelvin = _color_temp_kelvin(colour)
        self._attr_rgb_color = _rgb_color(colour)
        self._attr_rgbw_color = (
            _rgbw_color(colour) if ColorMode.RGBW in self._supported_modes else None
        )
        self._attr_rgbww_color = (
            _rgbww_color(colour) if ColorMode.RGBWW in self._supported_modes else None
        )

    def update_state(self) -> None:
        """Called by ZenHub when light level/colour changes."""
        self._apply_state()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await _async_set_level_or_colour(
                self._light,
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                colour=_colour_from_turn_on_kwargs(kwargs),
                current_level=self._light.level,
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn on light: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._light.off(fade=True)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn off light: {err}") from err


# ---------------------------------------------------------------------------
# ZenGroupEntity
# ---------------------------------------------------------------------------

class ZenGroupEntity(ZenControllerEntity, LightEntity):
    """HA entity wrapping a DALI group (ZenGroup)."""

    def __init__(self, hub: ZenHub, zen_group: Any) -> None:
        ctrl = zen_group.address.controller
        super().__init__(hub, ctrl)
        self._group = zen_group

        self._attr_unique_id = f"{ctrl.name}_group_{zen_group.address.number}"
        self._suggested_object_id = zen_group.address.entity_id_string()
        self._attr_device_info = hub.device_info_for(
            ctrl, assignment_key=group_assignment_key(zen_group)
        )
        self._attr_name = zen_group.label or f"Group {zen_group.address.number}"

        # Derive color modes from member lights
        self._supported_modes = self._build_group_modes(zen_group)
        self._attr_supported_color_modes = self._supported_modes

        # Kelvin range from member lights
        self._set_kelvin_range(zen_group)

        self._apply_state()
        hub.register_group_entity(zen_group, self)

    @staticmethod
    def _build_group_modes(zen_group: Any) -> set[ColorMode]:
        modes: set[ColorMode] = set()
        for light in zen_group.lights:
            modes |= _build_supported_modes(light.features)
        # Remove BRIGHTNESS if any color mode is present (HA constraint)
        if modes - {ColorMode.BRIGHTNESS, ColorMode.ONOFF}:
            modes.discard(ColorMode.BRIGHTNESS)
            modes.discard(ColorMode.ONOFF)
        return modes or {ColorMode.BRIGHTNESS}

    def _set_kelvin_range(self, zen_group: Any) -> None:
        min_k = max_k = None
        for light in zen_group.lights:
            lmin = light.properties.get("min_kelvin")
            lmax = light.properties.get("max_kelvin")
            if lmin is not None:
                min_k = lmin if min_k is None else min(min_k, lmin)
            if lmax is not None:
                max_k = lmax if max_k is None else max(max_k, lmax)
        if min_k:
            self._attr_min_color_temp_kelvin = min_k
        if max_k:
            self._attr_max_color_temp_kelvin = max_k

    def _apply_state(self) -> None:
        """Copy current ZenGroup state into HA _attr_* fields."""
        level = self._group.level
        colour = self._group.colour
        scene = self._group.scene
        # None when group is discoordinated (members at different levels).
        if level is None and colour is None and scene is None:
            self._attr_is_on = None
        else:
            self._attr_is_on = (level or 0) > 0
        self._attr_brightness = (
            None if level is None else arc_to_brightness(level)
        )
        self._attr_color_mode = _current_color_mode(self._supported_modes, colour)
        self._attr_color_temp_kelvin = _color_temp_kelvin(colour)
        self._attr_rgb_color = _rgb_color(colour)
        self._attr_rgbw_color = (
            _rgbw_color(colour) if ColorMode.RGBW in self._supported_modes else None
        )
        self._attr_rgbww_color = (
            _rgbww_color(colour) if ColorMode.RGBWW in self._supported_modes else None
        )

    def update_state(self) -> None:
        """Called by ZenHub when group level/colour/scene changes."""
        self._apply_state()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await _async_set_level_or_colour(
                self._group,
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                colour=_colour_from_turn_on_kwargs(kwargs),
                current_level=self._group.level,
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn on group: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._group.off(fade=True)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn off group: {err}") from err
