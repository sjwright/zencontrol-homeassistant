"""Config and options flow tests for zencontrol-tpi."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zencontrol_tpi.config_flow import (
    CONF_DISCOVERED,
)
from custom_components.zencontrol_tpi.const import (
    CONF_CONTROLLERS,
    CONF_LABEL,
    CONF_MAC,
    CONF_NAME,
    CONF_SUB_DEVICES,
    CONF_UNICAST,
    DEFAULT_PORT,
    DOMAIN,
    normalize_mac_id,
)
from custom_components.zencontrol_tpi.options_flow import CONF_PREFIXES

pytestmark = pytest.mark.usefixtures(
    "enable_custom_integrations",
    "mock_setup_entry",
    "mock_test_connection",
    "mock_prime_discovery",
)


def controller_config(
    *,
    host: str = "10.0.0.10",
    port: int = DEFAULT_PORT,
    mac: str = "AA:BB:CC:DD:EE:01",
    label: str = "House",
    name: str = "100010",
    sub_devices: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a persisted controller dict for entry data / form input."""
    data: dict[str, Any] = {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_MAC: mac,
        CONF_LABEL: label,
        CONF_NAME: name,
    }
    if sub_devices is not None:
        data[CONF_SUB_DEVICES] = sub_devices
    return data


def entry_data(
    ctrl_cfg: dict[str, Any] | None = None,
    *,
    unicast: bool = False,
) -> dict[str, Any]:
    """Build config-entry data for one controller."""
    return {
        CONF_CONTROLLERS: [ctrl_cfg or controller_config()],
        CONF_UNICAST: unicast,
    }

MAC = "AA:BB:CC:DD:EE:01"
MAC_ID = normalize_mac_id(MAC)
HOST = "10.0.0.10"
LABEL = "House"


def _manual_input(*, include_unicast: bool = True, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_HOST: HOST,
        CONF_PORT: DEFAULT_PORT,
        CONF_MAC: MAC,
        CONF_LABEL: LABEL,
    }
    if include_unicast:
        data[CONF_UNICAST] = False
    data.update(overrides)
    return data


async def _start_manual(hass: HomeAssistant) -> dict[str, Any]:
    """Open the user menu and navigate to the manual form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"discover", "manual"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    return result


async def test_user_menu(hass: HomeAssistant) -> None:
    """User step offers discovery or manual setup."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"discover", "manual"}


async def test_manual_create_entry(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_test_connection: AsyncMock,
    mock_prime_discovery: AsyncMock,
) -> None:
    """Manual form creates a single-controller entry after priming."""
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input()
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LABEL
    assert result["result"].unique_id == MAC_ID
    assert result["data"][CONF_UNICAST] is False
    controllers = result["data"][CONF_CONTROLLERS]
    assert len(controllers) == 1
    assert controllers[0][CONF_HOST] == HOST
    assert controllers[0][CONF_MAC] == MAC
    assert controllers[0][CONF_LABEL] == LABEL
    assert controllers[0][CONF_NAME]
    mock_test_connection.assert_awaited()
    mock_prime_discovery.assert_awaited_once()
    mock_setup_entry.assert_awaited()
    assert "next_flow" in result


async def test_manual_cannot_connect(
    hass: HomeAssistant, mock_test_connection: AsyncMock
) -> None:
    """Unreachable controller keeps the user on the manual form."""
    mock_test_connection.return_value = False
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input()
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (CONF_MAC, "not-a-mac", "invalid_mac"),
        (CONF_LABEL, "", "invalid_label"),
        (CONF_HOST, "", "invalid_host"),
    ],
)
async def test_manual_field_validation(
    hass: HomeAssistant, field: str, value: str, error: str
) -> None:
    """Invalid host/MAC/label surface the matching form error."""
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(**{field: value})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["errors"] == {field: error}


