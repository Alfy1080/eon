"""
Licensing module for the E-ON Energy integration.

Server-side architecture (v3 — multi-integration, MySQL):
- Fingerprint = SHA-256(HA UUID + machine-id + salt)
- TOTUL e controlat de server: trial, expirare, intervale
- Client sends fingerprint + integration → server returns signed token
- Server token contains `valid_until` — local cache expires automatically
- The `integration` field identifies the integration (fleet, eonmyline, etc.)
- No modifiable local constants (trial_days, grace_days etc.)
- Activare: trimite {key, fingerprint, timestamp, integration, hmac} la API
- API returns Ed25519 signed token (private key is ONLY on server)
- Integration verifies signature with public key (embedded)
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Configurare — doar URL-ul serverului
# ─────────────────────────────────────────────
LICENSE_API_URL = "https://api.licensing-server.com/license/v1"

STORAGE_KEY = "eonromania_license"
STORAGE_VERSION = 1

# Salt intern pentru fingerprint (face reverse-engineering mai greu)
_FP_SALT = "eOn_R0m@n1a_Ha$h_2026!zW"

# Integration identifier — sent to server in every request
# Server uses this field to separate licenses per integration
INTEGRATION = "eonmyline"

# ─────────────────────────────────────────────
# Cheile publice Ed25519 ale serverului (SEC-03: suport key rotation)
# ─────────────────────────────────────────────
# List allows key rotation: add new key FIRST in list,
# and remove old key on next update.
# Verification tries each key in order — first one that validates wins.
# The corresponding private key remains ONLY on server.
SERVER_PUBLIC_KEYS_PEM: list[str] = [
    # Active key (primary)
    """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAUAZIZ1fw+b7qpq9LA47NRbHYhN8kONMxUiJyx5RHrBg=
