"""Config flow for zencontrol-tpi."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from functools import partial
from typing import Any, Callable

import getmac
import voluptuous as vol
import zencontrol  # type: ignore[import-untyped]
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowType,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import AreaSelector, SelectSelector, SelectSelectorConfig
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_SUB_DEVICES,
    CONF_UNICAST,
    DATA_PENDING_MANIFEST,
    DEFAULT_PORT,
    DOMAIN,
)
from .manifest_store import build_manifest
from .sysvar import classify_sysvar_entity
from .sub_devices import (
    SubDeviceDef,
    parse_sub_device_prefixes,
    sub_device_from_prefixes,
    sub_devices_from_controller,
    sub_devices_to_config,
    validate_sub_device_prefixes,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}([0-9A-Fa-f]{2})$")
CONF_AREA_ID = "area_id"
CONF_PREFIXES = "prefixes"
CONF_DISCOVERED = "discovered"
CONF_ABORT_MANUAL = "abort_manual"
# Options-flow context: open suggest step for this controller name
CTX_SUGGEST_SUB_DEVICES = "suggest_sub_devices_ctrl"
DISCOVERY_LISTEN_SECONDS = 5.0


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
    return "zencontrol controllers"


def build_controller_dict(
    host: str,
    port: int,
    mac: str,
    label: str,
    name: str,
    sub_devices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a persisted controller config dict."""
    data: dict[str, Any] = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_MAC: mac,
        CONF_NAME: name,
        CONF_LABEL: label,
    }
    if sub_devices:
        data[CONF_SUB_DEVICES] = sub_devices
    return data


