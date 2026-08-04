"""Options flow for configuring controller sub-devices."""

from __future__ import annotations

from typing import Any, Literal

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import AreaSelector
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_NAME,
    CONF_SUB_DEVICES,
    DOMAIN,
    ControllerConfig,
    controllers_from_entry_data,
    entry_data_for_controller,
)
from .sub_devices import (
    SubDeviceDef,
    parse_sub_device_prefixes,
    sub_device_from_prefixes,
    sub_devices_from_controller,
    sub_devices_to_config,
    validate_sub_device_prefixes,
)

CONF_AREA_ID = "area_id"
CONF_PREFIXES = "prefixes"
CTX_SUGGEST_SUB_DEVICES = "suggest_sub_devices_ctrl"

type SaveReturnStep = Literal["suggest_sub_devices", "controller"] | None


def _entry_title(ctrl_cfg: ControllerConfig) -> str:
    """Return the user-facing title for a controller entry."""
    return str(ctrl_cfg.get(CONF_LABEL) or ctrl_cfg.get(CONF_NAME) or "zencontrol")


def _sub_device_schema(
    *,
    prefixes_default: str | None = None,
    area_id: str | None = None,
) -> vol.Schema:
    """Build the add/reconfigure sub-device schema."""
    if prefixes_default is None:
        prefixes_field: Any = vol.Required(CONF_PREFIXES)
    else:
        prefixes_field = vol.Required(CONF_PREFIXES, default=prefixes_default)
    schema: dict[Any, Any] = {prefixes_field: str}
    if area_id:
        schema[vol.Optional(CONF_AREA_ID, default=area_id)] = AreaSelector()
    else:
        schema[vol.Optional(CONF_AREA_ID)] = AreaSelector()
    return vol.Schema(schema)


def _area_id_from_input(user_input: dict[str, Any]) -> str | None:
    """Normalize an optional area selector value."""
    raw = user_input.get(CONF_AREA_ID)
    return str(raw) if raw else None