async def test_manual_discovers_mac_then_creates(
    hass: HomeAssistant,
) -> None:
    """Blank MAC triggers ARP lookup, then a second submit creates the entry."""
    result = await _start_manual(hass)
    with patch(
        "custom_components.zencontrol_tpi.config_flow._async_discover_mac",
        new_callable=AsyncMock,
        return_value=MAC,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            _manual_input(**{CONF_MAC: ""}),
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC_ID


async def test_manual_mac_not_found(hass: HomeAssistant) -> None:
    """Blank MAC with failed ARP lookup asks the user to enter it."""
    result = await _start_manual(hass)
    with patch(
        "custom_components.zencontrol_tpi.config_flow._async_discover_mac",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            _manual_input(**{CONF_MAC: ""}),
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAC: "mac_not_found"}


async def test_manual_duplicate_mac(hass: HomeAssistant) -> None:
    """A MAC already owned by another entry is rejected."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=entry_data(),
        title=LABEL,
    )
    existing.add_to_hass(hass)

    result = await _start_manual(hass)
    # Unicast is only on the first-entry form; later entries omit it.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input(include_unicast=False)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAC: "duplicate_mac"}


async def test_discover_select_creates_entry(hass: HomeAssistant) -> None:
    """Multicast discovery → select → connect → create entry."""
    discovered = {
        CONF_HOST: HOST,
        CONF_PORT: DEFAULT_PORT,
        CONF_MAC: MAC,
        CONF_LABEL: LABEL,
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.zencontrol_tpi.config_flow."
        "ZencontrolTpiConfigFlow._async_run_discovery",
        new_callable=AsyncMock,
        return_value=[discovered],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_discovered"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DISCOVERED: MAC}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC_ID
    assert result["data"][CONF_CONTROLLERS][0][CONF_MAC] == MAC


async def test_discover_none_found(hass: HomeAssistant) -> None:
    """Empty discovery returns the retry/manual menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.zencontrol_tpi.config_flow."
        "ZencontrolTpiConfigFlow._async_run_discovery",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "discover"}
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "discovery_failed"
    assert set(result["menu_options"]) == {"discover", "manual"}