def _controller_schema(
    defaults: dict[str, Any] | None = None,
    *,
    include_unicast: bool = False,
    include_back: bool = False,
) -> vol.Schema:
    """Build a controller connection schema."""
    defaults = defaults or {}
    if include_back:
        # Optional fields so abort can submit without filling connection details.
        schema: dict[Any, Any] = {
            vol.Optional(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Optional(
                CONF_PORT,
                default=defaults.get(CONF_PORT, DEFAULT_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_MAC, default=defaults.get(CONF_MAC, "")): str,
            vol.Optional(CONF_LABEL, default=defaults.get(CONF_LABEL, "")): str,
        }
    else:
        schema = {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)
            ): str,
            vol.Required(
                CONF_PORT,
                default=defaults.get(CONF_PORT, DEFAULT_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_MAC, default=defaults.get(CONF_MAC, "")): str,
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
    if include_back:
        schema[
            vol.Optional(
                CONF_ABORT_MANUAL,
                default=bool(defaults.get(CONF_ABORT_MANUAL, False)),
            )
        ] = bool
    return vol.Schema(schema)


def _sub_device_schema(
    *,
    prefixes_default: str | None = None,
    area_id: str | None = None,
) -> vol.Schema:
    """Schema for add/reconfigure sub-device (prefixes + optional area)."""
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
    """Normalize optional area selector value."""
    raw = user_input.get(CONF_AREA_ID)
    return str(raw) if raw else None


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


def _discovered_to_dict(discovered: Any) -> dict[str, Any]:
    """Normalize a library DiscoveredController (or mapping) to flow data."""
    if isinstance(discovered, dict):
        return {
            CONF_HOST: str(discovered[CONF_HOST]).strip(),
            CONF_PORT: int(discovered.get(CONF_PORT, DEFAULT_PORT)),
            CONF_MAC: _normalize_mac(str(discovered[CONF_MAC])),
            CONF_LABEL: str(
                discovered.get(CONF_LABEL) or discovered[CONF_MAC]
            ).strip(),
        }
    return {
        CONF_HOST: str(discovered.host).strip(),
        CONF_PORT: int(getattr(discovered, "port", DEFAULT_PORT) or DEFAULT_PORT),
        CONF_MAC: _normalize_mac(str(discovered.mac)),
        CONF_LABEL: str(getattr(discovered, "label", None) or discovered.mac).strip(),
    }


def _discovered_option_label(discovered: dict[str, Any]) -> str:
    """Human-readable label for a discovered controller selector option."""
    label = discovered.get(CONF_LABEL) or discovered[CONF_MAC]
    return f"{label} ({discovered[CONF_HOST]})"


def _selected_macs(user_input: dict[str, Any]) -> list[str]:
    """Normalize multi/single select discovered MAC values."""
    raw = user_input.get(CONF_DISCOVERED)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [_normalize_mac(raw)] if raw else []
    return [_normalize_mac(str(item)) for item in raw if item]


def _merge_discovered(
    *groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge discovered controller lists, keyed by MAC."""
    by_mac: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            by_mac[_mac_id(item[CONF_MAC])] = item
    return list(by_mac.values())


async def _async_listen_for_controllers(
    timeout: float = DISCOVERY_LISTEN_SECONDS,
) -> list[dict[str, Any]]:
    """Listen for multicast and return identified controllers."""
    zen = zencontrol.ZenControl()
    try:
        found = await zen.discover(timeout=timeout)
        return [_discovered_to_dict(item) for item in found]
    except Exception:
        _LOGGER.debug("Multicast discovery listen failed", exc_info=True)
        return []
    finally:
        try:
            await zen.aclose()
        except Exception:
            _LOGGER.debug("Failed to close discovery ZenControl", exc_info=True)


async def _async_append_discovered(
    selected: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    *,
    mac_blocked: Callable[[str], bool] | None = None,
) -> tuple[list[str], str | None]:
    """Validate and append discovered controllers that respond.

    Returns ``(added_names, error_key)``. On total failure, ``existing`` is
    unchanged and ``error_key`` explains why nothing was added.
    """
    to_add: list[dict[str, Any]] = []
    working = list(existing)
    last_error: str | None = None
    for match in selected:
        host = match[CONF_HOST]
        port = int(match.get(CONF_PORT, DEFAULT_PORT))
        mac = match[CONF_MAC]
        label = str(match.get(CONF_LABEL) or mac).strip()
        if any(_mac_id(c[CONF_MAC]) == _mac_id(mac) for c in working):
            last_error = "duplicate_mac"
            continue
        if mac_blocked is not None and mac_blocked(mac):
            last_error = "duplicate_mac"
            continue
        if not await _test_connection(host, port, mac, label):
            last_error = "cannot_connect"
            continue
        name = unique_controller_name(host, mac, working)
        ctrl = build_controller_dict(host, port, mac, label, name)
        to_add.append(ctrl)
        working.append(ctrl)

    if not to_add:
        return [], last_error or "no_devices_found"

    existing.extend(to_add)
    return [c[CONF_NAME] for c in to_add], None


async def _async_prime_discovery(
    hass: HomeAssistant,
    controllers: list[dict[str, Any]],
    *,
    unicast: bool = False,
) -> None:
    """Wait for controllers, discover entities, and stash a pending manifest."""
    zen = zencontrol.ZenControl(unicast=unicast)
    try:
        for idx, cfg in enumerate(controllers, start=1):
            zen.add_controller(
                id=idx,
                name=cfg[CONF_NAME],
                label=cfg[CONF_LABEL],
                host=cfg[CONF_HOST],
                port=int(cfg.get(CONF_PORT, DEFAULT_PORT)),
                mac=cfg.get(CONF_MAC),
            )

        for ctrl in zen.controllers:
            deadline = asyncio.get_running_loop().time() + 60.0
            while True:
                try:
                    ready = await asyncio.wait_for(
                        ctrl.is_controller_ready(), timeout=5.0
                    )
                except TimeoutError:
                    ready = None
                if ready:
                    break
                if ready is None or asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Controller {ctrl.label} ({ctrl.host}) not ready"
                    )
                await asyncio.sleep(2.0)
            await ctrl.interview()

        class _HubSnapshot:
            lights: list[Any]
            groups: list[Any]
            buttons: list[Any]
            motion_sensors: list[Any]
            sv_switches: list[Any]
            sv_sensors: list[Any]
            profiles: list[Any]

        snap = _HubSnapshot()
        snap.lights = sorted(
            await zen.get_lights(), key=lambda lt: lt.address.number
        )
        snap.groups = sorted(
            await zen.get_groups(), key=lambda g: g.address.number
        )
        snap.buttons = sorted(
            await zen.get_buttons(),
            key=lambda b: (b.instance.address.number, b.instance.number),
        )
        snap.motion_sensors = sorted(
            await zen.get_motion_sensors(),
            key=lambda s: (s.instance.address.number, s.instance.number),
        )
        snap.profiles = sorted(
            await zen.get_profiles(),
            key=lambda p: (p.controller.name, p.number),
        )
        snap.sv_switches = []
        snap.sv_sensors = []
        for sv in sorted(await zen.get_system_variables(), key=lambda s: s.id):
            as_sensor, as_switch = classify_sysvar_entity(sv)
            if as_switch:
                snap.sv_switches.append(sv)
            if as_sensor:
                snap.sv_sensors.append(sv)

        manifest = build_manifest(snap)
        hass.data.setdefault(DOMAIN, {})[DATA_PENDING_MANIFEST] = {
            "unique_id": _mac_id(controllers[0][CONF_MAC]),
            "manifest": manifest,
        }
    finally:
        try:
            await zen.aclose()
        except Exception:
            _LOGGER.debug("Failed to close prime-discovery ZenControl", exc_info=True)


class ZencontrolTpiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for zencontrol-tpi."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state for multi-controller setup."""
        self._controllers: list[dict[str, Any]] = []
        self._unicast: bool = False
        self._discovered: list[dict[str, Any]] = []
        self._discovery_info: dict[str, Any] | None = None
        self._discovery_task: asyncio.Task[list[dict[str, Any]]] | None = None
        self._connect_task: asyncio.Task[tuple[list[str], str | None]] | None = None
        self._connect_error: str | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._pending_controllers: list[dict[str, Any]] | None = None
        self._pending_title: str | None = None
        self._reload_next_step: str = "finish"
        self._finish_task: asyncio.Task[None] | None = None
        self._finish_error: str | None = None

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

    async def _async_run_discovery(self) -> list[dict[str, Any]]:
        """Listen for multicast and filter already-known controllers."""
        found = await _async_listen_for_controllers()
        return [
            item
            for item in found
            if not self._mac_in_other_entries(item[CONF_MAC])
            and not self._mac_in_flow(item[CONF_MAC])
        ]

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose discovery or manual controller setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["discover", "manual"],
        )

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual controller setup."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}
        # Abort checkbox only once at least one controller is already collected.
        include_back = bool(self._controllers)

        if user_input is not None:
            if include_back and user_input.get(CONF_ABORT_MANUAL):
                return await self.async_step_add_more()
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="manual",
                include_unicast=True,
                include_back=include_back,
                existing=self._controllers,
            )
            if handled is not None:
                return handled

        return self.async_show_form(
            step_id="manual",
            data_schema=_controller_schema(
                defaults or None,
                include_unicast=True,
                include_back=include_back,
            ),
            errors=errors,
        )

    async def async_step_discover(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Listen for multicast controllers with a progress UI."""
        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(
                self._async_run_discovery()
            )

        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="listen",
                progress_task=self._discovery_task,
            )

        try:
            self._discovered = self._discovery_task.result()
        except Exception:
            _LOGGER.debug("Discovery task failed", exc_info=True)
            self._discovered = []
        self._discovery_task = None

        if not self._discovered:
            return self.async_show_progress_done(next_step_id="discovery_failed")
        return self.async_show_progress_done(next_step_id="select_discovered")

    async def async_step_discovery_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """No controllers found — try again, enter manually, or go back."""
        if self._controllers:
            return self.async_show_menu(
                step_id="discovery_failed",
                menu_options=["discover", "add_another", "finish"],
            )
        return self.async_show_menu(
            step_id="discovery_failed",
            menu_options=["discover", "manual"],
        )

    async def async_step_select_discovered(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select one or more controllers found via multicast."""
        if self._connect_task is not None:
            if not self._connect_task.done():
                return self.async_show_progress(
                    step_id="select_discovered",
                    progress_action="connect_controllers",
                    progress_task=self._connect_task,
                )
            try:
                _added, error = self._connect_task.result()
            except Exception:
                _LOGGER.debug("Connect task failed", exc_info=True)
                error = "cannot_connect"
            self._connect_task = None
            if error:
                if self._controllers:
                    return self.async_show_progress_done(
                        next_step_id="discovery_failed"
                    )
                self._connect_error = error
                return self.async_show_progress_done(next_step_id="select_discovered")
            return self.async_show_progress_done(next_step_id="add_more")

        errors: dict[str, str] = {}
        if self._connect_error:
            errors["base" if self._connect_error == "cannot_connect" else CONF_MAC] = (
                self._connect_error
            )
            self._connect_error = None

        options = [
            {"value": item[CONF_MAC], "label": _discovered_option_label(item)}
            for item in self._discovered
        ]
        default_macs = [item[CONF_MAC] for item in self._discovered]

        if user_input is not None:
            selected_macs = {_mac_id(mac) for mac in _selected_macs(user_input)}
            selected = [
                item
                for item in self._discovered
                if _mac_id(item[CONF_MAC]) in selected_macs
            ]
            if not selected:
                return await self.async_step_discovery_failed()
            self._connect_task = self.hass.async_create_task(
                _async_append_discovered(
                    selected,
                    self._controllers,
                    mac_blocked=self._mac_in_other_entries,
                )
            )
            return self.async_show_progress(
                step_id="select_discovered",
                progress_action="connect_controllers",
                progress_task=self._connect_task,
            )

        return self.async_show_form(
            step_id="select_discovered",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DISCOVERED, default=default_macs
                    ): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a controller discovered while the integration is running."""
        info = _discovered_to_dict(discovery_info)
        mac = info[CONF_MAC]
        await self.async_set_unique_id(_mac_id(mac))
        self._abort_if_unique_id_configured()

        if self._mac_in_other_entries(mac):
            return self.async_abort(reason="already_configured")

        self._discovery_info = info
        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm adding a controller found via multicast discovery."""
        info = self._discovery_info
        if info is None:
            return self.async_abort(reason="no_devices_found")

        host = info[CONF_HOST]
        port = int(info.get(CONF_PORT, DEFAULT_PORT))
        mac = info[CONF_MAC]
        label = str(info.get(CONF_LABEL) or mac).strip()

        if user_input is not None:
            if self._mac_in_other_entries(mac):
                return self.async_abort(reason="already_configured")
            if not await _test_connection(host, port, mac, label):
                return self.async_abort(reason="cannot_connect")

            entries = self._async_current_entries()
            if entries:
                entry = entries[0]
                controllers = list(entry.data.get(CONF_CONTROLLERS, []))
                if any(_mac_id(c.get(CONF_MAC, "")) == _mac_id(mac) for c in controllers):
                    return self.async_abort(reason="already_configured")
                name = unique_controller_name(host, mac, controllers)
                controllers.append(
                    build_controller_dict(host, port, mac, label, name)
                )
                self._pending_controllers = controllers
                self._pending_title = entry_title(controllers)
                self._reload_next_step = "discovery_added"
                # Bind entry for loading_devices (config flow, not options)
                self._reload_entry = entry
                return await self.async_step_loading_devices()

            name = unique_controller_name(host, mac, [])
            self._controllers = [
                build_controller_dict(host, port, mac, label, name)
            ]
            return await self.async_step_finish()

        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "label": label,
                "host": host,
                "mac": mac,
            },
        )

    async def async_step_discovery_added(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Abort after a discovered controller was added to an existing entry."""
        return self.async_abort(reason="controller_added")

    async def async_step_loading_devices(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Persist controllers and reload while showing a progress UI."""
        if self._reload_task is None:
            self._reload_task = self.hass.async_create_task(
                self._async_persist_and_reload_entry()
            )

        if not self._reload_task.done():
            return self.async_show_progress(
                step_id="loading_devices",
                progress_action="setup_devices",
                progress_task=self._reload_task,
            )

        try:
            self._reload_task.result()
        except Exception:
            _LOGGER.debug("Reload after discovery failed", exc_info=True)
            self._reload_task = None
            return self.async_show_progress_done(next_step_id="reload_failed")
        self._reload_task = None
        return self.async_show_progress_done(next_step_id=self._reload_next_step)

    async def async_step_reload_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Abort after a progress-step reload failure."""
        return self.async_abort(reason="cannot_connect")

    async def _async_persist_and_reload_entry(self) -> None:
        """Update the existing entry and await a full reload."""
        entry = getattr(self, "_reload_entry", None)
        controllers = self._pending_controllers
        title = self._pending_title
        if entry is None or controllers is None or title is None:
            raise RuntimeError("Missing pending controller update")
        self.hass.config_entries.async_update_entry(
            entry,
            title=title,
            data={
                CONF_CONTROLLERS: controllers,
                CONF_UNICAST: entry.data.get(CONF_UNICAST, False),
            },
        )
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def async_step_add_another(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Add an additional controller during initial setup."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}
        include_back = bool(self._controllers)

        if user_input is not None:
            if include_back and user_input.get(CONF_ABORT_MANUAL):
                return await self.async_step_add_more()
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="add_another",
                include_unicast=False,
                include_back=include_back,
                existing=self._controllers,
            )
            if handled is not None:
                return handled
            if self._controllers and errors.get("base") == "cannot_connect":
                return await self.async_step_discovery_failed()

        return self.async_show_form(
            step_id="add_another",
            data_schema=_controller_schema(
                defaults or None,
                include_unicast=False,
                include_back=include_back,
            ),
            errors=errors,
        )

    async def async_step_add_more(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Ask whether to discover/add another controller or finish."""
        return self.async_show_menu(
            step_id="add_more",
            menu_options=["discover", "add_another", "finish"],
        )

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prime discovery while showing progress, then create the entry."""
        if not self._controllers:
            return self.async_abort(reason="no_controllers")

        if self._finish_task is None:
            self._finish_task = self.hass.async_create_task(
                _async_prime_discovery(
                    self.hass,
                    self._controllers,
                    unicast=self._unicast,
                )
            )

        if not self._finish_task.done():
            return self.async_show_progress(
                step_id="finish",
                progress_action="setup_devices",
                progress_task=self._finish_task,
            )

        try:
            self._finish_task.result()
        except Exception:
            _LOGGER.debug("Finish discovery priming failed", exc_info=True)
            self._finish_task = None
            # Keep already-collected controllers; let the user retry or finish later.
            return self.async_show_progress_done(next_step_id="add_more")
        self._finish_task = None
        # Progress steps may only transition to progress / progress_done.
        return self.async_show_progress_done(next_step_id="create_entry")

    async def async_step_create_entry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the config entry after setup-devices progress completes."""
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

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Continue into options to suggest sub-devices (skips Name and assign)."""
        entry = result["result"]
        ctrl_name = self._controllers[0][CONF_NAME] if self._controllers else None
        options_result = await self.hass.config_entries.options.async_init(
            entry.entry_id,
            context={"source": SOURCE_USER},
            data={CTX_SUGGEST_SUB_DEVICES: ctrl_name},
        )
        result["next_flow"] = (FlowType.OPTIONS_FLOW, options_result["flow_id"])
        return result

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
                            host,
                            port,
                            mac,
                            label,
                            name,
                            sub_devices=current.get(CONF_SUB_DEVICES),
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
        include_back: bool = False,
    ) -> ConfigFlowResult | None:
        """Validate and append a controller, or re-show for MAC confirm.

        Returns a ConfigFlowResult when navigation should continue, else None
        to show the form with ``errors``.
        """
        host = str(user_input.get(CONF_HOST, "")).strip()
        mac = (user_input.get(CONF_MAC) or "").strip()
        if not mac:
            if not host:
                errors[CONF_HOST] = "invalid_host"
                return None
            discovered = await _async_discover_mac(self.hass, host)
            if discovered:
                defaults.clear()
                defaults.update({**user_input, CONF_MAC: discovered})
                return self.async_show_form(
                    step_id=step_id,
                    data_schema=_controller_schema(
                        defaults,
                        include_unicast=include_unicast,
                        include_back=include_back,
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
        host = str(user_input.get(CONF_HOST, "")).strip()
        port = user_input.get(CONF_PORT, DEFAULT_PORT)
        mac = _normalize_mac(str(user_input.get(CONF_MAC, "")))
        label = str(user_input.get(CONF_LABEL, "")).strip()

        if not host:
            errors[CONF_HOST] = "invalid_host"
            return None
        if not _MAC_RE.match(mac):
            errors[CONF_MAC] = "invalid_mac"
            return None
        if not label:
            errors[CONF_LABEL] = "invalid_label"
            return None

        reachable = await _test_connection(host, int(port), mac, label)
        if not reachable:
            errors["base"] = "cannot_connect"
            return None

        return host, int(port), mac, label


class ZencontrolTpiOptionsFlow(OptionsFlow):
    """Options flow: controllers → sub-devices → reconfigure/delete."""

    _ctrl_name: str | None = None
    _sub_device_id: str | None = None
    # After saving a sub-device, return to this step instead of closing.
    _return_after_save: str | None = None
    _suggest_from_setup_handled: bool = False
    _discovered: list[dict[str, Any]] | None = None
    _discovery_task: asyncio.Task[list[dict[str, Any]]] | None = None
    _reload_task: asyncio.Task[None] | None = None
    _pending_controllers: list[dict[str, Any]] | None = None
    _pending_title: str | None = None
    _reload_next_step: str = "suggest_sub_devices"

    def __getattr__(self, name: str) -> Any:
        """Route dynamic menu steps for controllers and sub-devices."""
        if name.startswith("async_step_ctrl_"):
            ctrl_name = name.removeprefix("async_step_ctrl_")

            async def async_step_ctrl(
                user_input: dict[str, Any] | None = None,
            ) -> ConfigFlowResult:
                self._ctrl_name = ctrl_name
                return await self.async_step_controller()

            return async_step_ctrl

        if name.startswith("async_step_subdev_"):
            sub_id = name.removeprefix("async_step_subdev_")

            async def async_step_subdev(
                user_input: dict[str, Any] | None = None,
            ) -> ConfigFlowResult:
                self._sub_device_id = sub_id
                return await self.async_step_sub_device()

            return async_step_subdev

        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    async def _options_label(self, key: str, default: str) -> str:
        """Load an options-flow string for the current language."""
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "options", {DOMAIN}
        )
        return translations.get(f"component.{DOMAIN}.{key}", default)

    def _controllers(self) -> list[dict[str, Any]]:
        return list(self.config_entry.data.get(CONF_CONTROLLERS, []))

    def _controller(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return next((c for c in self._controllers() if c[CONF_NAME] == name), None)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List controllers plus Add controller, or open suggest after setup."""
        init_data = self.init_data if isinstance(self.init_data, dict) else {}
        suggest_ctrl = init_data.get(CTX_SUGGEST_SUB_DEVICES)
        if suggest_ctrl and not self._suggest_from_setup_handled:
            self._suggest_from_setup_handled = True
            self._ctrl_name = str(suggest_ctrl)
            return await self.async_step_suggest_sub_devices()

        menu_options: dict[str, str] = {
            f"ctrl_{c[CONF_NAME]}": str(c.get(CONF_LABEL) or c[CONF_NAME])
            for c in self._controllers()
        }
        menu_options["discover"] = await self._options_label(
            "step.init.menu_options.discover",
            "Discover another controller",
        )
        menu_options["add_controller"] = await self._options_label(
            "step.init.menu_options.add_controller",
            "Add another controller manually",
        )
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_suggest_sub_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """After adding a controller, offer to create sub-devices."""
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        self._return_after_save = "suggest_sub_devices"
        return self.async_show_menu(
            step_id="suggest_sub_devices",
            menu_options=["add_sub_device", "finish_setup"],
            description_placeholders={
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_finish_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Close options after declining or finishing sub-device setup."""
        self._return_after_save = None
        return self.async_create_entry(title="", data={})

    async def async_step_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List this controller's sub-devices plus Add / Remove controller."""
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        self._return_after_save = "controller"
        devices = sub_devices_from_controller(ctrl)
        menu_options: dict[str, str] = {
            "add_sub_device": await self._options_label(
                "step.controller.menu_options.add_sub_device",
                "➕ Add sub-device",
            ),
        }
        for d in devices:
            prefixes = ", ".join(d.prefixes)
            label = d.name if prefixes == d.name else f"{d.name} ({prefixes})"
            menu_options[f"subdev_{d.id}"] = label
        menu_options["remove_controller"] = await self._options_label(
            "step.controller.menu_options.remove_controller",
            "❌ Remove controller",
        )
        return self.async_show_menu(
            step_id="controller",
            menu_options=menu_options,
            description_placeholders={
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_sub_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure or delete the selected sub-device."""
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        device = next(
            (
                d
                for d in sub_devices_from_controller(ctrl)
                if d.id == self._sub_device_id
            ),
            None,
        )
        if device is None:
            return await self.async_step_controller()

        return self.async_show_menu(
            step_id="sub_device",
            menu_options=["reconfigure_sub_device", "delete_sub_device"],
            description_placeholders={
                "sub_device": device.name,
                "prefixes": ", ".join(device.prefixes),
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_add_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a controller manually to this config entry."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}
        controllers = self._controllers()

        if user_input is not None:
            mac = (user_input.get(CONF_MAC) or "").strip()
            host = str(user_input.get(CONF_HOST, "")).strip()
            if not mac:
                if not host:
                    errors[CONF_HOST] = "invalid_host"
                else:
                    discovered_mac = await _async_discover_mac(self.hass, host)
                    if discovered_mac:
                        defaults = {**user_input, CONF_MAC: discovered_mac}
                        return self.async_show_form(
                            step_id="add_controller",
                            data_schema=_controller_schema(defaults),
                            errors={},
                        )
                    errors[CONF_MAC] = "mac_not_found"
            else:
                port = user_input.get(CONF_PORT, DEFAULT_PORT)
                mac_n = _normalize_mac(user_input[CONF_MAC])
                label = str(user_input.get(CONF_LABEL, "")).strip()
                if not host:
                    errors[CONF_HOST] = "invalid_host"
                elif not _MAC_RE.match(mac_n):
                    errors[CONF_MAC] = "invalid_mac"
                elif not label:
                    errors[CONF_LABEL] = "invalid_label"
                elif any(_mac_id(c[CONF_MAC]) == _mac_id(mac_n) for c in controllers):
                    errors[CONF_MAC] = "duplicate_mac"
                elif await _mac_configured_elsewhere(
                    self.hass, self.config_entry.entry_id, mac_n
                ):
                    errors[CONF_MAC] = "duplicate_mac"
                elif not await _test_connection(host, int(port), mac_n, label):
                    errors["base"] = "cannot_connect"
                else:
                    name = unique_controller_name(host, mac_n, controllers)
                    controllers.append(
                        build_controller_dict(host, int(port), mac_n, label, name)
                    )
                    self._ctrl_name = name
                    self._pending_controllers = controllers
                    self._pending_title = entry_title(controllers)
                    self._reload_next_step = "suggest_sub_devices"
                    return await self.async_step_loading_devices()

        return self.async_show_form(
            step_id="add_controller",
            data_schema=_controller_schema(defaults or None),
            errors=errors,
        )

    def _unconfigured_discovered(self) -> list[dict[str, Any]]:
        """Return multicast-discovered controllers not yet in this entry."""
        hub = getattr(self.config_entry, "runtime_data", None)
        zen = getattr(hub, "zen", None) if hub is not None else None
        if zen is None:
            return []
        configured = {
            _mac_id(c.get(CONF_MAC, ""))
            for c in self._controllers()
            if c.get(CONF_MAC)
        }
        result: list[dict[str, Any]] = []
        for item in getattr(zen, "discovered_controllers", []) or []:
            data = _discovered_to_dict(item)
            if _mac_id(data[CONF_MAC]) not in configured:
                result.append(data)
        return result

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Listen for multicast controllers with a progress UI."""
        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(
                self._async_run_discovery()
            )

        if not self._discovery_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="listen",
                progress_task=self._discovery_task,
            )

        try:
            self._discovered = self._discovery_task.result()
        except Exception:
            _LOGGER.debug("Discovery task failed", exc_info=True)
            self._discovered = []
        self._discovery_task = None

        if not self._discovered:
            return self.async_show_progress_done(next_step_id="discovery_failed")
        return self.async_show_progress_done(next_step_id="add_discovered")

    async def _async_run_discovery(self) -> list[dict[str, Any]]:
        """Listen for multicast and merge with already-identified controllers."""
        configured = {
            _mac_id(c.get(CONF_MAC, ""))
            for c in self._controllers()
            if c.get(CONF_MAC)
        }
        found = await _async_listen_for_controllers()
        return _merge_discovered(
            self._unconfigured_discovered(),
            [
                item
                for item in found
                if _mac_id(item[CONF_MAC]) not in configured
            ],
        )

    async def async_step_discovery_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No controllers found — try again or enter manually."""
        return self.async_show_menu(
            step_id="discovery_failed",
            menu_options=["discover", "add_controller"],
        )

    async def async_step_add_discovered(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one or more discovered controllers to add."""
        discovered = getattr(self, "_discovered", None) or self._unconfigured_discovered()
        self._discovered = discovered
        if not discovered:
            return await self.async_step_discovery_failed()

        errors: dict[str, str] = {}
        options = [
            {"value": item[CONF_MAC], "label": _discovered_option_label(item)}
            for item in discovered
        ]
        default_macs = [item[CONF_MAC] for item in discovered]

        if user_input is not None:
            selected_macs = {_mac_id(mac) for mac in _selected_macs(user_input)}
            selected = [
                item
                for item in discovered
                if _mac_id(item[CONF_MAC]) in selected_macs
            ]
            if not selected:
                return await self.async_step_discovery_failed()
            controllers = self._controllers()
            entry_id = self.config_entry.entry_id

            def mac_blocked(mac: str) -> bool:
                target = _mac_id(mac)
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.entry_id == entry_id:
                        continue
                    for ctrl in entry.data.get(CONF_CONTROLLERS, []):
                        if _mac_id(ctrl.get(CONF_MAC, "")) == target:
                            return True
                return False

            added, error = await _async_append_discovered(
                selected,
                controllers,
                mac_blocked=mac_blocked,
            )
            if error:
                errors["base" if error != "duplicate_mac" else CONF_MAC] = error
            else:
                self._ctrl_name = added[-1]
                self._pending_controllers = controllers
                self._pending_title = entry_title(controllers)
                self._reload_next_step = "suggest_sub_devices"
                return await self.async_step_loading_devices()

        return self.async_show_form(
            step_id="add_discovered",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DISCOVERED, default=default_macs
                    ): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_loading_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist controllers and reload while showing a progress UI."""
        if self._reload_task is None:
            self._reload_task = self.hass.async_create_task(
                self._async_run_pending_reload()
            )

        if not self._reload_task.done():
            return self.async_show_progress(
                step_id="loading_devices",
                progress_action="setup_devices",
                progress_task=self._reload_task,
            )

        try:
            self._reload_task.result()
        except Exception:
            _LOGGER.debug("Options reload failed", exc_info=True)
            self._reload_task = None
            return self.async_show_progress_done(next_step_id="reload_failed")
        self._reload_task = None
        return self.async_show_progress_done(next_step_id=self._reload_next_step)

    async def async_step_reload_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Abort after a progress-step reload failure."""
        return self.async_abort(reason="cannot_connect")

    async def _async_run_pending_reload(self) -> None:
        """Write pending controllers and reload the config entry."""
        controllers = self._pending_controllers
        title = self._pending_title
        if controllers is None or title is None:
            raise RuntimeError("Missing pending controller update")
        await self._async_persist_controllers(
            controllers, title=title, reload=True
        )

    async def async_step_remove_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and remove the selected controller."""
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        controllers = self._controllers()
        if len(controllers) <= 1:
            return self.async_abort(reason="last_controller")

        if user_input is not None:
            name = ctrl[CONF_NAME]
            remaining = [c for c in controllers if c[CONF_NAME] != name]
            new_unique = self.config_entry.unique_id
            removed_mac = _mac_id(ctrl[CONF_MAC])
            if self.config_entry.unique_id == removed_mac:
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
            data_schema=vol.Schema({}),
            description_placeholders={
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_add_sub_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a sub-device from a comma-separated prefix list."""
        errors: dict[str, str] = {}
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl)

        if user_input is not None:
            prefixes = parse_sub_device_prefixes(user_input.get(CONF_PREFIXES, ""))
            err = validate_sub_device_prefixes(existing, prefixes)
            if err:
                errors[CONF_PREFIXES] = err
            else:
                device = sub_device_from_prefixes(prefixes)
                assert device is not None
                area_id = _area_id_from_input(user_input)
                ids = {d.id for d in existing}
                device_id = device.id
                base_id = device.id
                n = 2
                while device_id in ids:
                    device_id = f"{base_id}_{n}"
                    n += 1
                existing.append(
                    SubDeviceDef(
                        id=device_id,
                        name=device.name,
                        prefixes=device.prefixes,
                        area_id=area_id,
                    )
                )
                ctrl[CONF_SUB_DEVICES] = sub_devices_to_config(existing)
                return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="add_sub_device",
            data_schema=_sub_device_schema(),
            errors=errors,
            description_placeholders={
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_reconfigure_sub_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit prefixes for the selected sub-device (id stays stable)."""
        errors: dict[str, str] = {}
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl)
        device = next((d for d in existing if d.id == self._sub_device_id), None)
        if device is None:
            return await self.async_step_controller()

        if user_input is not None:
            prefixes = parse_sub_device_prefixes(user_input.get(CONF_PREFIXES, ""))
            err = validate_sub_device_prefixes(
                existing, prefixes, replacing_id=device.id
            )
            if err:
                errors[CONF_PREFIXES] = err
            else:
                updated = SubDeviceDef(
                    id=device.id,
                    name=prefixes[0],
                    prefixes=tuple(prefixes),
                    area_id=_area_id_from_input(user_input),
                )
                ctrl[CONF_SUB_DEVICES] = sub_devices_to_config(
                    [updated if d.id == device.id else d for d in existing]
                )
                return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="reconfigure_sub_device",
            data_schema=_sub_device_schema(
                prefixes_default=",".join(device.prefixes),
                area_id=device.area_id,
            ),
            errors=errors,
            description_placeholders={
                "sub_device": device.name,
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def async_step_delete_sub_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and delete the selected sub-device."""
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return await self.async_step_init()

        controllers = self._controllers()
        existing = sub_devices_from_controller(ctrl)
        device = next((d for d in existing if d.id == self._sub_device_id), None)
        if device is None:
            return await self.async_step_controller()

        if user_input is not None:
            remaining = [d for d in existing if d.id != device.id]
            if remaining:
                ctrl[CONF_SUB_DEVICES] = sub_devices_to_config(remaining)
            else:
                ctrl.pop(CONF_SUB_DEVICES, None)
            return await self._async_save_sub_devices(controllers)

        return self.async_show_form(
            step_id="delete_sub_device",
            data_schema=vol.Schema({}),
            description_placeholders={
                "sub_device": device.name,
                "controller": ctrl.get(CONF_LABEL) or ctrl[CONF_NAME],
            },
        )

    async def _async_save_sub_devices(
        self,
        controllers: list[dict[str, Any]],
    ) -> ConfigFlowResult:
        """Persist sub-device config and reassign entities without rediscovery."""
        await self._async_persist_controllers(
            controllers, title=entry_title(controllers), reload=False
        )
        hub = self.config_entry.runtime_data
        if hub is not None:
            hub.apply_sub_device_config()

        if self._return_after_save == "suggest_sub_devices":
            return await self.async_step_suggest_sub_devices()
        if self._return_after_save == "controller":
            return await self.async_step_controller()
        return self.async_create_entry(title="", data={})

    async def _async_persist_controllers(
        self,
        controllers: list[dict[str, Any]],
        *,
        title: str,
        reload: bool,
    ) -> None:
        """Write controllers into the config entry, optionally reloading."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=title,
            data={
                CONF_CONTROLLERS: controllers,
                CONF_UNICAST: self.config_entry.data.get(CONF_UNICAST, False),
            },
        )
        if reload:
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)

    async def _async_save_and_reload(
        self,
        controllers: list[dict[str, Any]],
        *,
        title: str,
    ) -> ConfigFlowResult:
        """Persist controllers, reload the entry, and close the options flow."""
        await self._async_persist_controllers(controllers, title=title, reload=True)
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
