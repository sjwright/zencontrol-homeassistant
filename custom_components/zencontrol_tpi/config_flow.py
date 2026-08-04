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
from typing import Any, cast

import getmac
import voluptuous as vol
import zencontrol
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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from zencontrol import DiscoveredController

from .const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_SUB_DEVICES,
    CONF_TCP,
    CONF_UNICAST,
    CONFIG_VERSION,
    DATA_PENDING_MANIFEST,
    DEFAULT_PORT,
    DOMAIN,
    ControllerConfig,
    DiscoveredControllerInfo,
    SubDeviceConfig,
    controller_tcp,
    controller_unicast,
    controllers_from_entry_data,
    entry_data_for_controller,
    migrate_entry_data_to_v3,
    normalize_mac,
    normalize_mac_id,
)
from .discovery import (
    ControllerNotReadyError,
    discover_controller_entities,
    wait_until_controller_ready,
)
from .entry_helpers import mac_is_configured
from .manifest_store import build_manifest
from .options_flow import (
    CTX_SUGGEST_SUB_DEVICES,
    ZencontrolTpiOptionsFlow,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-]){5}([0-9A-Fa-f]{2})$")
CONF_DISCOVERED = "discovered"
DISCOVERY_LISTEN_SECONDS = 5.0


def _derive_name(host: str) -> str:
    """Derive an alphanumeric controller name from the host IP."""
    return re.sub(r"[^A-Za-z0-9]", "", host)[:16] or "zen"


def _controllers_from_all_entries(hass: HomeAssistant) -> list[ControllerConfig]:
    """Return every controller config across all domain entries."""
    controller_configs: list[ControllerConfig] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        controller_configs.extend(controllers_from_entry_data(entry.data))
    return controller_configs


def unique_controller_name(host: str, mac: str, existing: list[ControllerConfig]) -> str:
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


def entry_title(ctrl_cfg: ControllerConfig) -> str:
    """Human-readable config entry title (label, else name)."""
    return str(ctrl_cfg.get(CONF_LABEL) or ctrl_cfg.get(CONF_NAME) or "zencontrol")


def build_controller_dict(
    host: str,
    port: int,
    mac: str,
    label: str,
    name: str,
    *,
    unicast: bool = False,
    tcp: bool = False,
    sub_devices: list[SubDeviceConfig] | None = None,
) -> ControllerConfig:
    """Build a persisted controller config dict."""
    data: ControllerConfig = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_MAC: mac,
        CONF_NAME: name,
        CONF_LABEL: label,
        CONF_UNICAST: unicast,
        CONF_TCP: tcp,
    }
    if sub_devices:
        data[CONF_SUB_DEVICES] = sub_devices
    return data


def _controller_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build a controller connection schema including per-controller options."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
            vol.Required(
                CONF_PORT,
                default=defaults.get(CONF_PORT, DEFAULT_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(CONF_MAC, default=defaults.get(CONF_MAC, "")): str,
            vol.Required(CONF_LABEL, default=defaults.get(CONF_LABEL, vol.UNDEFINED)): str,
            vol.Optional(
                CONF_UNICAST,
                default=bool(defaults.get(CONF_UNICAST, False)),
            ): bool,
            vol.Optional(
                CONF_TCP,
                default=bool(defaults.get(CONF_TCP, False)),
            ): bool,
        }
    )


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
        mac = await hass.async_add_executor_job(partial(getmac.get_mac_address, **params))
    except Exception:
        _LOGGER.debug("MAC lookup failed for %s", host, exc_info=True)
        return None

    if not mac or not normalize_mac_id(mac).strip("0"):
        return None
    return normalize_mac(mac)


async def _test_connection(host: str, port: int, mac: str, label: str) -> bool:
    """Return True if the controller responds within 5 seconds."""
    test_name = f"cftest{int(time.monotonic_ns()) % 10**9}"
    zen = zencontrol.ZenControl()
    try:
        ctrl = zen.add_controller(id=99, name=test_name, label=label, host=host, port=port, mac=mac)
        result = await asyncio.wait_for(
            zen.commands.query_controller_startup_complete(ctrl), timeout=5.0
        )
        return result is True
    except Exception:
        _LOGGER.debug("Connection test failed for %s:%s", host, port, exc_info=True)
        return False
    finally:
        try:
            await zen.aclose()
        except Exception:
            _LOGGER.debug("Failed to close connection-test ZenControl", exc_info=True)


