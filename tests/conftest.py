"""Shared pytest fixtures for Home Assistant and simulator integration tests."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Markers / sibling checkout discovery
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIBLING_SIMULATOR_ROOT = _REPO_ROOT.parent / "zencontrol-simulator"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "simulator: integration tests that start zencontrol-simulator "
        "(pip install -e ../zencontrol-simulator, or a sibling checkout)",
    )


def _ensure_simulator_importable() -> None:
    """Make zencontrol_simulator importable via install or sibling checkout."""
    try:
        import zencontrol_simulator  # noqa: F401

        return
    except ImportError:
        pass

    pkg_dir = _SIBLING_SIMULATOR_ROOT / "zencontrol_simulator"
    if not pkg_dir.is_dir():
        return

    sibling = str(_SIBLING_SIMULATOR_ROOT.resolve())
    if sibling not in sys.path:
        sys.path.insert(0, sibling)


def _require_simulator():
    """Import zencontrol_simulator or skip with a clear reason."""
    _ensure_simulator_importable()
    try:
        import zencontrol_simulator
    except ImportError:
        pytest.skip(
            "zencontrol-simulator not available - pip install -e "
            "../zencontrol-simulator or check it out as a sibling directory"
        )

    pytest.importorskip("yaml", reason="PyYAML required for zencontrol-simulator")
    return zencontrol_simulator


def _simulator_config_path() -> Path:
    """Resolve the demo world YAML shipped with zencontrol-simulator."""
    sim = _require_simulator()
    sim_file = sim.__file__
    assert sim_file is not None
    packaged = Path(sim_file).resolve().parent / "config.yaml"
    if packaged.is_file():
        return packaged
    sibling = _SIBLING_SIMULATOR_ROOT / "config.yaml"
    if sibling.is_file():
        return sibling
    pytest.skip("zencontrol-simulator config.yaml not found")


# ---------------------------------------------------------------------------
# Config-flow stubs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Simulator fixtures
# ---------------------------------------------------------------------------


@dataclass
class LiveSimulator:
    """Running simulator with bind port and MAC for config-entry data."""

    world: Any
    sim: Any

    @property
    def port(self) -> int:
        return int(self.sim.bind_port)

    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.world.mac)

    @property
    def label(self) -> str:
        return str(getattr(self.world, "label", None) or "Simulator")


@pytest.fixture
async def live_sim() -> AsyncGenerator[LiveSimulator]:
    """Start zencontrol-simulator on an ephemeral localhost port."""
    _require_simulator()
    from zencontrol_simulator.server import Simulator
    from zencontrol_simulator.world import load_world

    config = _simulator_config_path()
    world = load_world(config)
    world.bind_host = "127.0.0.1"
    world.bind_port = 0
    world.heartbeat_interval = 0  # avoid background occupancy noise

    sim = Simulator(world)
    await sim.start()
    live = LiveSimulator(world=world, sim=sim)
    try:
        yield live
    finally:
        await sim.stop()
