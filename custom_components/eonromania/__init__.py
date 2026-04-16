"""Initialization of the E-ON Energy integration."""

import logging
from datetime import timedelta
from dataclasses import dataclass, field

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL, DOMAIN_TOKEN_STORE, LICENSE_DATA_KEY, LICENSE_PURCHASE_URL, PLATFORMS
from .api import EonApiClient
from .coordinator import EonRomaniaCoordinator
from .license import LicenseManager

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class EonRomaniaRuntimeData:
    """Typed structure for the integration's runtime data."""

    coordinators: dict[str, EonRomaniaCoordinator] = field(default_factory=dict)
    api_client: EonApiClient | None = None


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the global E-ON Energy integration."""
    return True


def _update_license_notifications(hass: HomeAssistant, mgr: LicenseManager) -> None:
    """Create or dismiss license/trial expiration notifications."""
    if mgr.is_valid:
        ir.async_delete_issue(hass, DOMAIN, "trial_expired")
        ir.async_delete_issue(hass, DOMAIN, "license_expired")
        persistent_notification.async_dismiss(hass, "eonromania_license_expired")
        return

    has_token = bool(mgr._data.get("activation_token"))

    if has_token:
        issue_id = "license_expired"
        notif_title = "E.ON Romania — License expired"
        notif_message = (
            "The license for the **E.ON Romania** integration has expired.\n\n"
            "Sensors are disabled until the license is renewed.\n\n"
            f"[Renew license]({LICENSE_PURCHASE_URL})"
        )
    else:
        issue_id = "trial_expired"
        notif_title = "E.ON Romania — Trial period expired"
        notif_message = (
            "The free trial period for the **E.ON Romania** integration has ended.\n\n"
            "Sensors are disabled until a license is obtained.\n\n"
            f"[Get a license now]({LICENSE_PURCHASE_URL})"
        )

    other_id = "license_expired" if issue_id == "trial_expired" else "trial_expired"
    ir.async_delete_issue(hass, DOMAIN, other_id)

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        learn_more_url=LICENSE_PURCHASE_URL,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue_id,
        translation_placeholders={"learn_more_url": LICENSE_PURCHASE_URL},
    )

    persistent_notification.async_create(
        hass,
        notif_message,
        title=notif_title,
        notification_id="eonromania_license_expired",
    )

    _LOGGER.debug("[EON] Expiration notification created: %s", issue_id)


async def _handle_setup_failure(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle setup failure with optional auto-reload."""
    auto_reload_enabled = entry.data.get("auto_reload_on_failure", False)
    if auto_reload_enabled:
        reload_interval_min = entry.data.get("auto_reload_interval", 30)
        _LOGGER.warning(
            "Initialization failed, but auto-reload is enabled. "
            "Will retry in %d minutes.",
            reload_interval_min,
        )

        async def _auto_reload_entry(_now):
            _LOGGER.info(
                "Auto-reloading entry %s after previous failure.",
                entry.entry_id,
            )
            await hass.config_entries.async_reload(entry.entry_id)

        async_track_point_in_time(
            hass,
            _auto_reload_entry,
            dt_util.utcnow() + timedelta(minutes=reload_interval_min),
        )
        # Return True to prevent HA's backoff mechanism, as we are handling it.
        # The entry will be in a loaded state but without devices until reload.
        return True

    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up the integration for a specific config entry."""
    _LOGGER.info("Setting up integration %s (entry_id=%s).", DOMAIN, entry.entry_id)

    hass.data.setdefault(DOMAIN, {})

    # ── Initialize License Manager (single instance per domain) ──
    if LICENSE_DATA_KEY not in hass.data.get(DOMAIN, {}):
        _LOGGER.debug("[EonRomania] Initializing LicenseManager (first entry)")
        license_mgr = LicenseManager(hass)
        # IMPORTANT: set the reference BEFORE async_load() to prevent
        # race conditions: async_load() does an await HTTP call, which yields
        # the event loop. Without this order, other concurrent entries would see
        # LICENSE_DATA_KEY as missing and create duplicate LicenseManagers,
        # generating N simultaneous /check requests (one per entry).
        hass.data[DOMAIN][LICENSE_DATA_KEY] = license_mgr
        await license_mgr.async_load()
        _LOGGER.debug(
            "[EonRomania] LicenseManager: status=%s, valid=%s, fingerprint=%s...",
            license_mgr.status,
            license_mgr.is_valid,
            license_mgr.fingerprint[:16],
        )

        # Periodic heartbeat — interval comes from server (via valid_until)
        from datetime import timedelta

        from homeassistant.helpers.event import (
            async_track_point_in_time,
            async_track_time_interval,
        )
        from homeassistant.util import dt as dt_util

        interval_sec = license_mgr.check_interval_seconds
        _LOGGER.debug(
            "[EonRomania] Scheduling periodic heartbeat every %d seconds (%d hours)",
            interval_sec,
            interval_sec // 3600,
        )

        async def _heartbeat_periodic(_now) -> None:
            """Check status with server if cache has expired.

            Logic:
            1. Capture is_valid BEFORE heartbeat
            2. If cache expired → contact server
            3. Capture is_valid AFTER heartbeat
            4. If state changed → reload entries (clean transition)
            5. Reschedule heartbeat at updated server interval
            """
            mgr: LicenseManager | None = hass.data.get(DOMAIN, {}).get(
                LICENSE_DATA_KEY
            )
            if not mgr:
                _LOGGER.debug("[EonRomania] Heartbeat: LicenseManager not found, skip")
                return

            # Capture state BEFORE heartbeat
            was_valid = mgr.is_valid

            if mgr.needs_heartbeat:
                _LOGGER.debug("[EonRomania] Heartbeat: cache expired, checking with server")
                await mgr.async_heartbeat()

                # Capture state AFTER heartbeat
                now_valid = mgr.is_valid

                # Detect transitions that async_check_status didn't catch
                # (e.g.: server unreachable + cache expired → is_valid becomes False)
                if was_valid and not now_valid:
                    _LOGGER.warning(
                        "[EonRomania] License became invalid — reloading sensors"
                    )
                    _update_license_notifications(hass, mgr)
                    await mgr._async_reload_entries()
                elif not was_valid and now_valid:
                    _LOGGER.info(
                        "[EonRomania] License became valid again — reloading sensors"
                    )
                    _update_license_notifications(hass, mgr)
                    await mgr._async_reload_entries()

                # Reschedule heartbeat at updated server interval
                new_interval = mgr.check_interval_seconds
                _LOGGER.debug(
                    "[EonRomania] Heartbeat: rescheduling at %d seconds (%d min)",
                    new_interval,
                    new_interval // 60,
                )
                # Stop old timer
                cancel_old = hass.data.get(DOMAIN, {}).get("_cancel_heartbeat")
                if cancel_old:
                    cancel_old()
                # Schedule new timer with updated interval
                cancel_new = async_track_time_interval(
                    hass,
                    _heartbeat_periodic,
                    timedelta(seconds=new_interval),
                )
                hass.data[DOMAIN]["_cancel_heartbeat"] = cancel_new
            else:
                _LOGGER.debug("[EonRomania] Heartbeat: cache valid, no check needed")

        cancel_heartbeat = async_track_time_interval(
            hass,
            _heartbeat_periodic,
            timedelta(seconds=interval_sec),
        )
        hass.data[DOMAIN]["_cancel_heartbeat"] = cancel_heartbeat
        _LOGGER.debug("[EonRomania] Heartbeat scheduled and stored in hass.data")

        # ── Precise timer at valid_until (zero gap at cache expiry) ──
        def _schedule_cache_expiry_check(mgr_ref: LicenseManager) -> None:
            """Schedule a check EXACTLY at the moment of cache expiry.

            Completely eliminates the window between cache expiry and
            the next periodic heartbeat. At expiry, contacts the
            server immediately and triggers reload if state changes.
            """
            # Cancel previous timer (if exists)
            cancel_prev = hass.data.get(DOMAIN, {}).pop(
                "_cancel_cache_expiry", None
            )
            if cancel_prev:
                cancel_prev()

            valid_until = (mgr_ref._status_token or {}).get("valid_until")
            if not valid_until or valid_until <= 0:
                return

            expiry_dt = dt_util.utc_from_timestamp(valid_until)
            # Add 2 seconds as margin (avoids race condition with cache check)
            expiry_dt = expiry_dt + timedelta(seconds=2)

            async def _on_cache_expiry(_now) -> None:
                """Callback executed EXACTLY at cache expiry."""
                mgr_now: LicenseManager | None = hass.data.get(
                    DOMAIN, {}
                ).get(LICENSE_DATA_KEY)
                if not mgr_now:
                    return

                was_valid = mgr_now.is_valid
                _LOGGER.debug(
                    "[E-ON Energy] Cache expired — checking with server immediately"
                )
                await mgr_now.async_check_status()
                now_valid = mgr_now.is_valid

                if was_valid != now_valid:
                    if now_valid:
                        _LOGGER.info(
                            "[E-ON Energy] License became valid again — reloading"
                        )
                    else:
                        _LOGGER.warning(
                            "[E-ON Energy] License became invalid — reloading"
                        )
                    _update_license_notifications(hass, mgr_now)
                    await mgr_now._async_reload_entries()

                # Schedule next check (if server gave a new valid_until)
                _schedule_cache_expiry_check(mgr_now)

            cancel_expiry = async_track_point_in_time(
                hass, _on_cache_expiry, expiry_dt
            )
            hass.data[DOMAIN]["_cancel_cache_expiry"] = cancel_expiry

            _LOGGER.debug(
                "[E-ON Energy] Cache expiry timer scheduled at %s",
                expiry_dt.isoformat(),
            )

        _schedule_cache_expiry_check(license_mgr)


        # ── Re-enable notification (if previously disabled) ──
        was_disabled = hass.data.pop(f"{DOMAIN}_was_disabled", False)
        if was_disabled:
            await license_mgr.async_notify_event("integration_enabled")

        if not license_mgr.is_valid:
            _LOGGER.warning(
                "[EonRomania] Integration does not have a valid license. "
                "Sensors will show 'License required'."
            )
        elif license_mgr.is_trial_valid:
            _LOGGER.info(
                "[EonRomania] Trial period — %d days remaining",
                license_mgr.trial_days_remaining,
            )
        else:
            _LOGGER.info(
                "[EonRomania] Active license — type: %s",
                license_mgr.license_type,
            )

        _update_license_notifications(hass, license_mgr)
    else:
        _LOGGER.debug(
            "[EonRomania] LicenseManager already exists (additional entry)"
        )

    session = async_get_clientsession(hass)
    username = entry.data["username"]
    password = entry.data["password"]
    update_interval = entry.data.get("update_interval", DEFAULT_UPDATE_INTERVAL)

    # Compatibility: old format (single cod_incasare) vs new (list)
    selected_contracts = entry.data.get("selected_contracts", [])
    if not selected_contracts:
        # Old format — single contract
        old_cod = entry.data.get("cod_incasare", "")
        if old_cod:
            selected_contracts = [old_cod]

    is_account_only = entry.data.get("account_only", False) or not selected_contracts

    if not selected_contracts and not is_account_only:
        _LOGGER.error(
            "No contracts selected for %s (entry_id=%s).",
            DOMAIN, entry.entry_id,
        )
        return False

    _LOGGER.debug(
        "Selected contracts for %s (entry_id=%s): %s, interval=%ss, account_only=%s.",
        DOMAIN, entry.entry_id, selected_contracts, update_interval, is_account_only,
    )

    # Single shared API client (one account, one token)
    api_client = EonApiClient(session, username, password)

    # Inject saved token — priority: hass.data (fresh, from config_flow),
    # then config_entry.data (persistent, for HA restart)
    token_store = hass.data.get(DOMAIN_TOKEN_STORE, {})
    stored_token = token_store.pop(username.lower(), None)
    if stored_token:
        api_client.inject_token(stored_token)
        _LOGGER.debug(
            "Token injected from config_flow (fresh) for %s (entry_id=%s).",
            username, entry.entry_id,
        )
        # Dismiss re-authentication notification (if exists)
        for contract in selected_contracts:
            persistent_notification.async_dismiss(
                hass, f"eonromania_reauth_{contract}"
            )
    elif entry.data.get("token_data"):
        api_client.inject_token(entry.data["token_data"])
        _LOGGER.debug(
            "Token injected from config_entry.data (persistent) for %s (entry_id=%s).",
            username, entry.entry_id,
        )
    else:
        _LOGGER.debug(
            "No saved token available for %s (entry_id=%s). Will perform login.",
            username, entry.entry_id,
        )
    # Clean up store if empty
    if DOMAIN_TOKEN_STORE in hass.data and not hass.data[DOMAIN_TOKEN_STORE]:
        hass.data.pop(DOMAIN_TOKEN_STORE, None)

    # Contract metadata (utility type, collective/not)
    contract_metadata = entry.data.get("contract_metadata", {})

    # Create one coordinator per selected contract
    coordinators: dict[str, EonRomaniaCoordinator] = {}

    if is_account_only:
        # Account without contracts — single coordinator for personal data
        coordinator = EonRomaniaCoordinator(
            hass,
            api_client=api_client,
            cod_incasare="__account__",
            update_interval=update_interval,
            is_collective=False,
            config_entry=entry,
            account_only=True,
        )

        try:
            await coordinator.async_config_entry_first_refresh()
        except UpdateFailed as err:
            _LOGGER.error(
                "First update failed for personal data (entry_id=%s): %s",
                entry.entry_id, err,
            )
            return await _handle_setup_failure(hass, entry)
        except Exception as err:
            _LOGGER.exception(
                "Unexpected error for personal data (entry_id=%s): %s",
                entry.entry_id, err,
            )
            return await _handle_setup_failure(hass, entry)

        coordinators["__account__"] = coordinator
    else:
        for cod in selected_contracts:
            meta = contract_metadata.get(cod, {})
            is_collective = meta.get("is_collective", False)

            coordinator = EonRomaniaCoordinator(
                hass,
                api_client=api_client,
                cod_incasare=cod,
                update_interval=update_interval,
                is_collective=is_collective,
                config_entry=entry,
            )

            try:
                await coordinator.async_config_entry_first_refresh()
            except UpdateFailed as err:
                _LOGGER.error(
                    "First update failed (entry_id=%s, contract=%s): %s",
                    entry.entry_id, cod, err,
                )
                # Continue with remaining contracts — don't stop everything for one
                continue
            except Exception as err:
                _LOGGER.exception(
                    "Unexpected error at first update (entry_id=%s, contract=%s): %s",
                    entry.entry_id, cod, err,
                )
                continue

            coordinators[cod] = coordinator

    if not coordinators:
        _LOGGER.error(
            "No coordinator initialized successfully for %s (entry_id=%s).",
            DOMAIN, entry.entry_id,
        )
        return await _handle_setup_failure(hass, entry)

    _LOGGER.info(
        "%s active coordinators out of %s selected contracts (entry_id=%s, account_only=%s).",
        len(coordinators), len(selected_contracts), entry.entry_id, is_account_only,
    )

    # Save runtime data
    entry.runtime_data = EonRomaniaRuntimeData(
        coordinators=coordinators,
        api_client=api_client,
    )

    # Load platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listener for options changes
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _LOGGER.info(
        "Integration %s configured (entry_id=%s, contracts=%s).",
        DOMAIN, entry.entry_id, list(coordinators.keys()),
    )
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """Reload integration when options change."""
    _LOGGER.info(
        "Integration %s options changed (entry_id=%s). Reloading...",
        DOMAIN, entry.entry_id,
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload an entry from config_entries."""
    _LOGGER.info(
        "[EonRomania] ── async_unload_entry ── entry_id=%s",
        entry.entry_id,
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("[EonRomania] Unload platforms: %s", "OK" if unload_ok else "FAILED")

    if unload_ok:
        # runtime_data is cleaned up automatically by HA at unload — no manual pop needed

        # Check if there are remaining active entries (BUG-03: use config_entries, not hass.data)
        remaining_entries = hass.config_entries.async_entries(DOMAIN)
        # Exclude the current entry (just unloaded)
        remaining_entry_ids = {e.entry_id for e in remaining_entries if e.entry_id != entry.entry_id}

        _LOGGER.debug(
            "[EonRomania] Remaining entries after unload: %d (%s)",
            len(remaining_entry_ids),
            remaining_entry_ids or "none",
        )

        if not remaining_entry_ids:
            _LOGGER.info("[EonRomania] Last entry unloaded — cleaning up domain completely")

            # ── Lifecycle notification (before cleanup!) ──
            mgr = hass.data[DOMAIN].get(LICENSE_DATA_KEY)
            if mgr and not hass.is_stopping:
                if entry.disabled_by:
                    await mgr.async_notify_event("integration_disabled")
                    # Flag for async_setup_entry: on re-enable, send "enabled"
                    hass.data[f"{DOMAIN}_was_disabled"] = True
                else:
                    # Save fingerprint for async_remove_entry
                    hass.data.setdefault(f"{DOMAIN}_notify", {}).update({
                        "fingerprint": mgr.fingerprint,
                        "license_key": mgr._data.get("license_key", ""),
                    })
                    _LOGGER.debug(
                        "[EonRomania] Fingerprint saved for async_remove_entry"
                    )

            # Stop periodic heartbeat
            cancel_hb = hass.data[DOMAIN].pop("_cancel_heartbeat", None)
            if cancel_hb:
                cancel_hb()
                _LOGGER.debug("[EonRomania] Periodic heartbeat stopped")

            # Stop cache expiry timer
            cancel_ce = hass.data[DOMAIN].pop("_cancel_cache_expiry", None)
            if cancel_ce:
                cancel_ce()
                _LOGGER.debug("[E-ON Energy] Cache expiry timer stopped")

            # Remove LicenseManager
            hass.data[DOMAIN].pop(LICENSE_DATA_KEY, None)
            _LOGGER.debug("[EonRomania] LicenseManager removed")

            # Remove domain completely
            hass.data.pop(DOMAIN, None)
            _LOGGER.debug("[EonRomania] hass.data[%s] removed completely", DOMAIN)

            _LOGGER.info("[EonRomania] Cleanup complete — domain %s unloaded", DOMAIN)
    else:
        _LOGGER.error("[EonRomania] Unload FAILED for entry_id=%s", entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Notify server when the integration is completely removed (deleted)."""
    _LOGGER.debug(
        "[EonRomania] ── async_remove_entry ── entry_id=%s",
        entry.entry_id,
    )

    # Check if there are remaining entries
    remaining = hass.config_entries.async_entries(DOMAIN)
    if not remaining:
        notify_data = hass.data.pop(f"{DOMAIN}_notify", None)
        if notify_data and notify_data.get("fingerprint"):
            await _send_lifecycle_event(
                hass,
                notify_data["fingerprint"],
                notify_data.get("license_key", ""),
                "integration_removed",
            )


async def _send_lifecycle_event(
    hass: HomeAssistant, fingerprint: str, license_key: str, action: str
) -> None:
    """Send a lifecycle event directly (without LicenseManager).

    Used in async_remove_entry when LicenseManager no longer exists.
    BUG-06: Uses HA's shared session instead of a new aiohttp.ClientSession().
    """
    import hashlib
    import hmac as hmac_lib
    import json
    import time

    import aiohttp

    from .license import INTEGRATION, LICENSE_API_URL

    timestamp = int(time.time())
    payload = {
        "fingerprint": fingerprint,
        "timestamp": timestamp,
        "action": action,
        "license_key": license_key,
        "integration": INTEGRATION,
    }
    data = {k: v for k, v in payload.items() if k != "hmac"}
    msg = json.dumps(data, sort_keys=True).encode()
    payload["hmac"] = hmac_lib.new(
        fingerprint.encode(), msg, hashlib.sha256
    ).hexdigest()

    try:
        session = async_get_clientsession(hass)
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
                        "[EonRomania] Server rejected '%s': %s",
                        action, result.get("error"),
                    )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("[EonRomania] Could not report '%s': %s", action, err)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate from old versions to the current version."""
    _LOGGER.debug(
        "Migrating config entry %s from version %s.",
        config_entry.entry_id, config_entry.version,
    )

    if config_entry.version < 3:
        # v1/v2 → v3: convert cod_incasare to selected_contracts[]
        old_data = dict(config_entry.data)
        old_cod = old_data.get("cod_incasare", "")
        old_interval = old_data.get("update_interval",
                        config_entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL))

        new_data = {
            "username": old_data.get("username", ""),
            "password": old_data.get("password", ""),
            "update_interval": old_interval,
            "select_all": False,
            "selected_contracts": [old_cod] if old_cod else [],
        }
        # BUG-04: Preserve token_data during migration (avoids re-authentication with MFA)
        if old_data.get("token_data"):
            new_data["token_data"] = old_data["token_data"]

        _LOGGER.info(
            "Migrating entry %s: v%s → v3 (cod_incasare=%s → selected_contracts).",
            config_entry.entry_id, config_entry.version, old_cod,
        )

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options={}, version=3
        )
        return True

    _LOGGER.error(
        "Unknown version for migration: %s (entry_id=%s).",
        config_entry.version, config_entry.entry_id,
    )
    return False