def _discovered_to_dict(
    discovered: DiscoveredController | dict[str, Any],
) -> DiscoveredControllerInfo:
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
                CONF_PORT: int(discovered.port or DEFAULT_PORT),
                CONF_MAC: normalize_mac(str(discovered.mac)),
                CONF_LABEL: str(discovered.label or discovered.mac).strip(),
            }


def _discovered_option_label(discovered: DiscoveredControllerInfo) -> str:
    """Human-readable label for a discovered controller selector option."""
    label = discovered.get(CONF_LABEL) or discovered[CONF_MAC]
    return f"{label} ({discovered[CONF_HOST]})"


def _selected_mac(user_input: dict[str, Any]) -> str | None:
    """Normalize a single-select discovered MAC value."""
    match user_input.get(CONF_DISCOVERED):
        case str() as raw if raw:
            return normalize_mac(raw)
        case _:
            return None


async def _async_listen_for_controllers(
    hass: HomeAssistant,
    duration: float = DISCOVERY_LISTEN_SECONDS,
) -> list[DiscoveredControllerInfo]:
    """Listen for multicast and return identified controllers.

    duration is how long to keep listening, not a deadline for the call.

    When the shared runtime already has a listener, reuse it so we do not bind
    a second multicast socket (SO_REUSEPORT can drop events on Linux).
    """
    from .runtime import async_get_runtime

    runtime = async_get_runtime(hass)
    if runtime is not None and runtime.listener_up:
        # Shared ZenControl - discover() returns identities heard in this window
        # (last_seen), so "try again" / "add another" still surfaces controllers
        # already cached from an earlier listen.
        try:
            discovered_controllers = await runtime.zen.discover(timeout=duration)
        except Exception:
            _LOGGER.debug("Multicast discovery listen failed", exc_info=True)
            return []
        return [_discovered_to_dict(controller) for controller in discovered_controllers]

    zen = zencontrol.ZenControl()
    try:
        # discover() already probes QUERY_CONTROLLER_LABEL per identity.
        discovered_controllers = await zen.discover(timeout=duration)
        return [_discovered_to_dict(controller) for controller in discovered_controllers]
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
    ctrl_cfg: ControllerConfig,
) -> None:
    """Wait for the controller, discover entities, and stash a pending manifest.

    Does not proceed until query_controller_startup_complete() is True (up to
    CONTROLLER_READY_WAIT_MAX - controllers can take 1-10 minutes after reboot).
    """
    zen = zencontrol.ZenControl()
    try:
        ctrl = zen.add_controller(
            id=1,
            name=ctrl_cfg[CONF_NAME],
            label=ctrl_cfg[CONF_LABEL],
            host=ctrl_cfg[CONF_HOST],
            port=int(ctrl_cfg.get(CONF_PORT, DEFAULT_PORT)),
            mac=ctrl_cfg.get(CONF_MAC),
            tcp=controller_tcp(ctrl_cfg),
            unicast=controller_unicast(ctrl_cfg),
        )
        try:
            await wait_until_controller_ready(zen, ctrl)
        except ControllerNotReadyError as err:
            raise RuntimeError(str(err)) from err

        discovery_snapshot = await discover_controller_entities(zen, ctrl)
        manifest = build_manifest(discovery_snapshot)
        mac_id = normalize_mac_id(ctrl_cfg[CONF_MAC])
        hass.data.setdefault(DOMAIN, {}).setdefault(DATA_PENDING_MANIFEST, {})[mac_id] = {"manifest": manifest}
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
    for device_entry in list(dr.async_entries_for_config_entry(device_registry, old_entry_id)):
        domain_identifiers = [ident for ident in device_entry.identifiers if ident[0] == DOMAIN]
        if not domain_identifiers:
            continue
        if not any(ident == mac_norm or ident == mac_id or ident.startswith(sub_prefix) for _, ident in domain_identifiers):
            continue
        device_registry.async_update_device(
            device_entry.id,
            add_config_entry_id=new_entry_id,
            remove_config_entry_id=old_entry_id,
        )


class ZencontrolTpiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for zencontrol-tpi (one entry per controller)."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        """Initialize flow state for single-controller setup."""
        self._controller: ControllerConfig | None = None
        self._discovered: list[DiscoveredControllerInfo] = []
        self._discovery_info: DiscoveredControllerInfo | None = None
        self._discovery_task: asyncio.Task[list[DiscoveredControllerInfo]] | None = None
        self._connect_task: asyncio.Task[str | None] | None = None
        self._connect_error: str | None = None
        self._finish_task: asyncio.Task[None] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for managing sub-devices."""
        return ZencontrolTpiOptionsFlow()

    async def _async_run_discovery(self) -> list[DiscoveredControllerInfo]:
        """Listen for multicast and filter already-configured controllers."""
        discovered_controllers = await _async_listen_for_controllers(self.hass)
        return [
            controller
            for controller in discovered_controllers
            if not mac_is_configured(self.hass, controller[CONF_MAC])
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
        defaults: dict[str, Any] = dict(user_input) if user_input else {
            CONF_UNICAST: False,
            CONF_TCP: False,
        }

        if user_input is not None:
            handled = await self._async_handle_controller_form(
                user_input,
                errors,
                defaults,
                step_id="manual",
            )
            if handled is not None:
                return handled

        return self.async_show_form(
            step_id="manual",
            data_schema=_controller_schema(defaults),
            errors=errors,
        )

    async def async_step_discover(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Listen for multicast controllers with a progress UI."""
        if self._discovery_task is None:
            self._discovery_task = self.hass.async_create_task(self._async_run_discovery())

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
        """No controllers found - try again or enter manually."""
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

        options = [SelectOptionDict(value=item[CONF_MAC], label=_discovered_option_label(item)) for item in self._discovered]
        default_mac = self._discovered[0][CONF_MAC] if self._discovered else None

        # Only treat this as a form submit when the selector field is present.
        # After SHOW_PROGRESS_DONE, HA's configure loop may re-enter this step
        # with leftover menu navigation input (e.g. {"next_step_id": "discover"}).
        if user_input is not None and CONF_DISCOVERED in user_input:
            selected_mac = _selected_mac(user_input)
            selected = next(
                (
                    item
                    for item in self._discovered
                    if selected_mac and normalize_mac_id(item[CONF_MAC]) == normalize_mac_id(selected_mac)
                ),
                None,
            )
            if selected is None:
                return await self.async_step_discovery_failed()
            self._connect_task = self.hass.async_create_task(self._async_connect_discovered(selected))
            # Re-enter so an eagerly completed connect advances immediately,
            # matching the discover/finish progress pattern.
            return await self.async_step_select_discovered()

        schema_field: Any
        if default_mac is not None:
            schema_field = vol.Required(CONF_DISCOVERED, default=default_mac)
        else:
            schema_field = vol.Required(CONF_DISCOVERED)

        return self.async_show_form(
            step_id="select_discovered",
            data_schema=vol.Schema({schema_field: SelectSelector(SelectSelectorConfig(options=options, multiple=False))}),
            errors=errors,
        )

    async def _async_connect_discovered(self, selected: DiscoveredControllerInfo) -> str | None:
        """Validate connectivity and store the single controller. Return error key."""
        host = selected[CONF_HOST]
        port = int(selected.get(CONF_PORT, DEFAULT_PORT))
        mac = selected[CONF_MAC]
        label = str(selected.get(CONF_LABEL) or mac).strip()
        if mac_is_configured(self.hass, mac):
            return "duplicate_mac"
        if not await _test_connection(host, port, mac, label):
            return "cannot_connect"
        existing = _controllers_from_all_entries(self.hass)
        name = unique_controller_name(host, mac, existing)
        self._controller = build_controller_dict(host, port, mac, label, name)
        return None

    async def async_step_discovery(self, discovery_info: dict[str, Any]) -> ConfigFlowResult:
        """Handle a controller discovered while the integration is running."""
        info = _discovered_to_dict(discovery_info)
        mac = info[CONF_MAC]
        await self.async_set_unique_id(normalize_mac_id(mac))
        self._abort_if_unique_id_configured()

        if mac_is_configured(self.hass, mac):
            return self.async_abort(reason="already_configured")

        self._discovery_info = info
        # Discovery banner/notice title uses title_placeholders.name (not the
        # confirm-step description placeholders).
        self.context["title_placeholders"] = {
            "name": str(info.get(CONF_LABEL) or mac).strip(),
        }
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
        self.context["title_placeholders"] = {"name": label}

        if user_input is not None:
            if mac_is_configured(self.hass, mac):
                return self.async_abort(reason="already_configured")
            if not await _test_connection(host, port, mac, label):
                return self.async_abort(reason="cannot_connect")

            existing = _controllers_from_all_entries(self.hass)
            name = unique_controller_name(host, mac, existing)
            self._controller = build_controller_dict(host, port, mac, label, name)
            return await self.async_step_finish()

        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "label": label,
                "host": host,
                "mac": mac,
            },
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import a single controller entry (migration from multi-controller)."""
        controllers = import_data.get(CONF_CONTROLLERS) or []
        if not isinstance(controllers, list) or not controllers:
            return self.async_abort(reason="no_controllers")

        ctrl_cfg_raw = controllers[0]
        if not isinstance(ctrl_cfg_raw, dict):
            return self.async_abort(reason="no_controllers")

        # Fold legacy entry-level unicast onto the controller when present.
        normalized = migrate_entry_data_to_v3(import_data)
        ctrl_cfg = cast(ControllerConfig, normalized[CONF_CONTROLLERS][0])
        mac = str(ctrl_cfg.get(CONF_MAC, ""))
        mac_id = normalize_mac_id(mac)
        if not mac_id:
            return self.async_abort(reason="no_controllers")

        await self.async_set_unique_id(mac_id)
        self._abort_if_unique_id_configured()

        title = str(import_data.get("title") or entry_title(ctrl_cfg))
        flow_result = self.async_create_entry(
            title=title,
            data=entry_data_for_controller(ctrl_cfg),
        )

        old_entry_id = import_data.get("migrate_from_entry_id")
        created_entry = flow_result.get("result")
        if old_entry_id and isinstance(created_entry, ConfigEntry):
            _async_relink_migrated_devices(
                self.hass,
                old_entry_id=str(old_entry_id),
                new_entry_id=created_entry.entry_id,
                mac=mac,
            )
        return flow_result

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Prime discovery while showing progress, then create the entry."""
        if self._controller is None:
            return self.async_abort(reason="no_controllers")

        if self._finish_task is None:
            self._finish_task = self.hass.async_create_task(
                _async_prime_discovery(self.hass, self._controller)
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
            data=entry_data_for_controller(self._controller),
        )

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Continue into options to suggest sub-devices for the new controller."""
        if self.source == SOURCE_IMPORT or self._controller is None:
            return result

        created_entry = result.get("result")
        if not isinstance(created_entry, ConfigEntry):
            return result
        options_flow_result = await self.hass.config_entries.options.async_init(
            created_entry.entry_id,
            context={"source": SOURCE_USER},
            data={CTX_SUGGEST_SUB_DEVICES: self._controller[CONF_NAME]},
        )
        result["next_flow"] = (FlowType.OPTIONS_FLOW, options_flow_result["flow_id"])
        return result

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reconfigure this entry's controller connection."""
        entry = self._get_reconfigure_entry()
        controllers = controllers_from_entry_data(entry.data)
        if not controllers:
            return self.async_abort(reason="no_controllers")
        return await self.async_step_reconfigure_controller()

    async def async_step_reconfigure_controller(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Update the single controller on this entry."""
        entry = self._get_reconfigure_entry()
        controllers = controllers_from_entry_data(entry.data)
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
            CONF_UNICAST: controller_unicast(current),
            CONF_TCP: controller_tcp(current),
        }
        if user_input:
            defaults = {**defaults, **user_input}

        if user_input is not None:
            mac = (user_input.get(CONF_MAC) or "").strip()
            if not mac:
                discovered = await _async_discover_mac(self.hass, user_input[CONF_HOST])
                if discovered:
                    defaults = {**defaults, CONF_MAC: discovered}
                    return self.async_show_form(
                        step_id="reconfigure_controller",
                        data_schema=_controller_schema(defaults),
                        errors={},
                    )
                errors[CONF_MAC] = "mac_not_found"
            else:
                validated_fields = await self._async_validate_fields(user_input, errors)
                if validated_fields is not None:
                    host, port, mac, label = validated_fields
                    if mac_is_configured(self.hass, mac, ignore_entry_id=entry.entry_id):
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
                            unicast=bool(user_input.get(CONF_UNICAST, False)),
                            tcp=bool(user_input.get(CONF_TCP, False)),
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
                            data=entry_data_for_controller(updated),
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
    ) -> ConfigFlowResult | None:
        """Validate and store the controller, or re-show for MAC confirm.

        Returns a ConfigFlowResult when navigation should continue, else None
        to show the form with errors.
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
                    data_schema=_controller_schema(defaults),
                    errors={},
                )
            errors[CONF_MAC] = "mac_not_found"
            return None

        validated_fields = await self._async_validate_fields(user_input, errors)
        if validated_fields is None:
            return None

        host, port, mac, label = validated_fields
        if mac_is_configured(self.hass, mac):
            errors[CONF_MAC] = "duplicate_mac"
            return None

        existing = _controllers_from_all_entries(self.hass)
        name = unique_controller_name(host, mac, existing)
        self._controller = build_controller_dict(
            host,
            port,
            mac,
            label,
            name,
            unicast=bool(user_input.get(CONF_UNICAST, False)),
            tcp=bool(user_input.get(CONF_TCP, False)),
        )

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