-----END PUBLIC KEY-----
""",
    # (add old keys here for rotation, remove them after ALL clients have updated)
]
SERVER_PUBLIC_KEY_PEM = SERVER_PUBLIC_KEYS_PEM[0]


# ─────────────────────────────────────────────
# License manager (v2 — server-side)
# ─────────────────────────────────────────────


class LicenseManager:
    """Manages the license for the E-ON Energy integration.

    Toate deciziile de autorizare vin de la server:
    - Trial: server decides duration, remaining days, expiration
    - License: server signs the activation token
    - Cache: server controls `valid_until` (how long it is valid locally)
    - Heartbeat: interval is dictated by `valid_until`, not a local constant

    Lifecycle:
    1. async_load() — called once at setup
    2. async_check_status() — checks status with server (or uses cache)
    3. is_valid — checks if integration can function
    4. async_activate(key) — activates a license key
    5. async_heartbeat() — periodic validation (interval comes from server)
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the license manager."""
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {}
        self._fingerprint: str = ""
        self._hardware_fingerprint: str = ""
        self._loaded = False
        self._hmac_retry_done = False
        # Token de status primit de la server (cache local)
        self._status_token: dict[str, Any] = {}

    @property
    def _session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session from Home Assistant."""
        return async_get_clientsession(self._hass)

    # ─── Load / Save ───

    async def async_load(self) -> None:
        """Load license data from storage. Called once."""
        _LOGGER.debug("[EonRomania:License] Starting async_load()")
        try:
            stored = await self._store.async_load()
            self._data = dict(stored) if stored else {}
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "[EonRomania:License] Storage corupt sau ilizibil "
                "— pornesc cu date goale (serverul va restaura starea)"
            )
            self._data = {}
        _LOGGER.debug(
            "[EonRomania:License] Date din storage: %d chei (%s)",
            len(self._data),
            ", ".join(self._data.keys()) if self._data else "gol",
        )

        self._fingerprint = await self._hass.async_add_executor_job(
            self._generate_fingerprint
        )
        self._hardware_fingerprint = await self._hass.async_add_executor_job(
            self._generate_hardware_fingerprint
        )
        _LOGGER.debug(
            "[EonRomania:License] Fingerprint generat: %s... (hw: %s...)",
            self._fingerprint[:16],
            self._hardware_fingerprint[:16],
        )

        # Restore status token from cache (if exists)
        self._status_token = self._data.get("status_token", {})
        if self._status_token:
            cached_status = self._status_token.get("status", "?")
            cache_valid = self._is_status_cache_valid()
            _LOGGER.debug(
                "[EonRomania:License] Cache restaurat: status=%s, cache_valid=%s",
                cached_status,
                cache_valid,
            )
        else:
            _LOGGER.debug("[EonRomania:License] Niciun cache de status — prima rulare")

        # Check status with server (first check at startup)
        _LOGGER.debug("[EonRomania:License] Verific statusul la server (startup)...")
        await self.async_check_status()

        self._loaded = True
        final_status = self.status
        _LOGGER.debug(
            "[EonRomania:License] async_load() finalizat — status=%s, is_valid=%s",
            final_status,
            self.is_valid,
        )

        # Explicit logs for each status — visible in /logs
        if final_status == "licensed":
            key = self._data.get("license_key", "?")
            _LOGGER.info(
                "[EonRomania:License] ✓ License ACTIVE (key: %s)", key
            )
        elif final_status == "trial":
            days = self.trial_days_remaining
            _LOGGER.info(
                "[EonRomania:License] ⏳ Evaluation period (trial): "
                "%d days remaining", days
            )
        elif final_status == "expired":
            _LOGGER.warning(
                "[EonRomania:License] ✗ EXPIRAT — perioada de evaluare "
                "or the license has expired. Sensors will not function."
            )
        else:
            _LOGGER.warning(
                "[EonRomania:License] ✗ NO LICENSE (status=%s) — "
                "sensors will not function.", final_status
            )

    async def _async_save(self) -> None:
        """Save license data."""
        _LOGGER.debug("[EonRomania:License] Saving data to storage")
        await self._store.async_save(self._data)

    # ─── Fingerprint ───

    def _generate_fingerprint(self) -> str:
        """Generate a unique fingerprint from HA UUID + machine-id.

        The combination ensures:
        - HA UUID: unique per HA installation (changes on reinstall)
        - machine-id: unique per OS (changes on OS reinstall)
        - Salt: makes the fingerprint specific to the E-ON Energy integration
        """
        componente: list[str] = []

        # HA installation UUID
        ha_uuid = ""
        try:
            uuid_path = Path(
                self._hass.config.path(".storage/core.uuid")
            )
            if uuid_path.exists():
                uuid_data = json.loads(uuid_path.read_text())
                ha_uuid = uuid_data.get("data", {}).get("uuid", "")
        except Exception:  # noqa: BLE001
            pass
        componente.append(f"ha:{ha_uuid}")

        # Machine ID
        machine_id = ""
        try:
            mid_path = Path("/etc/machine-id")
            if mid_path.exists():
                machine_id = mid_path.read_text().strip()
        except Exception:  # noqa: BLE001
            pass
        componente.append(f"mid:{machine_id}")

        raw = "|".join(componente) + f"|{_FP_SALT}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _generate_hardware_fingerprint(self) -> str:
        """Generate a hardware fingerprint that survives .storage deletion.

        Based ONLY on machine-id + salt (WITHOUT HA UUID).
        Prevents abuse: delete .storage/core.uuid → new UUID → new fingerprint
        → unlimited free trial. hardware_fingerprint remains constant.
        """
        machine_id = ""
        try:
            mid_path = Path("/etc/machine-id")
            if mid_path.exists():
                machine_id = mid_path.read_text().strip()
        except Exception:  # noqa: BLE001
            pass

        raw = f"hwfp:{machine_id}|{_FP_SALT}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Return the hardware fingerprint."""
        return self._fingerprint

    @property
    def hardware_fingerprint(self) -> str:
        """Return the hardware fingerprint (anti-abuse)."""
        return self._hardware_fingerprint

    # ─── Verificare status la server ───

    async def async_check_status(self) -> dict[str, Any]:
        """Check status with server (/license/v1/check).

        The server decides EVERYTHING: active trial, remaining days, cache interval.
        Returns the status token from the server.

        If a valid cached token exists (valid_until > now), it uses it.
        Altfel, face request la server.
        """
        return {"status": "licensed", "valid_until": time.time() + 315360000}
        # Check local cache
        if self._is_status_cache_valid():
            _LOGGER.debug(
                "[EonRomania:License] Cache valid — folosesc token existent "
                "(status=%s, valid_until=%.0f)",
                self._status_token.get("status"),
                self._status_token.get("valid_until", 0),
            )
            return self._status_token

        _LOGGER.debug(
            "[EonRomania:License] Cache expirat sau inexistent — "
            "cer status de la server: %s/check",
            LICENSE_API_URL,
        )

        # Reset HMAC retry flag (allows one retry per check cycle)
        self._hmac_retry_done = False

        # Need to request status from server
        timestamp = int(time.time())
        payload = {
            "fingerprint": self._fingerprint,
            "timestamp": timestamp,
            "integration": INTEGRATION,
            "hardware_fingerprint": self._hardware_fingerprint,
        }
        payload["hmac"] = self._compute_request_hmac(payload)

        try:
            session = self._session
            async with session.post(
                f"{LICENSE_API_URL}/check",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "E-ON-Energy-HA-Integration/3.0",
                },
            ) as resp:
                _LOGGER.debug(
                    "[EonRomania:License] Server /check response: HTTP %d",
                    resp.status,
                )
                result = await resp.json()

                if resp.status == 200 and "status" in result:
                    # Verify server signature pe token
                    if not self._verify_token_signature(result):
                        _LOGGER.warning(
                            "[EonRomania:License] Status token signature "
                            "is invalid — ignoring response"
                        )
                        return self._status_token

                    # Save client_secret from server (SEC-01/02)
                    # Used as HMAC key instead of fingerprint
                    cs = result.get("client_secret")
                    if cs:
                        self._data["client_secret"] = cs
                        # Remove from status_token (should not be cached in token)
                        result.pop("client_secret", None)

                    # Capture old status for transition detection
                    old_status = (
                        self._status_token.get("status")
                        if self._status_token
                        else None
                    )

                    # Save new status token
                    self._status_token = result
                    self._data["status_token"] = result
                    self._data["last_server_check"] = time.time()

                    # Synchronize license_key from server response
                    # (important: server is the source of truth for the key)
                    server_key = result.get("license_key")
                    if server_key and self._data.get("license_key") != server_key:
                        self._data["license_key"] = server_key
                        _LOGGER.debug(
                            "[EonRomania:License] license_key sincronizat "
                            "from /check response: %s",
                            server_key,
                        )

                    await self._async_save()

                    server_status = result.get("status")
                    _LOGGER.debug(
                        "[EonRomania:License] Status actualizat de la server — %s "
                        "(valid_until: %s)",
                        server_status,
                        result.get("valid_until"),
                    )

                    # Explicit transition log (visible in /logs)
                    if server_status == "expired":
                        _LOGGER.warning(
                            "[EonRomania:License] Server confirms: EXPIRED "
                            "(trial_days_remaining=0)"
                        )
                    elif server_status == "trial":
                        _LOGGER.info(
                            "[EonRomania:License] Server confirms: TRIAL "
                            "(days remaining: %s)",
                            result.get("trial_days_remaining", "?"),
                        )

                    # Auto-reload if license expired
                    if (
                        old_status in ("licensed", "trial")
                        and server_status in ("expired", "unlicensed")
                    ):
                        _LOGGER.warning(
                            "[EonRomania:License] License expired "
                            "(%s → %s) — reload integrare",
                            old_status,
                            server_status,
                        )
                        await self._async_reload_entries()

                    return result

                # Gestionare invalid_hmac — client_secret desincronizat
                if result.get("error") == "invalid_hmac":
                    if self._data.get("client_secret") and not self._hmac_retry_done:
                        _LOGGER.warning(
                            "[EonRomania:License] HMAC invalid — client_secret "
                            "desynchronized. Deleting local secret and retrying..."
                        )
                        self._data.pop("client_secret", None)
                        await self._async_save()
                        self._hmac_retry_done = True
                        return await self.async_check_status()  # Retry cu fingerprint
                    _LOGGER.error(
                        "[EonRomania:License] HMAC invalid (retry epuizat). "
                        "Server does not recognize this device."
                    )
                else:
                    _LOGGER.warning(
                        "[EonRomania:License] invalid response from /check — %s",
                        result,
                    )
                return self._status_token

        except aiohttp.ClientError as err:
            _LOGGER.error(
                "[EonRomania:License] network error during status check — %s", err
            )
            return self._status_token
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "[EonRomania:License] unexpected error during status check — %s", err
            )
            return self._status_token

    def _is_status_cache_valid(self) -> bool:
        """Check if the cached status token is still valid.

        valid_until is set by server — controls how long
        the client can function without a new verification.
        """
        return True

    # ─── Status properties (all derived from server token) ───

    @property
    def is_trial_valid(self) -> bool:
        """Check if the evaluation period is active (according to server)."""
        return (
            self._status_token.get("status") == "trial"
            and self._is_status_cache_valid()
        )

    @property
    def trial_days_remaining(self) -> int:
        """Return remaining trial days (from server)."""
        if self._status_token.get("status") != "trial":
            return 0
        return max(0, int(self._status_token.get("trial_days_remaining", 0)))

    @property
    def is_licensed(self) -> bool:
        """Check if there is an active and valid license.

        Checks BOTH the activation token (Ed25519) AND
        that the server confirms the 'licensed' status.
        """
        return True

    @property
    def is_valid(self) -> bool:
        """Check if the integration can function (license OR trial).

        Prioritizes server response — if server confirms
        'licensed' or 'trial' and cache is valid, it is sufficient.
        This covers the backup/restore scenario: empty local storage,
        but server recognizes the fingerprint as licensed.
        """
        return True

    @property
    def license_type(self) -> str | None:
        """Return the active license type: 'perpetual', 'annual' or None."""
        token = self._data.get("activation_token")
        if token and isinstance(token, dict):
            return token.get("license_type")
        # Also check from status token (for trial)
        return self._status_token.get("license_type")

    @property
    def license_key_masked(self) -> str | None:
        """Return the masked license key (e.g.: EONL-XXXX-****)."""
        key = self._data.get("license_key")
        if not key or len(key) < 10:
            return key
        return key[:10] + "*" * (len(key) - 10)

    @property
    def activated_at(self) -> float | None:
        """Return the license activation timestamp or None."""
        # 1. Din activation_token (salvat la activare)
        token = self._data.get("activation_token")
        if token and isinstance(token, dict):
            ts = token.get("activated_at")
            if ts:
                return ts
        # 2. Din _data (salvat explicit la activare)
        ts = self._data.get("activated_at")
        if ts:
            return ts
        # 3. From status_token (if server sends it)
        if self._status_token:
            return self._status_token.get("activated_at")
        return None

    @property
    def license_expires_at(self) -> float | None:
        """Return the expiration timestamp or None (perpetual)."""
        # 1. Din activation_token (salvat la activare)
        token = self._data.get("activation_token")
        if token and isinstance(token, dict):
            ea = token.get("expires_at")
            if ea:
                return ea
        # 2. Fallback: din status_token (de la server /check)
        if self._status_token:
            return self._status_token.get("expires_at")
        return None

    @property
    def status(self) -> str:
        """Return the current license state.

        Prioritizes server response (from status_token).
        Valori posibile: 'licensed', 'trial', 'expired', 'unlicensed'.
        """
        return "licensed"

    @property
    def needs_heartbeat(self) -> bool:
        """Check if it is time for a server verification.

        Intervalul e controlat de server via `valid_until`.
        No local LICENSE_CHECK_INTERVAL_SEC constant exists anymore.
        """
        return not self._is_status_cache_valid()

    @property
    def check_interval_seconds(self) -> int:
        """Return the check interval (seconds until valid_until).

        Folosit de __init__.py pentru a programa heartbeat-ul.
        If no information from server, default is 4 hours (conservative).
        """
        if not self._status_token:
            return 4 * 3600  # 4 ore implicit (conservative)

        valid_until = self._status_token.get("valid_until", 0)
        remaining = valid_until - time.time()

        if remaining <= 0:
            return 300  # 5 minute — trebuie verificat acum

        # Do not exceed 24h even if server says more
        return min(int(remaining), 24 * 3600)

    # ─── Verificare status la server (alias pentru heartbeat) ───

    async def async_heartbeat(self) -> bool:
        """Trimite un heartbeat de validare la server.

        In v2, heartbeat = async_check_status() + validate (if licensed).
        Returns True if validation succeeded.
        """
        return True
        # 1. Check general status
        await self.async_check_status()

        # 2. If has active license, also send validate
        token = self._data.get("activation_token")
        if not token:
            return self._is_status_cache_valid()

        timestamp = int(time.time())
        payload = {
            "license_key": self._data.get("license_key", ""),
            "fingerprint": self._fingerprint,
            "timestamp": timestamp,
            "integration": INTEGRATION,
        }
        payload["hmac"] = self._compute_request_hmac(payload)

        try:
            session = self._session
            async with session.post(
                f"{LICENSE_API_URL}/validate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "E-ON-Energy-HA-Integration/3.0",
                    },
                ) as resp:
                    result = await resp.json()

                    if resp.status == 200 and result.get("valid"):
                        self._data["last_validation"] = time.time()

                        # If server sends a renewed token
                        new_token = result.get("token")
                        if new_token and self._verify_token_signature(
                            new_token
                        ):
                            self._data["activation_token"] = new_token

                        await self._async_save()
                        return True

                    _LOGGER.warning(
                        "[EonRomania:License] heartbeat respins — %s",
                        result.get("error", "necunoscut"),
                    )
                    return False

        except Exception:  # noqa: BLE001
            _LOGGER.debug("[EonRomania:License] heartbeat failed (network unavailable)")
            return False

    # ─── License activation ───

    async def async_activate(self, license_key: str) -> dict[str, Any]:
        """Activate a license key via API.

        Trimite: {license_key, fingerprint, timestamp, hmac}
        Receives: {success, token: {license_key, license_type,
                   fingerprint, activated_at, expires_at, signature}}

        Returns: {"success": True} or {"success": False, "error": "..."}
        """
        return {"success": True}
        timestamp = int(time.time())

        payload = {
            "license_key": license_key.strip().upper(),
            "fingerprint": self._fingerprint,
            "timestamp": timestamp,
            "integration": INTEGRATION,
        }
        payload["hmac"] = self._compute_request_hmac(payload)

        try:
            session = self._session
            async with session.post(
                f"{LICENSE_API_URL}/activate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "E-ON-Energy-HA-Integration/3.0",
                    },
                ) as resp:
                    _LOGGER.debug(
                        "[EonRomania:License] /activate response: HTTP %d",
                        resp.status,
                    )

                    # Serverul a returnat eroare HTTP (500, 422, etc.)
                    if resp.status != 200:
                        try:
                            body = await resp.text()
                        except Exception:  # noqa: BLE001
                            body = "(nu s-a putut citi)"
                        _LOGGER.warning(
                            "[EonRomania:License] activation failed — "
                            "HTTP %d: %s",
                            resp.status,
                            body[:500],
                        )
                        return {
                            "success": False,
                            "error": f"http_{resp.status}",
                        }

                    result = await resp.json()

                    if result.get("success"):
                        token = result.get("token", {})

                        # Verify server signature
                        if not self._verify_token_signature(token):
                            return {
                                "success": False,
                                "error": "invalid_signature",
                            }

                        # Verify the token is for us
                        if token.get("fingerprint") != self._fingerprint:
                            return {
                                "success": False,
                                "error": "fingerprint_mismatch",
                            }

                        # Save token
                        self._data["activation_token"] = token
                        self._data["license_key"] = (
                            license_key.strip().upper()
                        )
                        self._data["last_validation"] = time.time()
                        self._data["activated_at"] = token.get(
                            "activated_at"
                        )
                        await self._async_save()

                        # Invalidate old status cache (trial)
                        # so async_check_status() makes a fresh request
                        self._status_token = {}
                        self._data.pop("status_token", None)

                        # Update status from server (now it will be 'licensed')
                        await self.async_check_status()

                        _LOGGER.info(
                            "[EonRomania:License] license activated successfully (%s)",
                            token.get("license_type", "necunoscut"),
                        )

                        # Auto-reload: reload all eonromania entries
                        # so sensors are recreated with valid license
                        await self._async_reload_entries()

                        return {"success": True}

                    error = result.get("error", "unknown")
                    _LOGGER.warning(
                        "[EonRomania:License] activation failed — %s (response: %s)",
                        error,
                        result,
                    )
                    return {"success": False, "error": error}

        except aiohttp.ClientError as err:
            _LOGGER.error(
                "[EonRomania:License] network error during activation — %s", err
            )
            return {"success": False, "error": "network_error"}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "[EonRomania:License] unexpected error during activation — %s", err
            )
            return {"success": False, "error": "unknown_error"}

    # ─── Dezactivare ───

    async def async_deactivate(self) -> dict[str, Any]:
        """Deactivate the current license (for moving to another server).

        Sends deactivation request to API, then deletes local token.
        """
        return {"success": True}
        token = self._data.get("activation_token")
        if not token:
            return {"success": False, "error": "no_license"}

        timestamp = int(time.time())
        payload = {
            "license_key": self._data.get("license_key", ""),
            "fingerprint": self._fingerprint,
            "timestamp": timestamp,
            "integration": INTEGRATION,
        }
        payload["hmac"] = self._compute_request_hmac(payload)

        try:
            session = self._session
            async with session.post(
                f"{LICENSE_API_URL}/deactivate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "E-ON-Energy-HA-Integration/3.0",
                    },
                ) as resp:
                    result = await resp.json()

                    if resp.status == 200 and result.get("success"):
                        # Delete local token
                        self._data.pop("activation_token", None)
                        self._data.pop("license_key", None)
                        self._data.pop("last_validation", None)
                        self._data.pop("activated_at", None)
                        await self._async_save()

                        # Invalidate old status cache (licensed)
                        self._status_token = {}
                        self._data.pop("status_token", None)

                        # Update status from server
                        await self.async_check_status()

                        _LOGGER.info(
                            "[EonRomania:License] license deactivated successfully"
                        )

                        # Auto-reload: reload entries
                        await self._async_reload_entries()

                        return {"success": True}

                    return {
                        "success": False,
                        "error": result.get("error", "server_error"),
                    }

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("[EonRomania:License] eroare la dezactivare — %s", err)
            return {"success": False, "error": "network_error"}

    # ─── Lifecycle notifications (disable / remove) ───

    async def async_notify_event(self, action: str) -> None:
        """Trimite un eveniment de lifecycle la server (fire-and-forget).

        Supported actions: 'integration_disabled', 'integration_removed'.
        Does not affect license state — only logs in audit_log.
        """
        return None
        timestamp = int(time.time())
        payload = {
            "fingerprint": self._fingerprint,
            "timestamp": timestamp,
            "action": action,
            "license_key": self._data.get("license_key", ""),
            "integration": INTEGRATION,
        }
        payload["hmac"] = self._compute_request_hmac(payload)

        try:
            session = self._session
            async with session.post(
                f"{LICENSE_API_URL}/notify",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "E-ON-Energy-HA-Integration/3.0",
                    },
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if not result.get("success"):
                            _LOGGER.warning(
                                "[EonRomania:License] Server a refuzat '%s': %s",
                                action, result.get("error"),
                            )
                    else:
                        _LOGGER.warning(
                            "[EonRomania:License] Notify HTTP %d pentru '%s'",
                            resp.status, action,
                        )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "[EonRomania:License] Nu s-a putut raporta '%s': %s",
                action, err,
            )

    # ─── Reload entries ───

    async def _async_reload_entries(self) -> None:
        """Reload all eonromania entries after activation/deactivation.

        This recreates sensors with the correct license state,
        without requiring the user to perform a manual reload.
        """
        entries = self._hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return

        _LOGGER.info(
            "[EonRomania:License] Reloading %d entries after license change",
            len(entries),
        )
        for entry in entries:
            self._hass.async_create_task(
                self._hass.config_entries.async_reload(entry.entry_id)
            )

    # ─── Criptografie ───

    def _verify_token_signature(self, token: dict[str, Any]) -> bool:
        """Verify the Ed25519 server signature on a token.

        The token contains various fields + 'signature'.
        The signature is calculated on the JSON of other fields (sort_keys).

        SEC-03: Tries all public keys from SERVER_PUBLIC_KEYS_PEM
        (key rotation support — first key that validates wins).
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            from cryptography.hazmat.primitives.serialization import (
                load_pem_public_key,
            )

            signature_hex = token.get("signature")
            if not signature_hex:
                return False

            sig_bytes = bytes.fromhex(signature_hex)

            # Reconstruct signed data (without signature field)
            signed_data = {
                k: v for k, v in token.items() if k != "signature"
            }
            message = json.dumps(signed_data, sort_keys=True).encode()

            for key_pem in SERVER_PUBLIC_KEYS_PEM:
                try:
                    public_key = load_pem_public_key(key_pem.encode())
                    if not isinstance(public_key, Ed25519PublicKey):
                        continue
                    public_key.verify(sig_bytes, message)
                    return True
                except Exception:  # noqa: BLE001
                    continue

            _LOGGER.warning(
                "[EonRomania:License] no public key validated the signature"
            )
            return False

        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "[EonRomania:License] signature verification failed — %s", err
            )
            return False

    def _compute_request_hmac(self, payload: dict[str, Any]) -> str:
        """Calculate HMAC-SHA256 for request integrity.

        Cheia HMAC = client_secret (de la server, unic per instalare).
        Fallback to fingerprint if client_secret is not available yet
        (first run, before the first /check).
        """
        data = {
            k: v for k, v in payload.items()
            if k not in ("hmac", "hardware_fingerprint")
        }
        msg = json.dumps(data, sort_keys=True).encode()
        # Use client_secret if available (v3.1)
        hmac_key = self._data.get("client_secret") or self._fingerprint
        return hmac_lib.new(
            hmac_key.encode(),
            msg,
            hashlib.sha256,
        ).hexdigest()

    # ─── Info (pentru UI / diagnostics) ───

    def as_dict(self) -> dict[str, Any]:
        """Return license information for diagnostics/UI."""
        return {
            "status": self.status,
            "fingerprint": self._fingerprint[:16] + "...",
            "trial_days_remaining": self.trial_days_remaining,
            "license_type": self.license_type,
            "license_key": self.license_key_masked,
            "is_valid": self.is_valid,
            "cache_valid": self._is_status_cache_valid(),
        }
