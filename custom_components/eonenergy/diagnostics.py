"""
Diagnostics for the E-ON Energy integration.

Exports diagnostic information for support tickets:
- Active contracts and sensors
- Coordinator state

Sensitive data (password, tokens) is excluded.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostic data for E-ON Energy."""

    # ── Contracts and coordinators ──
    runtime = getattr(entry, "runtime_data", None)
    coordinators_info: dict[str, Any] = {}
    if runtime and hasattr(runtime, "coordinators"):
        for cod, coordinator in runtime.coordinators.items():
            coordinators_info[cod] = {
                "is_collective": getattr(coordinator, "is_collective", False),
                "last_update_success": coordinator.last_update_success,
            }

    # ── Active sensors ──
    active_sensors = sorted(
        entity.entity_id
        for entity in hass.states.async_all("sensor")
        if entity.entity_id.startswith(f"sensor.{DOMAIN}_")
    )

    # ── Config entry (without sensitive data) ──
    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "domain": DOMAIN,
            "username": _mask_email(entry.data.get("username", "")),
            "update_interval": entry.data.get("update_interval"),
            "selected_contracts": entry.data.get("selected_contracts", []),
        },
        "contracts": coordinators_info,
        "state": {
            "active_sensors": len(active_sensors),
            "sensor_list": active_sensors,
        },
    }


def _mask_email(email: str) -> str:
    """Mask the email keeping the first letter and domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"
