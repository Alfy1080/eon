"""
Diagnostics for the E-ON Energy integration.

Exports diagnostic information for support tickets:
- License (fingerprint, status, masked key)
- Active contracts and sensors
- Starea coordinator-elor

Sensitive data (password, tokens) is excluded.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LICENSE_DATA_KEY


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostic data for E-ON Energy."""

    # ── License (fingerprint + masked key) ──
    license_mgr = hass.data.get(DOMAIN, {}).get(LICENSE_DATA_KEY)
    licenta_info: dict[str, Any] = {}
    if license_mgr:
        licenta_info = {
            "fingerprint": license_mgr.fingerprint,
            "status": license_mgr.status,
            "license_key": license_mgr.license_key_masked,
            "is_valid": license_mgr.is_valid,
            "license_type": license_mgr.license_type,
        }

    # ── Contracts and coordinators ──
    runtime = getattr(entry, "runtime_data", None)
    coordinators_info: dict[str, Any] = {}
    if runtime and hasattr(runtime, "coordinators"):
        for cod, coordinator in runtime.coordinators.items():
            coordinators_info[cod] = {
                "is_collective": getattr(coordinator, "is_collective", False),
                "last_update_success": coordinator.last_update_success,
            }

    # ── Senzori activi ──
    senzori_activi = sorted(
        entitate.entity_id
        for entitate in hass.states.async_all("sensor")
        if entitate.entity_id.startswith(f"sensor.{DOMAIN}_")
    )

    # ── Config entry (without sensitive data) ──
    return {
        "intrare": {
            "titlu": entry.title,
            "versiune": entry.version,
            "domeniu": DOMAIN,
            "username": _mascheaza_email(entry.data.get("username", "")),
            "update_interval": entry.data.get("update_interval"),
            "selected_contracts": entry.data.get("selected_contracts", []),
        },
        "licenta": licenta_info,
        "contracte": coordinators_info,
        "stare": {
            "senzori_activi": len(senzori_activi),
            "lista_senzori": senzori_activi,
        },
    }


def _mascheaza_email(email: str) -> str:
    """Mask the email keeping the first letter and domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"
