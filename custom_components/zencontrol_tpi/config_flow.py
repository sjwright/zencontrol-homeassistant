"""Config flow for zencontrol-tpi."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from functools import partial
from typing import Any

import getmac
import voluptuous as vol
import zencontrol  # type: ignore[import-untyped]
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_UNICAST,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}([0-9A-Fa-f]{2})$")


def _normalize_mac(mac: str) -> str:
    """Normalize MAC to uppercase colon-separated format."""
    return mac.upper().replace("-", ":").strip()


def _mac_id(mac: str) -> str:
    """Return MAC without separators for unique-id comparisons."""
    return _normalize_mac(mac).replace(":", "")


def _derive_name(host: str) -> str:
    """Derive an alphanumeric controller name from the host IP."""
    return re.sub(r"[^A-Za-z0-9]", "", host)[:16] or "zen"


def unique_controller_name(
    host: str, mac: str, existing: list[dict[str, Any]]
) -> str:
    """Return a name unique among existing controllers in this entry."""
    names = {c.get(CONF_NAME) for c in existing}
    base = _derive_name(host)
    if base not in names:
        return base
    suffix = _mac_id(mac)[-4:].lower()
    candidate = f"{base}{suffix}"[:16]
    if candidate not in names:
        return candidate
    n = 2
    while True:
        candidate = f"{base}{n}"[:16]
        if candidate not in names:
            return candidate
        n += 1


def entry_title(controllers: list[dict[str, Any]]) -> str:
    """Human-readable config entry title."""
    if not controllers:
        return "zencontrol"
    label = controllers[0].get(CONF_LABEL) or controllers[0].get(CONF_NAME) or "zencontrol"
    if len(controllers) == 1:
        return str(label)
    return f"{label} (+{len(controllers) - 1})"


def build_controller_dict(
    host: str,
    port: int,
    mac: str,
    label: str,
    name: str,
) -> dict[str, Any]:
    """Build a persisted controller config dict."""
    return {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_MAC: mac,
        CONF_NAME: name,
        CONF_LABEL: label,
    }


def _controller_schema(
    defaults: dict[str, Any] | None = None,
    *,
    include_unicast: bool = False,
) -> vol.Schema:
    """Build a controller connection schema."""
    defaults = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)
        ): str,
        vol.Required(
            CONF_PORT,
            default=defaults.get(CONF_PORT, DEFAULT_PORT),
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Optional(
            CONF_MAC,
            default=defaults.get(CONF_MAC, ""),
        ): str,
        vol.Required(
            CONF_LABEL, default=defaults.get(CONF_LABEL, vol.UNDEFINED)
        ): str,
    }
    if include_unicast:
        schema[
            vol.Optional(
                CONF_UNICAST,
                default=defaults.get(CONF_UNICAST, False),
            )
        ] = bool
    return vol.Schema(schema)


async def _async_discover_mac(hass: HomeAssistant, host: str) -> str | None:
    """Resolve host and look up its MAC via ARP/neighbor discovery."""
    host = host.strip()
    if not host:
        return None

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = await hass.async_add_executor_job(
                socket.getaddrinfo,
                host,
                None,
                socket.AF_UNSPEC,
                socket.SOCK_DGRAM,
            )
        except OSError:
            _LOGGER.debug("Could not resolve host %s for MAC lookup", host)
            return None
        if not infos:
            return None
        try:
            ip = ipaddress.ip_address(infos[0][4][0])
        except ValueError:
            return None

    params = {"ip": str(ip)} if ip.version == 4 else {"ip6": str(ip)}
    try:
        mac = await hass.async_add_executor_job(
            partial(getmac.get_mac_address, **params)
        )
    except Exception:
        _LOGGER.debug("MAC lookup failed for %s", host, exc_info=True)
        return None

    if not mac or mac.replace(":", "").replace("-", "").strip("0") == "":
        return None
    return _normalize_mac(mac)


async def _test_connection(host: str, port: int, mac: str, label: str) -> bool:
    """Return True if the controller responds within 5 seconds."""
    test_name = f"cftest{int(time.monotonic_ns()) % 10 ** 9}"
    zen = zencontrol.ZenControl()
    try:
        ctrl = zen.add_controller(
            id=99, name=test_name, label=label, host=host, port=port, mac=mac
        )
        result = await asyncio.wait_for(ctrl.is_controller_ready(), timeout=5.0)
        return result is True
    except Exception:
        _LOGGER.debug(
            "Connection test failed for %s:%s", host, port, exc_info=True
        )
        return False
    finally:
        try:
            await zen.aclose()
        except Exception:
            _LOGGER.debug("Failed to close connection-test ZenControl", exc_info=True)


class ZencontrolTpiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for zencontrol-tpi."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state for multi-controller setup."""
        self._controllers: list[dict[str, Any]] = []
        self._unicast: bool = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for managing controllers."""
        return ZencontrolTpiOptionsFlow()

    def _mac_in_flow(self, mac: str) -> bool:
        target = _mac_id(mac)
        return any(_mac_id(c[CONF_MAC]) == target for c in self._controllers)

    def _mac_in_other_entries(self, mac: str) -> bool:
        target = _mac_id(mac)
        for entry in self._async_current_entries():
            for ctrl in entry.data.get(CONF_CONTROLLERS, []):
                if _mac_id(ctrl.get(CONF_MAC, "")) == target:
                    return True
        return False

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial controller setup step."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}

        if user_input is not None:
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="user",
                include_unicast=True,
                existing=self._controllers,
            )
            if handled is not None:
                return handled

        # After successful append, handler returns add_more directly.
        return self.async_show_form(
            step_id="user",
            data_schema=_controller_schema(
                defaults or None, include_unicast=True
            ),
            errors=errors,
        )

    async def async_step_add_another(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Add an additional controller during initial setup."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}

        if user_input is not None:
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="add_another",
                include_unicast=False,
                existing=self._controllers,
            )
            if handled is not None:
                return handled

        return self.async_show_form(
            step_id="add_another",
            data_schema=_controller_schema(defaults or None, include_unicast=False),
            errors=errors,
        )

    async def async_step_add_more(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask whether to add another controller or finish."""
        return self.async_show_menu(
            step_id="add_more",
            menu_options=["add_another", "finish"],
        )

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the config entry from collected controllers."""
        if not self._controllers:
            return self.async_abort(reason="no_controllers")

        mac_id = _mac_id(self._controllers[0][CONF_MAC])
        await self.async_set_unique_id(mac_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=entry_title(self._controllers),
            data={
                CONF_CONTROLLERS: self._controllers,
                CONF_UNICAST: self._unicast,
            },
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose what to reconfigure: a controller or unicast settings."""
        entry = self._get_reconfigure_entry()
        controllers = list(entry.data.get(CONF_CONTROLLERS, []))
        if not controllers:
            return self.async_abort(reason="no_controllers")

        if len(controllers) == 1:
            self._reconfigure_index = 0
            return await self.async_step_reconfigure_controller()

        choices = {
            str(i): c.get(CONF_LABEL) or c.get(CONF_NAME) or f"Controller {i + 1}"
            for i, c in enumerate(controllers)
        }
        choices["unicast"] = "Unicast settings"

        if user_input is not None:
            action = user_input["target"]
            if action == "unicast":
                return await self.async_step_unicast_settings()
            self._reconfigure_index = int(action)
            return await self.async_step_reconfigure_controller()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required("target"): vol.In(choices)}
            ),
        )

    async def async_step_unicast_settings(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Update the shared unicast option for this entry."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            new_data = {
                **entry.data,
                CONF_UNICAST: user_input.get(CONF_UNICAST, False),
            }
            return self.async_update_reload_and_abort(
                entry,
                data=new_data,
            )

        return self.async_show_form(
            step_id="unicast_settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_UNICAST,
                        default=entry.data.get(CONF_UNICAST, False),
                    ): bool
                }
            ),
        )

    async def async_step_reconfigure_controller(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Update one controller in the entry."""
        entry = self._get_reconfigure_entry()
        controllers = list(entry.data.get(CONF_CONTROLLERS, []))
        index = getattr(self, "_reconfigure_index", 0)
        current = controllers[index]
        errors: dict[str, str] = {}
        defaults = {
            CONF_HOST: current.get(CONF_HOST, ""),
            CONF_PORT: current.get(CONF_PORT, DEFAULT_PORT),
            CONF_MAC: current.get(CONF_MAC, ""),
            CONF_LABEL: current.get(CONF_LABEL, ""),
            CONF_NAME: current.get(CONF_NAME),
        }
        if user_input:
            defaults = {**defaults, **user_input}

        if user_input is not None:
            mac = (user_input.get(CONF_MAC) or "").strip()
            if not mac:
                discovered = await _async_discover_mac(
                    self.hass, user_input[CONF_HOST]
                )
                if discovered:
                    defaults = {**defaults, CONF_MAC: discovered}
                    return self.async_show_form(
                        step_id="reconfigure_controller",
                        data_schema=_controller_schema(defaults),
                        errors={},
                    )
                errors[CONF_MAC] = "mac_not_found"
            else:
                result = await self._async_validate_fields(user_input, errors)
                if result is not None:
                    host, port, mac, label = result
                    others = [c for i, c in enumerate(controllers) if i != index]
                    if any(_mac_id(c[CONF_MAC]) == _mac_id(mac) for c in others):
                        errors[CONF_MAC] = "duplicate_mac"
                    else:
                        # Keep CONF_NAME stable so entity unique_ids survive IP edits.
                        name = current.get(CONF_NAME) or unique_controller_name(
                            host, mac, others
                        )
                        controllers[index] = build_controller_dict(
                            host, port, mac, label, name
                        )
                        new_unique = entry.unique_id
                        if index == 0:
                            new_unique = _mac_id(mac)
                            await self.async_set_unique_id(new_unique)
                            if entry.unique_id != new_unique:
                                self._abort_if_unique_id_configured()

                        return self.async_update_reload_and_abort(
                            entry,
                            unique_id=new_unique,
                            title=entry_title(controllers),
                            data={
                                CONF_CONTROLLERS: controllers,
                                CONF_UNICAST: entry.data.get(CONF_UNICAST, False),
                            },
                        )

        return self.async_show_form(
            step_id="reconfigure_controller",
            data_schema=_controller_schema(defaults),
            errors=errors,
        )

    async def _async_handle_controller_form(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
        defaults: dict[str, Any],
        *,
        step_id: str,
        include_unicast: bool,
        existing: list[dict[str, Any]],
    ) -> ConfigFlowResult | None:
        """Validate and append a controller, or re-show for MAC confirm.

        Returns a ConfigFlowResult when navigation should continue, else None
        to show the form with ``errors``.
        """
        mac = (user_input.get(CONF_MAC) or "").strip()
        if not mac:
            discovered = await _async_discover_mac(self.hass, user_input[CONF_HOST])
            if discovered:
                defaults.clear()
                defaults.update({**user_input, CONF_MAC: discovered})
                return self.async_show_form(
                    step_id=step_id,
                    data_schema=_controller_schema(
                        defaults, include_unicast=include_unicast
                    ),
                    errors={},
                )
            errors[CONF_MAC] = "mac_not_found"
            return None

        result = await self._async_validate_fields(user_input, errors)
        if result is None:
            return None

        host, port, mac, label = result
        if self._mac_in_flow(mac) or self._mac_in_other_entries(mac):
            errors[CONF_MAC] = "duplicate_mac"
            return None

        name = unique_controller_name(host, mac, existing)
        existing.append(build_controller_dict(host, port, mac, label, name))
        if include_unicast:
            self._unicast = bool(user_input.get(CONF_UNICAST, False))

        return await self.async_step_add_more()

    async def _async_validate_fields(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> tuple[str, int, str, str] | None:
        """Validate host/port/mac/label and connectivity."""
        host = user_input[CONF_HOST].strip()
        port = user_input[CONF_PORT]
        mac = _normalize_mac(user_input[CONF_MAC])
        label = user_input[CONF_LABEL].strip()

        if not _MAC_RE.match(mac):
            errors[CONF_MAC] = "invalid_mac"
            return None
        if not label:
            errors[CONF_LABEL] = "invalid_label"
            return None

        reachable = await _test_connection(host, port, mac, label)
        if not reachable:
            errors["base"] = "cannot_connect"
            return None

        return host, port, mac, label


class ZencontrolTpiOptionsFlow(OptionsFlow):
    """Options flow to add/remove controllers on an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_controller", "remove_controller"],
        )

    async def async_step_add_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a controller to this config entry."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}
        controllers = list(self.config_entry.data.get(CONF_CONTROLLERS, []))

        if user_input is not None:
            mac = (user_input.get(CONF_MAC) or "").strip()
            if not mac:
                discovered = await _async_discover_mac(
                    self.hass, user_input[CONF_HOST]
                )
                if discovered:
                    defaults = {**user_input, CONF_MAC: discovered}
                    return self.async_show_form(
                        step_id="add_controller",
                        data_schema=_controller_schema(defaults),
                        errors={},
                    )
                errors[CONF_MAC] = "mac_not_found"
            else:
                host = user_input[CONF_HOST].strip()
                port = user_input[CONF_PORT]
                mac_n = _normalize_mac(user_input[CONF_MAC])
                label = user_input[CONF_LABEL].strip()
                if not _MAC_RE.match(mac_n):
                    errors[CONF_MAC] = "invalid_mac"
                elif not label:
                    errors[CONF_LABEL] = "invalid_label"
                elif any(_mac_id(c[CONF_MAC]) == _mac_id(mac_n) for c in controllers):
                    errors[CONF_MAC] = "duplicate_mac"
                elif await _mac_configured_elsewhere(
                    self.hass, self.config_entry.entry_id, mac_n
                ):
                    errors[CONF_MAC] = "duplicate_mac"
                elif not await _test_connection(host, port, mac_n, label):
                    errors["base"] = "cannot_connect"
                else:
                    name = unique_controller_name(host, mac_n, controllers)
                    controllers.append(
                        build_controller_dict(host, port, mac_n, label, name)
                    )
                    return await self._async_save_and_reload(
                        controllers, title=entry_title(controllers)
                    )

        return self.async_show_form(
            step_id="add_controller",
            data_schema=_controller_schema(defaults or None),
            errors=errors,
        )

    async def async_step_remove_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a controller from this config entry."""
        controllers = list(self.config_entry.data.get(CONF_CONTROLLERS, []))
        if len(controllers) <= 1:
            return self.async_abort(reason="last_controller")

        choices = {
            c[CONF_NAME]: c.get(CONF_LABEL) or c[CONF_NAME] for c in controllers
        }

        if user_input is not None:
            name = user_input["controller"]
            remaining = [c for c in controllers if c[CONF_NAME] != name]
            if len(remaining) < 1:
                return self.async_abort(reason="last_controller")

            new_unique = self.config_entry.unique_id
            removed_mac = next(
                (_mac_id(c[CONF_MAC]) for c in controllers if c[CONF_NAME] == name),
                None,
            )
            if removed_mac and self.config_entry.unique_id == removed_mac:
                new_unique = _mac_id(remaining[0][CONF_MAC])

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                unique_id=new_unique,
                title=entry_title(remaining),
                data={
                    CONF_CONTROLLERS: remaining,
                    CONF_UNICAST: self.config_entry.data.get(CONF_UNICAST, False),
                },
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="remove_controller",
            data_schema=vol.Schema(
                {vol.Required("controller"): vol.In(choices)}
            ),
        )

    async def _async_save_and_reload(
        self,
        controllers: list[dict[str, Any]],
        *,
        title: str,
    ) -> ConfigFlowResult:
        """Persist controllers and reload the entry."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=title,
            data={
                CONF_CONTROLLERS: controllers,
                CONF_UNICAST: self.config_entry.data.get(CONF_UNICAST, False),
            },
        )
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data={})


async def _mac_configured_elsewhere(
    hass: HomeAssistant, entry_id: str, mac: str
) -> bool:
    """Return True if another config entry already uses this MAC."""
    target = _mac_id(mac)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            continue
        for ctrl in entry.data.get(CONF_CONTROLLERS, []):
            if _mac_id(ctrl.get(CONF_MAC, "")) == target:
                return True
    return False