class ZencontrolTpiOptionsFlow(OptionsFlow):
    """Options flow for sub-devices on an entry's controller."""

    _ctrl_name: str | None = None
    _sub_device_id: str | None = None
    _return_after_save: SaveReturnStep = None
    _suggest_from_setup_handled: bool = False

    def __getattr__(self, name: str) -> Any:
        """Route dynamic menu steps for sub-devices."""
        if name.startswith("async_step_subdev_"):
            sub_device_id = name.removeprefix("async_step_subdev_")

            async def async_step_subdev(
                user_input: dict[str, Any] | None = None,
            ) -> ConfigFlowResult:
                self._sub_device_id = sub_device_id
                return await self.async_step_sub_device()

            return async_step_subdev
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    async def _options_label(self, key: str, default: str) -> str:
        """Load an options-flow string for the current language."""
        translations = await async_get_translations(self.hass, self.hass.config.language, "options", {DOMAIN})
        return translations.get(f"component.{DOMAIN}.{key}", default)

    def _controllers(self) -> list[ControllerConfig]:
        """Return this entry's controllers (always length 0 or 1)."""
        return list(controllers_from_entry_data(self.config_entry.data))

    def _controller(self, name: str | None = None) -> ControllerConfig | None:
        """Return the only controller, optionally requiring a name match."""
        controllers = self._controllers()
        if not controllers:
            return None
        ctrl_cfg = controllers[0]
        if name is None or ctrl_cfg[CONF_NAME] == name:
            return ctrl_cfg
        return None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Open suggested setup or the controller's sub-device menu."""
        init_data = self.init_data if isinstance(self.init_data, dict) else {}
        if not self._suggest_from_setup_handled:
            match init_data.get(CTX_SUGGEST_SUB_DEVICES):
                case None:
                    pass
                case str() as name:
                    self._suggest_from_setup_handled = True
                    self._ctrl_name = name
                    return await self.async_step_suggest_sub_devices()

        ctrl_cfg = self._controller()
        if ctrl_cfg is None:
            return self.async_abort(reason="no_controllers")
        self._ctrl_name = ctrl_cfg[CONF_NAME]
        return await self.async_step_controller()

    async def async_step_suggest_sub_devices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer sub-device creation immediately after controller setup."""
        ctrl_cfg = self._controller(self._ctrl_name) or self._controller()
        if ctrl_cfg is None:
            return self.async_create_entry(title="", data={})
        self._ctrl_name = ctrl_cfg[CONF_NAME]
        self._return_after_save = "suggest_sub_devices"
        return self.async_show_menu(
            step_id="suggest_sub_devices",
            menu_options=["add_sub_device", "finish_setup"],
            description_placeholders={
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def async_step_finish_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Close options after declining or completing suggested setup."""
        self._return_after_save = None
        self._ctrl_name = None
        return self.async_create_entry(title="", data={})

    async def async_step_controller(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """List this controller's sub-devices plus Add sub-device."""
        ctrl_cfg = self._controller(self._ctrl_name) or self._controller()
        if ctrl_cfg is None:
            return self.async_abort(reason="no_controllers")
        self._ctrl_name = ctrl_cfg[CONF_NAME]
        self._return_after_save = "controller"
        sub_device_definitions = sub_devices_from_controller(ctrl_cfg)
        menu_options: dict[str, str] = {
            "add_sub_device": await self._options_label(
                "step.controller.menu_options.add_sub_device",
                "➕ Add sub-device",
            ),
        }
        for sub_device_definition in sub_device_definitions:
            prefixes = ", ".join(sub_device_definition.prefixes)
            label = (
                sub_device_definition.name
                if prefixes == sub_device_definition.name
                else f"{sub_device_definition.name} ({prefixes})"
            )
            menu_options[f"subdev_{sub_device_definition.id}"] = label
        return self.async_show_menu(
            step_id="controller",
            menu_options=menu_options,
            description_placeholders={
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def async_step_sub_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure or delete the selected sub-device."""
        ctrl_cfg = self._controller(self._ctrl_name)
        if ctrl_cfg is None:
            return await self.async_step_init()
        sub_device_definition = next(
            (item for item in sub_devices_from_controller(ctrl_cfg) if item.id == self._sub_device_id),
            None,
        )
        if sub_device_definition is None:
            return await self.async_step_controller()
        return self.async_show_menu(
            step_id="sub_device",
            menu_options=["reconfigure_sub_device", "delete_sub_device"],
            description_placeholders={
                "sub_device": sub_device_definition.name,
                "prefixes": ", ".join(sub_device_definition.prefixes),
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def async_step_add_sub_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a sub-device from a comma-separated prefix list."""
        errors: dict[str, str] = {}
        ctrl_cfg = self._controller(self._ctrl_name)
        if ctrl_cfg is None:
            return await self.async_step_init()
        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl_cfg)

        if user_input is not None:
            prefixes = parse_sub_device_prefixes(user_input.get(CONF_PREFIXES, ""))
            error = validate_sub_device_prefixes(existing, prefixes)
            if error:
                errors[CONF_PREFIXES] = error
            else:
                sub_device_definition = sub_device_from_prefixes(prefixes)
                assert sub_device_definition is not None
                area_id = _area_id_from_input(user_input)
                ids = {item.id for item in existing}
                sub_device_id = sub_device_definition.id
                base_id = sub_device_definition.id
                suffix = 2
                while sub_device_id in ids:
                    sub_device_id = f"{base_id}_{suffix}"
                    suffix += 1
                existing.append(
                    SubDeviceDef(
                        id=sub_device_id,
                        name=sub_device_definition.name,
                        prefixes=sub_device_definition.prefixes,
                        area_id=area_id,
                    )
                )
                ctrl_cfg[CONF_SUB_DEVICES] = sub_devices_to_config(existing)
                return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="add_sub_device",
            data_schema=_sub_device_schema(),
            errors=errors,
            description_placeholders={
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def async_step_reconfigure_sub_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit prefixes for the selected sub-device while retaining its ID."""
        errors: dict[str, str] = {}
        ctrl_cfg = self._controller(self._ctrl_name)
        if ctrl_cfg is None:
            return await self.async_step_init()
        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl_cfg)
        sub_device_definition = next((item for item in existing if item.id == self._sub_device_id), None)
        if sub_device_definition is None:
            return await self.async_step_controller()

        if user_input is not None:
            prefixes = parse_sub_device_prefixes(user_input.get(CONF_PREFIXES, ""))
            error = validate_sub_device_prefixes(existing, prefixes, replacing_id=sub_device_definition.id)
            if error:
                errors[CONF_PREFIXES] = error
            else:
                updated = SubDeviceDef(
                    id=sub_device_definition.id,
                    name=prefixes[0],
                    prefixes=tuple(prefixes),
                    area_id=_area_id_from_input(user_input),
                )
                ctrl_cfg[CONF_SUB_DEVICES] = sub_devices_to_config(
                    [updated if item.id == sub_device_definition.id else item for item in existing]
                )
                return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="reconfigure_sub_device",
            data_schema=_sub_device_schema(
                prefixes_default=",".join(sub_device_definition.prefixes),
                area_id=sub_device_definition.area_id,
            ),
            errors=errors,
            description_placeholders={
                "sub_device": sub_device_definition.name,
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def async_step_delete_sub_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm and delete the selected sub-device."""
        ctrl_cfg = self._controller(self._ctrl_name)
        if ctrl_cfg is None:
            return await self.async_step_init()
        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl_cfg)
        sub_device_definition = next((item for item in existing if item.id == self._sub_device_id), None)
        if sub_device_definition is None:
            return await self.async_step_controller()

        if user_input is not None:
            remaining = [item for item in existing if item.id != sub_device_definition.id]
            if remaining:
                ctrl_cfg[CONF_SUB_DEVICES] = sub_devices_to_config(remaining)
            else:
                ctrl_cfg.pop(CONF_SUB_DEVICES, None)
            return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="delete_sub_device",
            data_schema=vol.Schema({}),
            description_placeholders={
                "sub_device": sub_device_definition.name,
                "controller": ctrl_cfg.get(CONF_LABEL) or ctrl_cfg[CONF_NAME],
            },
        )

    async def _async_save_sub_devices(self, controllers: list[ControllerConfig]) -> ConfigFlowResult:
        """Persist sub-device config and reassign entities without rediscovery."""
        ctrl_cfg = controllers[0] if controllers else None
        title = _entry_title(ctrl_cfg) if ctrl_cfg else self.config_entry.title
        await self._async_persist_controller(controllers, title=title)
        hub = self.config_entry.runtime_data
        if hub is not None:
            hub.sync_device_assignments()

        match self._return_after_save:
            case "suggest_sub_devices":
                return await self.async_step_suggest_sub_devices()
            case "controller":
                return await self.async_step_controller()
            case _:
                return self.async_create_entry(title="", data={})

    async def _async_persist_controller(self, controllers: list[ControllerConfig], *, title: str) -> None:
        """Write the single controller into the config entry without reloading."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=title,
            data=entry_data_for_controller(controllers[0]) if controllers else {CONF_CONTROLLERS: []},
        )
