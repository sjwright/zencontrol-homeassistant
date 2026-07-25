"""System variable label classification."""

from __future__ import annotations

from zencontrol import ZenSystemVariable


def classify_sysvar(label: str | None) -> tuple[bool, bool]:
    """Return (as_sensor, as_switch) from a Zen system variable label."""
    lower = (label or "").casefold()
    return "sensor" in lower, "switch" in lower


def classify_sysvar_entity(zen_sv: ZenSystemVariable) -> tuple[bool, bool]:
    """Classify a ZenSystemVariable for HA exposure."""
    return classify_sysvar(zen_sv.label)
