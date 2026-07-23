"""Config flow for zencontrol-tpi.

One Home Assistant config entry per physical controller. Controllers share a
single ZenControl runtime (see runtime.py / hub.py).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from functools import partial
from typing import Any, Literal

import getmac
import voluptuous as vol
import zencontrol  # type: ignore[import-untyped]
from homeassistant.config_entries import (
    SOURCE_IMPORT,
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowType,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    AreaSelector,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_SUB_DEVICES,
    CONF_UNICAST,
    CONFIG_VERSION,
    DATA_PENDING_MANIFEST,
    DEFAULT_PORT,
    DOMAIN,
    normalize_mac,
    normalize_mac_id,
)
from .manifest_store import build_manifest
from .sub_devices import (
    SubDeviceDef,
    parse_sub_device_prefixes,
    sub_device_from_prefixes,
    sub_devices_from_controller,
    sub_devices_to_config,
    validate_sub_device_prefixes,
)
from .sysvar import classify_sysvar_entity

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}([0-9A-Fa-f]{2})$")
CONF_AREA_ID = "area_id"
CONF_PREFIXES = "prefixes"
CONF_DISCOVERED = "discovered"
# Options-flow context: open suggest step for this controller name
CTX_SUGGEST_SUB_DEVICES = "suggest_sub_devices_ctrl"
DISCOVERY_LISTEN_SECONDS = 5.0

type ControllerConfig = dict[str, Any]
type SaveReturnStep = Literal["suggest_sub_devices", "controller"] | None


def _derive_name(host: str) -> str:
    """Derive an alphanumeric controller name from the host IP."""
    return re.sub(r"[^A-Za-z0-9]", "", host)[:16] or "zen"


def _controllers_from_all_entries(hass: HomeAssistant) -> list[ControllerConfig]:
    """Return every controller config across all domain entries."""
    result: list[ControllerConfig] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        for ctrl in entry.data.get(CONF_CONTROLLERS, []):
            if isinstance(ctrl, dict):
                result.append(ctrl)
    return result


def unique_controller_name(
    host: str, mac: str, existing: list[ControllerConfig]
) -> str:
    """Return a name unique among existing controllers (all domain entries)."""
    names = {c.get(CONF_NAME) for c in existing}
    base = _derive_name(host)
    if base not in names:
        return base
    suffix = normalize_mac_id(mac)[-4:].lower()
    candidate = f"{base}{suffix}"[:16]
    if candidate not in names:
        return candidate
    n = 2
    while True:
        candidate = f"{base}{n}"[:16]
        if candidate not in names:
            return candidate
        n += 1


def entry_title(controller: ControllerConfig) -> str:
    """Human-readable config entry title (label, else name)."""
    return str(
        controller.get(CONF_LABEL) or controller.get(CONF_NAME) or "zencontrol"
    )


def build_controller_dict(
    host: str,
    port: int,
    mac: str,
    label: str,
    name: str,
    sub_devices: list[dict[str, Any]] | None = None,
) -> ControllerConfig:
    """Build a persisted controller config dict."""
    data: ControllerConfig = {
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
    return normalize_mac(mac)


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


def _discovered_to_dict(discovered: Any) -> ControllerConfig:
    """Normalize a library DiscoveredController (or mapping) to flow data."""
    match discovered:
        case dict() as data:
            return {
                CONF_HOST: str(data[CONF_HOST]).strip(),
                CONF_PORT: int(data.get(CONF_PORT, DEFAULT_PORT)),
                CONF_MAC: normalize_mac(str(data[CONF_MAC])),
                CONF_LABEL: str(data.get(CONF_LABEL) or data[CONF_MAC]).strip(),
            }
        case _:
            return {
                CONF_HOST: str(discovered.host).strip(),
                CONF_PORT: int(
                    getattr(discovered, "port", DEFAULT_PORT) or DEFAULT_PORT
                ),
                CONF_MAC: normalize_mac(str(discovered.mac)),
                CONF_LABEL: str(
                    getattr(discovered, "label", None) or discovered.mac
                ).strip(),
            }


def _discovered_option_label(discovered: ControllerConfig) -> str:
    """Human-readable label for a discovered controller selector option."""
    label = discovered.get(CONF_LABEL) or discovered[CONF_MAC]
    return f"{label} ({discovered[CONF_HOST]})"


def _selected_mac(user_input: dict[str, Any]) -> str | None:
    """Normalize a single-select discovered MAC value."""
    match user_input.get(CONF_DISCOVERED):
        case None:
            return None
        case str() as raw if raw:
            return normalize_mac(raw)
        case str():
            return None
        case items:
            for item in items:
                if item:
                    return normalize_mac(str(item))
            return None


async def _async_listen_for_controllers(
    hass: HomeAssistant,
    timeout: float = DISCOVERY_LISTEN_SECONDS,
) -> list[ControllerConfig]:
    """Listen for multicast and return identified controllers.

    When the shared runtime already has a listener, reuse it so we do not bind
    a second multicast socket (SO_REUSEPORT can drop events on Linux).
    """
    from .runtime import async_get_runtime

    runtime = async_get_runtime(hass)
    if runtime is not None and runtime.listener_up:
        before = {
            (
                normalize_mac(str(d.mac)),
                str(d.host),
            )
            for d in runtime.zen.discovered_controllers
        }
        await asyncio.sleep(timeout)
        return [
            _discovered_to_dict(item)
            for item in runtime.zen.discovered_controllers
            if (normalize_mac(str(item.mac)), str(item.host)) not in before
        ]

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


async def _async_prime_discovery(
    hass: HomeAssistant,
    controller: ControllerConfig,
    *,
    unicast: bool = False,
) -> None:
    """Wait for the controller, discover entities, and stash a pending manifest."""
    zen = zencontrol.ZenControl(unicast=unicast)
    try:
        zen.add_controller(
            id=1,
            name=controller[CONF_NAME],
            label=controller[CONF_LABEL],
            host=controller[CONF_HOST],
            port=int(controller.get(CONF_PORT, DEFAULT_PORT)),
            mac=controller.get(CONF_MAC),
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
        mac_id = normalize_mac_id(controller[CONF_MAC])
        hass.data.setdefault(DOMAIN, {}).setdefault(DATA_PENDING_MANIFEST, {})[
            mac_id
        ] = {"manifest": manifest}
    finally:
        try:
            await zen.aclose()
        except Exception:
            _LOGGER.debug("Failed to close prime-discovery ZenControl", exc_info=True)


def _async_relink_migrated_devices(
    hass: HomeAssistant,
    *,
    old_entry_id: str,
    new_entry_id: str,
    mac: str,
) -> None:
    """Move devices for this MAC (and its sub-devices) from old entry to new."""
    device_registry = dr.async_get(hass)
    mac_norm = normalize_mac(mac)
    mac_id = normalize_mac_id(mac)
    sub_prefix = f"{mac_norm}:sub:"
    for device in list(
        dr.async_entries_for_config_entry(device_registry, old_entry_id)
    ):
        domain_idents = [ident for ident in device.identifiers if ident[0] == DOMAIN]
        if not domain_idents:
            continue
        if not any(
            ident == mac_norm
            or ident == mac_id
            or ident.startswith(sub_prefix)
            for _, ident in domain_idents
        ):
            continue
        device_registry.async_update_device(
            device.id,
            add_config_entry_id=new_entry_id,
            remove_config_entry_id=old_entry_id,
        )


class ZencontrolTpiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for zencontrol-tpi (one entry per controller)."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        """Initialize flow state for single-controller setup."""
        self._controller: ControllerConfig | None = None
        self._unicast: bool = False
        self._discovered: list[ControllerConfig] = []
        self._discovery_info: dict[str, Any] | None = None
        self._discovery_task: asyncio.Task[list[ControllerConfig]] | None = None
        self._connect_task: asyncio.Task[str | None] | None = None
        self._connect_error: str | None = None
        self._finish_task: asyncio.Task[None] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for managing sub-devices."""
        return ZencontrolTpiOptionsFlow()

    def _mac_in_other_entries(
        self, mac: str, *, ignore_entry_id: str | None = None
    ) -> bool:
        """Return True if another config entry already uses this MAC."""
        target = normalize_mac_id(mac)
        for entry in self._async_current_entries():
            if ignore_entry_id and entry.entry_id == ignore_entry_id:
                continue
            for ctrl in entry.data.get(CONF_CONTROLLERS, []):
                if normalize_mac_id(ctrl.get(CONF_MAC, "")) == target:
                    return True
        return False

    def _existing_unicast(self) -> bool:
        """Copy unicast from an existing entry when adding another controller."""
        entries = self._async_current_entries()
        if not entries:
            return False
        return bool(entries[0].data.get(CONF_UNICAST, False))

    async def _async_run_discovery(self) -> list[ControllerConfig]:
        """Listen for multicast and filter already-configured controllers."""
        found = await _async_listen_for_controllers(self.hass)
        return [
            item
            for item in found
            if not self._mac_in_other_entries(item[CONF_MAC])
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
        """Handle manual controller setup for a single controller."""
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = dict(user_input) if user_input else {}
        # Unicast only matters when creating the shared runtime (first entry).
        include_unicast = not self._async_current_entries()

        if user_input is not None:
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="manual",
                include_unicast=include_unicast,
            )
            if handled is not None:
                return handled

        if include_unicast and CONF_UNICAST not in defaults:
            defaults[CONF_UNICAST] = False

        return self.async_show_form(
            step_id="manual",
            data_schema=_controller_schema(
                defaults or None,
                include_unicast=include_unicast,
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
        """No controllers found — try again or enter manually."""
        return self.async_show_menu(
            step_id="discovery_failed",
            menu_options=["discover", "manual"],
        )

    async def async_step_select_discovered(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select a single controller found via multicast."""
        if self._connect_task is not None:
            if not self._connect_task.done():
                return self.async_show_progress(
                    step_id="select_discovered",
                    progress_action="connect_controllers",
                    progress_task=self._connect_task,
                )
            try:
                error = self._connect_task.result()
            except Exception:
                _LOGGER.debug("Connect task failed", exc_info=True)
                error = "cannot_connect"
            self._connect_task = None
            if error:
                self._connect_error = error
                return self.async_show_progress_done(next_step_id="select_discovered")
            return self.async_show_progress_done(next_step_id="finish")

        errors: dict[str, str] = {}
        if self._connect_error:
            match self._connect_error:
                case "cannot_connect":
                    errors["base"] = self._connect_error
                case _:
                    errors[CONF_MAC] = self._connect_error
            self._connect_error = None

        options = [
            {"value": item[CONF_MAC], "label": _discovered_option_label(item)}
            for item in self._discovered
        ]
        default_mac = self._discovered[0][CONF_MAC] if self._discovered else None

        if user_input is not None:
            selected_mac = _selected_mac(user_input)
            selected = next(
                (
                    item
                    for item in self._discovered
                    if selected_mac
                    and normalize_mac_id(item[CONF_MAC])
                    == normalize_mac_id(selected_mac)
                ),
                None,
            )
            if selected is None:
                return await self.async_step_discovery_failed()
            self._connect_task = self.hass.async_create_task(
                self._async_connect_discovered(selected)
            )
            return self.async_show_progress(
                step_id="select_discovered",
                progress_action="connect_controllers",
                progress_task=self._connect_task,
            )

        schema_field: Any
        if default_mac is not None:
            schema_field = vol.Required(CONF_DISCOVERED, default=default_mac)
        else:
            schema_field = vol.Required(CONF_DISCOVERED)

        return self.async_show_form(
            step_id="select_discovered",
            data_schema=vol.Schema(
                {
                    schema_field: SelectSelector(
                        SelectSelectorConfig(options=options, multiple=False)
                    )
                }
            ),
            errors=errors,
        )

    async def _async_connect_discovered(
        self, selected: ControllerConfig
    ) -> str | None:
        """Validate connectivity and store the single controller. Return error key."""
        host = selected[CONF_HOST]
        port = int(selected.get(CONF_PORT, DEFAULT_PORT))
        mac = selected[CONF_MAC]
        label = str(selected.get(CONF_LABEL) or mac).strip()
        if self._mac_in_other_entries(mac):
            return "duplicate_mac"
        if not await _test_connection(host, port, mac, label):
            return "cannot_connect"
        existing = _controllers_from_all_entries(self.hass)
        name = unique_controller_name(host, mac, existing)
        self._controller = build_controller_dict(host, port, mac, label, name)
        self._unicast = self._existing_unicast()
        return None

    async def async_step_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a controller discovered while the integration is running."""
        info = _discovered_to_dict(discovery_info)
        mac = info[CONF_MAC]
        await self.async_set_unique_id(normalize_mac_id(mac))
        self._abort_if_unique_id_configured()

        if self._mac_in_other_entries(mac):
            return self.async_abort(reason="already_configured")

        self._discovery_info = info
        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm creating a new entry for a runtime-discovered controller."""
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

            existing = _controllers_from_all_entries(self.hass)
            name = unique_controller_name(host, mac, existing)
            self._controller = build_controller_dict(host, port, mac, label, name)
            self._unicast = self._existing_unicast()
            return await self.async_step_finish()

        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "label": label,
                "host": host,
                "mac": mac,
            },
        )

    async def async_step_import(
        self, import_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Import a single controller entry (migration from multi-controller)."""
        controllers = import_data.get(CONF_CONTROLLERS) or []
        if not isinstance(controllers, list) or not controllers:
            return self.async_abort(reason="no_controllers")

        ctrl = controllers[0]
        if not isinstance(ctrl, dict):
            return self.async_abort(reason="no_controllers")

        mac = str(ctrl.get(CONF_MAC, ""))
        mac_id = normalize_mac_id(mac)
        if not mac_id:
            return self.async_abort(reason="no_controllers")

        await self.async_set_unique_id(mac_id)
        self._abort_if_unique_id_configured()

        title = str(import_data.get("title") or entry_title(ctrl))
        result = self.async_create_entry(
            title=title,
            data={
                CONF_CONTROLLERS: [ctrl],
                CONF_UNICAST: bool(import_data.get(CONF_UNICAST, False)),
            },
        )

        old_entry_id = import_data.get("migrate_from_entry_id")
        new_entry = result.get("result")
        if old_entry_id and isinstance(new_entry, ConfigEntry):
            _async_relink_migrated_devices(
                self.hass,
                old_entry_id=str(old_entry_id),
                new_entry_id=new_entry.entry_id,
                mac=mac,
            )
        return result

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prime discovery while showing progress, then create the entry."""
        if self._controller is None:
            return self.async_abort(reason="no_controllers")

        if self._finish_task is None:
            self._finish_task = self.hass.async_create_task(
                _async_prime_discovery(
                    self.hass,
                    self._controller,
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
            return self.async_show_progress_done(next_step_id="prime_failed")
        self._finish_task = None
        return self.async_show_progress_done(next_step_id="create_entry")

    async def async_step_prime_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Abort after discovery priming failed."""
        return self.async_abort(reason="cannot_connect")

    async def async_step_create_entry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the config entry after setup-devices progress completes."""
        if self._controller is None:
            return self.async_abort(reason="no_controllers")

        mac_id = normalize_mac_id(self._controller[CONF_MAC])
        await self.async_set_unique_id(mac_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=entry_title(self._controller),
            data={
                CONF_CONTROLLERS: [self._controller],
                CONF_UNICAST: self._unicast,
            },
        )

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Continue into options to suggest sub-devices for the new controller."""
        if self.source == SOURCE_IMPORT or self._controller is None:
            return result

        entry = result["result"]
        options_result = await self.hass.config_entries.options.async_init(
            entry.entry_id,
            context={"source": SOURCE_USER},
            data={CTX_SUGGEST_SUB_DEVICES: self._controller[CONF_NAME]},
        )
        result["next_flow"] = (FlowType.OPTIONS_FLOW, options_result["flow_id"])
        return result

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reconfigure this entry's controller or domain-wide unicast flag."""
        entry = self._get_reconfigure_entry()
        controllers = list(entry.data.get(CONF_CONTROLLERS, []))
        if not controllers:
            return self.async_abort(reason="no_controllers")

        choices = {
            "controller": "Controller connection",
            "unicast": "Unicast settings (domain-wide)",
        }

        if user_input is not None:
            match user_input["target"]:
                case "unicast":
                    return await self.async_step_unicast_settings()
                case _:
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
        """Update unicast on this entry (shared runtime uses first-created mode).

        Changing this flag updates entry.data but does not hot-swap a running
        shared runtime; it only takes effect when the runtime is recreated.
        """
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
        """Update the single controller on this entry."""
        entry = self._get_reconfigure_entry()
        controllers = list(entry.data.get(CONF_CONTROLLERS, []))
        if not controllers:
            return self.async_abort(reason="no_controllers")
        current = controllers[0]
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
                    if self._mac_in_other_entries(
                        mac, ignore_entry_id=entry.entry_id
                    ):
                        errors[CONF_MAC] = "duplicate_mac"
                    else:
                        # Keep CONF_NAME stable so entity unique_ids survive IP edits.
                        name = current.get(CONF_NAME) or unique_controller_name(
                            host, mac, _controllers_from_all_entries(self.hass)
                        )
                        updated = build_controller_dict(
                            host,
                            port,
                            mac,
                            label,
                            name,
                            sub_devices=current.get(CONF_SUB_DEVICES),
                        )
                        new_unique = normalize_mac_id(mac)
                        await self.async_set_unique_id(new_unique)
                        if entry.unique_id != new_unique:
                            self._abort_if_unique_id_configured()

                        return self.async_update_reload_and_abort(
                            entry,
                            unique_id=new_unique,
                            title=entry_title(updated),
                            data={
                                CONF_CONTROLLERS: [updated],
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
    ) -> ConfigFlowResult | None:
        """Validate and store the controller, or re-show for MAC confirm.

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
                    ),
                    errors={},
                )
            errors[CONF_MAC] = "mac_not_found"
            return None

        result = await self._async_validate_fields(user_input, errors)
        if result is None:
            return None

        host, port, mac, label = result
        if self._mac_in_other_entries(mac):
            errors[CONF_MAC] = "duplicate_mac"
            return None

        existing = _controllers_from_all_entries(self.hass)
        name = unique_controller_name(host, mac, existing)
        self._controller = build_controller_dict(host, port, mac, label, name)
        if include_unicast:
            self._unicast = bool(user_input.get(CONF_UNICAST, False))
        else:
            self._unicast = self._existing_unicast()

        return await self.async_step_finish()

    async def _async_validate_fields(
        self,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> tuple[str, int, str, str] | None:
        """Validate host/port/mac/label and connectivity."""
        host = str(user_input.get(CONF_HOST, "")).strip()
        port = user_input.get(CONF_PORT, DEFAULT_PORT)
        mac = normalize_mac(str(user_input.get(CONF_MAC, "")))
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
    """Options flow: sub-devices for this entry's single controller."""

    _ctrl_name: str | None = None
    _sub_device_id: str | None = None
    # After saving a sub-device, return to this step instead of closing.
    _return_after_save: SaveReturnStep = None
    _suggest_from_setup_handled: bool = False

    def __getattr__(self, name: str) -> Any:
        """Route dynamic menu steps for sub-devices."""
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

    def _controllers(self) -> list[ControllerConfig]:
        """Return this entry's controllers (always length 0 or 1)."""
        return list(self.config_entry.data.get(CONF_CONTROLLERS, []))

    def _controller(self, name: str | None = None) -> ControllerConfig | None:
        """Return the only controller, or the one matching ``name``."""
        controllers = self._controllers()
        if not controllers:
            return None
        if name is None:
            return controllers[0]
        return next((c for c in controllers if c[CONF_NAME] == name), None)

    def _set_suggest_queue(self, names: list[str]) -> None:
        """Queue controllers that should each get the sub-device prompt."""
        self._suggest_queue = [n for n in names if n]

    def _pop_suggest_controller(self) -> str | None:
        """Return the next controller name awaiting a sub-device prompt."""
        queue: list[str] = getattr(self, "_suggest_queue", None) or []
        self._suggest_queue = queue
        while queue:
            name = queue.pop(0)
            if self._controller(name) is not None:
                return name
        return None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Open suggest after setup, else sub-device menu for this controller."""
        init_data = self.init_data if isinstance(self.init_data, dict) else {}
        if not self._suggest_from_setup_handled:
            match init_data.get(CTX_SUGGEST_SUB_DEVICES):
                case None:
                    pass
                case str() as name:
                    self._suggest_from_setup_handled = True
                    self._set_suggest_queue([name])
                    return await self.async_step_suggest_sub_devices()
                case names:
                    self._suggest_from_setup_handled = True
                    self._set_suggest_queue([str(n) for n in names])
                    return await self.async_step_suggest_sub_devices()

        ctrl = self._controller()
        if ctrl is None:
            return self.async_abort(reason="no_controllers")
        self._ctrl_name = ctrl[CONF_NAME]
        return await self.async_step_controller()

    async def async_step_suggest_sub_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """After adding a controller, offer to create sub-devices."""
        if self._ctrl_name is None or self._controller(self._ctrl_name) is None:
            self._ctrl_name = self._pop_suggest_controller()
        ctrl = self._controller(self._ctrl_name)
        if ctrl is None:
            return self.async_create_entry(title="", data={})

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
        self._ctrl_name = None
        if getattr(self, "_suggest_queue", None):
            return await self.async_step_suggest_sub_devices()
        return self.async_create_entry(title="", data={})

    async def async_step_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List this controller's sub-devices plus Add sub-device."""
        ctrl = self._controller(self._ctrl_name) or self._controller()
        if ctrl is None:
            return self.async_abort(reason="no_controllers")
        self._ctrl_name = ctrl[CONF_NAME]

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
        controllers: list[ControllerConfig],
    ) -> ConfigFlowResult:
        """Persist sub-device config and reassign entities without rediscovery."""
        ctrl = controllers[0] if controllers else None
        title = entry_title(ctrl) if ctrl else self.config_entry.title
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

    async def _async_persist_controller(
        self,
        controllers: list[ControllerConfig],
        *,
        title: str,
    ) -> None:
        """Write the single controller into the config entry (no reload)."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=title,
            data={
                CONF_CONTROLLERS: controllers[:1],
                CONF_UNICAST: self.config_entry.data.get(CONF_UNICAST, False),
            },
        )