async def test_runtime_discovery_confirm(hass: HomeAssistant) -> None:
    """Runtime SOURCE_DISCOVERY confirm creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "discovery"},
        data={
            CONF_HOST: HOST,
            CONF_PORT: DEFAULT_PORT,
            CONF_MAC: MAC,
            CONF_LABEL: LABEL,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_discovery"
    assert result["description_placeholders"]["mac"] == MAC
    assert result["description_placeholders"]["label"] == LABEL

    # Discovery banner/notice title comes from context title_placeholders.
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["title_placeholders"]["name"] == LABEL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC_ID


async def test_runtime_discovery_already_configured(hass: HomeAssistant) -> None:
    """Runtime discovery aborts when the MAC unique_id exists."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=entry_data(),
        title=LABEL,
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "discovery"},
        data={
            CONF_HOST: HOST,
            CONF_PORT: DEFAULT_PORT,
            CONF_MAC: MAC,
            CONF_LABEL: LABEL,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_import_creates_entry(hass: HomeAssistant) -> None:
    """Legacy multi-controller migration import creates a single entry."""
    ctrl_cfg = controller_config()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_CONTROLLERS: [ctrl_cfg],
            CONF_UNICAST: True,
            "title": "Imported",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Imported"
    assert result["data"][CONF_UNICAST] is True
    assert result["result"].unique_id == MAC_ID


async def test_import_already_configured(hass: HomeAssistant) -> None:
    """Import aborts when the controller MAC is already configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=entry_data(),
        title=LABEL,
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={CONF_CONTROLLERS: [controller_config()]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_finish_prime_failure_aborts(
    hass: HomeAssistant, mock_prime_discovery: AsyncMock
) -> None:
    """Failed discovery priming ends the flow with cannot_connect."""
    mock_prime_discovery.side_effect = RuntimeError("still starting")
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _manual_input()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_reconfigure_does_not_offer_unicast(hass: HomeAssistant) -> None:
    """Reconfigure opens controller settings without exposing unicast."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=entry_data(unicast=False),
        title=LABEL,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_controller"
    assert CONF_UNICAST not in result["data_schema"].schema


async def test_reconfigure_controller(hass: HomeAssistant) -> None:
    """Reconfigure updates host while keeping the controller name stable."""
    ctrl_cfg = controller_config(name="stable")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=entry_data(ctrl_cfg),
        title=LABEL,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_controller"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "10.0.0.99",
            CONF_PORT: DEFAULT_PORT,
            CONF_MAC: MAC,
            CONF_LABEL: "Renamed",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = entry.data[CONF_CONTROLLERS][0]
    assert updated[CONF_HOST] == "10.0.0.99"
    assert updated[CONF_LABEL] == "Renamed"
    assert updated[CONF_NAME] == "stable"


def _entry_with_runtime(hass: HomeAssistant, data: dict) -> ConfigEntry:
    """Add an entry and attach a hub stub for options-flow persistence."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC_ID,
        data=data,
        title=LABEL,
    )
    entry.add_to_hass(hass)
    hub = MagicMock()
    hub.sync_device_assignments = MagicMock()
    entry.runtime_data = hub
    return entry


async def test_options_add_sub_device(hass: HomeAssistant) -> None:
    """Options flow can add a label-prefix sub-device."""
    entry = _entry_with_runtime(hass, entry_data())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "controller"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_sub_device"}
    )
    assert result["step_id"] == "add_sub_device"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PREFIXES: "Kitchen, Living"},
    )
    # Saving returns to the controller menu.
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "controller"

    devices = entry.data[CONF_CONTROLLERS][0][CONF_SUB_DEVICES]
    assert len(devices) == 1
    assert devices[0]["name"] == "Kitchen"
    assert devices[0]["prefixes"] == ["Kitchen", "Living"]
    entry.runtime_data.sync_device_assignments.assert_called()


async def test_options_add_duplicate_prefix(hass: HomeAssistant) -> None:
    """Duplicate prefixes are rejected when adding a sub-device."""
    ctrl_cfg = controller_config(
        sub_devices=[
            {
                "id": "kitchen",
                "name": "Kitchen",
                "prefixes": ["Kitchen"],
            }
        ]
    )
    entry = _entry_with_runtime(hass, entry_data(ctrl_cfg))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_sub_device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PREFIXES: "Kitchen"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PREFIXES: "duplicate_prefix"}


async def test_options_delete_sub_device(hass: HomeAssistant) -> None:
    """Options flow can delete an existing sub-device via the dynamic menu."""
    ctrl_cfg = controller_config(
        sub_devices=[
            {
                "id": "kitchen",
                "name": "Kitchen",
                "prefixes": ["Kitchen"],
            }
        ]
    )
    entry = _entry_with_runtime(hass, entry_data(ctrl_cfg))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "subdev_kitchen" in result["menu_options"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "subdev_kitchen"}
    )
    assert result["step_id"] == "sub_device"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "delete_sub_device"}
    )
    assert result["step_id"] == "delete_sub_device"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "controller"
    assert CONF_SUB_DEVICES not in entry.data[CONF_CONTROLLERS][0]


async def test_options_suggest_from_setup(hass: HomeAssistant) -> None:
    """Post-setup options context opens the suggest-sub-devices menu."""
    ctrl_cfg = controller_config()
    entry = _entry_with_runtime(hass, entry_data(ctrl_cfg))

    result = await hass.config_entries.options.async_init(
        entry.entry_id,
        context={"source": SOURCE_USER},
        data={"suggest_sub_devices_ctrl": ctrl_cfg[CONF_NAME]},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "suggest_sub_devices"
    assert set(result["menu_options"]) == {"add_sub_device", "finish_setup"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish_setup"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
