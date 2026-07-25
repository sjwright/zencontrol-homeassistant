"""Shared pytest fixtures for Home Assistant config-flow tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Prevent real hub/runtime setup when a config entry is created."""
    with patch(
        "custom_components.zencontrol_tpi.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def mock_test_connection() -> Generator[AsyncMock]:
    """Stub the TPI reachability probe used by the config flow."""
    with patch(
        "custom_components.zencontrol_tpi.config_flow._test_connection",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def mock_prime_discovery() -> Generator[AsyncMock]:
    """Stub pre-entry bus discovery / pending-manifest priming."""
    with patch(
        "custom_components.zencontrol_tpi.config_flow._async_prime_discovery",
        new_callable=AsyncMock,
    ) as mock:
        yield mock
