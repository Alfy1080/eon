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
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL, DOMAIN_TOKEN_STORE, PLATFORMS
from .api import EonApiClient
from .coordinator import EonEnergyCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class EonEnergyRuntimeData:
    """Typed structure for the integration's runtime data."""

    coordinators: dict[str, EonEnergyCoordinator] = field(default_factory=dict)
    api_client: EonApiClient | None = None


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the global E-ON Energy integration."""
    return True


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
                hass, f"eonenergy_reauth_{contract}"
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
    coordinators: dict[str, EonEnergyCoordinator] = {}

    if is_account_only:
        # Account without contracts — single coordinator for personal data
        coordinator = EonEnergyCoordinator(
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

            coordinator = EonEnergyCoordinator(
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
    entry.runtime_data = EonEnergyRuntimeData(
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
        "[EonEnergy] ── async_unload_entry ── entry_id=%s",
        entry.entry_id,
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("[EonEnergy] Unload platforms: %s", "OK" if unload_ok else "FAILED")

    if unload_ok:
        # runtime_data is cleaned up automatically by HA at unload — no manual pop needed

        # Check if there are remaining active entries (BUG-03: use config_entries, not hass.data)
        remaining_entries = hass.config_entries.async_entries(DOMAIN)
        # Exclude the current entry (just unloaded)
        remaining_entry_ids = {e.entry_id for e in remaining_entries if e.entry_id != entry.entry_id}

        _LOGGER.debug(
            "[EonEnergy] Remaining entries after unload: %d (%s)",
            len(remaining_entry_ids),
            remaining_entry_ids or "none",
        )

        if not remaining_entry_ids:
            _LOGGER.info("[EonEnergy] Last entry unloaded — cleaning up domain completely")

            # Remove domain completely
            hass.data.pop(DOMAIN, None)
            _LOGGER.debug("[EonEnergy] hass.data[%s] removed completely", DOMAIN)

            _LOGGER.info("[EonEnergy] Cleanup complete — domain %s unloaded", DOMAIN)
    else:
        _LOGGER.error("[EonEnergy] Unload FAILED for entry_id=%s", entry.entry_id)

    return unload_ok


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
