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
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

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


def _derive_name(host: str) -> str:
    """Derive an alphanumeric controller name from the host IP."""
    return re.sub(r"[^A-Za-z0-9]", "", host)[:16] or "zen"


def _controller_defaults(entry_data: dict[str, Any]) -> dict[str, Any]:
    """Return form defaults from the first configured controller."""
    controllers = entry_data.get(CONF_CONTROLLERS) or [{}]
    ctrl = controllers[0]
    return {
        CONF_HOST: ctrl.get(CONF_HOST, ""),
        CONF_PORT: ctrl.get(CONF_PORT, DEFAULT_PORT),
        CONF_MAC: ctrl.get(CONF_MAC, ""),
        CONF_LABEL: ctrl.get(CONF_LABEL, ""),
        CONF_UNICAST: entry_data.get(CONF_UNICAST, False),
        CONF_NAME: ctrl.get(CONF_NAME),
    }


def _connection_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the shared user/reconfigure connection schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
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
            vol.Optional(
                CONF_UNICAST,
                default=defaults.get(CONF_UNICAST, False),
            ): bool,
        }
    )


async def _async_discover_mac(hass: HomeAssistant, host: str) -> str | None:
    """Resolve host and look up its MAC via ARP/neighbor discovery.

    Relies on getmac (same approach as some HA core integrations). Only works
    when Home Assistant shares a layer-2 network with the controller; Docker
    bridge networking and routed VLANs often fail — callers must keep MAC
    manually enterable.
    """
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
    """Return True if the controller responds within 5 seconds.

    Uses a dedicated ZenControl instance. Entity identity is scoped per
    ZenControl/ZenProtocol, so this cannot overwrite a running integration's
    entities. A unique controller name is still used for clarity in logs.
    """
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

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}

        if user_input is not None:
            mac = (user_input.get(CONF_MAC) or "").strip()
            if not mac:
                discovered = await _async_discover_mac(
                    self.hass, user_input[CONF_HOST]
                )
                if discovered:
                    # Re-show so the user can confirm the looked-up MAC.
                    defaults = {**user_input, CONF_MAC: discovered}
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_connection_schema(defaults),
                        errors={},
                    )
                errors[CONF_MAC] = "mac_not_found"
            else:
                result = await self._async_validate_and_build(user_input, errors)
                if result is not None:
                    host, port, mac, label, unicast = result
                    name = _derive_name(host)
                    mac_id = mac.replace(":", "")
                    await self.async_set_unique_id(mac_id)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=label,
                        data={
                            CONF_CONTROLLERS: [
                                {
                                    CONF_HOST: host,
                                    CONF_PORT: port,
                                    CONF_MAC: mac,
                                    CONF_NAME: name,
                                    CONF_LABEL: label,
                                }
                            ],
                            CONF_UNICAST: unicast,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(defaults or None),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allow changing host, port, MAC, label, and unicast on an existing entry."""
        reconfigure_entry = self._get_reconfigure_entry()
        defaults = _controller_defaults(dict(reconfigure_entry.data))
        errors: dict[str, str] = {}

        if user_input is not None:
            defaults = {**defaults, **user_input}
            mac = (user_input.get(CONF_MAC) or "").strip()
            if not mac:
                discovered = await _async_discover_mac(
                    self.hass, user_input[CONF_HOST]
                )
                if discovered:
                    defaults = {**defaults, CONF_MAC: discovered}
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=_connection_schema(defaults),
                        errors={},
                    )
                errors[CONF_MAC] = "mac_not_found"
            else:
                result = await self._async_validate_and_build(user_input, errors)
                if result is not None:
                    host, port, mac, label, unicast = result
                    mac_id = mac.replace(":", "")
                    await self.async_set_unique_id(mac_id)
                    # Same-MAC reconfigure must not call _abort_if_unique_id_configured —
                    # that helper aborts against this entry. New MAC: reject conflicts.
                    if reconfigure_entry.unique_id != mac_id:
                        self._abort_if_unique_id_configured()

                    # Keep CONF_NAME stable across IP changes so entity unique_ids
                    # (still derived from name) are not orphaned by a host update.
                    name = defaults.get(CONF_NAME) or _derive_name(host)

                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        unique_id=mac_id,
                        title=label,
                        data={
                            CONF_CONTROLLERS: [
                                {
                                    CONF_HOST: host,
                                    CONF_PORT: port,
                                    CONF_MAC: mac,
                                    CONF_NAME: name,
                                    CONF_LABEL: label,
                                }
                            ],
                            CONF_UNICAST: unicast,
                        },
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults),
            errors=errors,
        )

    async def _async_validate_and_build(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> tuple[str, int, str, str, bool] | None:
        """Validate form input. On success return host/port/mac/label/unicast."""
        host = user_input[CONF_HOST].strip()
        port = user_input[CONF_PORT]
        mac = _normalize_mac(user_input[CONF_MAC])
        label = user_input[CONF_LABEL].strip()
        unicast = user_input.get(CONF_UNICAST, False)

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

        return host, port, mac, label, unicast
