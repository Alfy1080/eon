"""DataUpdateCoordinator for the E-ON Energy integration.

Update strategy:
- First update (refresh #0): calls ALL endpoints → detects capabilities
- Light refreshes: essential endpoints only (5 calls)
- Heavy refreshes (every 4th): + historical/optional endpoints
- Capabilities are recalibrated every 4th refresh (~1/day at 6h interval)
"""

import asyncio
import logging
from datetime import timedelta
import json

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EonApiClient
from .const import DOMAIN, LICENSE_DATA_KEY

_LOGGER = logging.getLogger(__name__)

# Every Nth refresh is "heavy" (includes historical/paginated endpoints)
HEAVY_REFRESH_EVERY = 4  # At 6h interval = heavy every 24h

# Pagination limit for paginated endpoints (payments, invoices_prosum)
MAX_PAGINATED_PAGES = 3


class EonRomaniaCoordinator(DataUpdateCoordinator):
    """Coordinator that handles all E-ON Energy data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: EonApiClient,
        cod_incasare: str,
        update_interval: int,
        is_collective: bool = False,
        config_entry: ConfigEntry | None = None,
        account_only: bool = False,
    ):
        """Initialize the coordinator with required parameters."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"EonRomaniaCoordinator_{cod_incasare}",
            update_interval=timedelta(seconds=update_interval),
        )
        self.api_client = api_client
        self.cod_incasare = cod_incasare
        self.is_collective = is_collective
        self.account_only = account_only
        self._config_entry = config_entry

        # Capabilities detected at first update
        # None = undetermined (first update will set them)
        self._capabilities: dict[str, bool] | None = None
        self._refresh_counter: int = 0

    @property
    def _is_heavy_refresh(self) -> bool:
        """Determine if the current refresh is "heavy" (includes historical endpoints)."""
        return self._refresh_counter % HEAVY_REFRESH_EVERY == 0

    def _update_capabilities(
        self,
        invoices_prosum,
        invoice_balance_prosum,
        rescheduling_plans,
        payments,
    ) -> None:
        """Update capabilities based on received data."""
        # Prosum: has data if invoices_prosum is non-empty OR invoice_balance_prosum has balance
        has_prosum = False
        if invoices_prosum and isinstance(invoices_prosum, list) and len(invoices_prosum) > 0:
            has_prosum = True
        elif invoice_balance_prosum and isinstance(invoice_balance_prosum, dict):
            # Check if balance_prosum has real data (not just empty structure)
            balance_val = invoice_balance_prosum.get("totalBalance") or invoice_balance_prosum.get("balance")
            if balance_val is not None and balance_val != 0:
                has_prosum = True

        has_rescheduling = bool(
            rescheduling_plans and isinstance(rescheduling_plans, list) and len(rescheduling_plans) > 0
        )

        has_payments = bool(
            payments and isinstance(payments, list) and len(payments) > 0
        )

        self._capabilities = {
            "has_prosum": has_prosum,
            "has_rescheduling": has_rescheduling,
            "has_payments": has_payments,
        }

        _LOGGER.info(
            "[CAPABILITIES] Detected (contract=%s): prosum=%s, rescheduling=%s, payments=%s.",
            self.cod_incasare,
            has_prosum,
            has_rescheduling,
            has_payments,
        )

    @property
    def capabilities(self) -> dict[str, bool] | None:
        """Return detected capabilities (None if not yet determined)."""
        return self._capabilities

    def _cap(self, key: str) -> bool:
        """Check a capability. Returns True if undetermined (first time)."""
        if self._capabilities is None:
            return True  # First update: call everything
        return self._capabilities.get(key, False)

    async def _async_update_data(self) -> dict:
        """Fetch data from API with light/heavy strategy.

        Light refresh (frecvent): contract_details, invoice_balance, invoices_unpaid,
            meter_index, consumption_convention
        Heavy refresh (rar): + payments, invoices_prosum, invoice_balance_prosum,
            rescheduling_plans, graphic_consumption, meter_history
        Account-only: user-details only (no contracts)
        """
        # License check — do not fetch data if the license/trial is not valid
        license_mgr = self.hass.data.get(DOMAIN, {}).get(LICENSE_DATA_KEY)
        if license_mgr and not license_mgr.is_valid:
            _LOGGER.debug("[EonRomania] Invalid license — skipping API calls")
            return self.data or {}

        # ── Account-only mode: personal data only ──
        if self.account_only:
            return await self._async_update_data_account_only()

        cod = self.cod_incasare
        is_heavy = self._is_heavy_refresh

        _LOGGER.debug(
            "E-ON update (contract=%s, collective=%s, refresh=#%s, type=%s).",
            cod, self.is_collective, self._refresh_counter,
            "HEAVY" if is_heavy else "light",
        )

        try:
            # Ensure valid token — refresh_token first, then full login
            # _ensure_token_valid() uses refresh_token (without MFA!) as first step
            if not self.api_client.is_token_likely_valid():
                # Check if login is blocked by MFA
                if self.api_client.mfa_blocked:
                    _LOGGER.warning(
                        "Login blocked — MFA required. Reconfigure the integration (contract=%s).",
                        cod,
                    )
                    self._create_reauth_notification()
                    raise UpdateFailed(
                        "Authentication requires MFA. "
                        "Reconfigure the integration from Settings → Devices & services → E-ON Energy."
                    )

                _LOGGER.debug(
                    "Token absent or likely expired. Ensuring valid token (contract=%s).",
                    cod,
                )
                ok = await self.api_client.async_ensure_authenticated()
                if not ok:
                    if self.api_client.mfa_blocked:
                        self._create_reauth_notification()
                    _LOGGER.warning(
                        "Authentication failed for E-ON API (contract=%s).", cod
                    )
                    raise UpdateFailed("Could not authenticate with E-ON API.")

            # ──────────────────────────────────────
            # ESSENTIAL endpoints (every refresh)
            # ──────────────────────────────────────
            essential_tasks = [
                self.api_client.async_fetch_contract_details(cod),
                self.api_client.async_fetch_invoice_balance(cod),
                self.api_client.async_fetch_invoices_unpaid(cod),
            ]

            (
                contract_details,
                invoice_balance,
                invoices_unpaid,
            ) = await asyncio.gather(*essential_tasks)

            _LOGGER.debug(
                "Essential data (contract=%s): contract_details=%s, invoice_balance=%s, "
                "invoices_unpaid=%s (len=%s).",
                cod,
                type(contract_details).__name__ if contract_details else None,
                type(invoice_balance).__name__ if invoice_balance else None,
                type(invoices_unpaid).__name__ if invoices_unpaid else None,
                len(invoices_unpaid) if isinstance(invoices_unpaid, list) else "N/A",
            )

            # ──────────────────────────────────────
            # HEAVY / OPTIONAL endpoints (heavy refresh only)
            # Previous data is reused on light refresh.
            # ──────────────────────────────────────
            prev = self.data or {}

            if is_heavy:
                heavy_tasks = []
                heavy_labels = []

                # Payments — only if has capability or first time
                if self._cap("has_payments"):
                    heavy_tasks.append(
                        self.api_client.async_fetch_payments(cod, max_pages=MAX_PAGINATED_PAGES)
                    )
                    heavy_labels.append("payments")

                # Prosum — only if has capability or first time
                if self._cap("has_prosum"):
                    heavy_tasks.append(
                        self.api_client.async_fetch_invoices_prosum(cod, max_pages=MAX_PAGINATED_PAGES)
                    )
                    heavy_labels.append("invoices_prosum")
                    heavy_tasks.append(
                        self.api_client.async_fetch_invoice_balance_prosum(cod)
                    )
                    heavy_labels.append("invoice_balance_prosum")

                # Rescheduling — only if has capability or first time
                if self._cap("has_rescheduling"):
                    heavy_tasks.append(
                        self.api_client.async_fetch_rescheduling_plans(cod)
                    )
                    heavy_labels.append("rescheduling_plans")

                if heavy_tasks:
                    heavy_results = await asyncio.gather(*heavy_tasks)
                    heavy_map = dict(zip(heavy_labels, heavy_results))
                else:
                    heavy_map = {}

                payments = heavy_map.get("payments")
                invoices_prosum = heavy_map.get("invoices_prosum")
                invoice_balance_prosum = heavy_map.get("invoice_balance_prosum")
                rescheduling_plans = heavy_map.get("rescheduling_plans")

                _LOGGER.debug(
                    "Heavy data (contract=%s): %s endpoints called (%s).",
                    cod, len(heavy_tasks), ", ".join(heavy_labels),
                )

                # Update capabilities (on every heavy refresh)
                self._update_capabilities(
                    invoices_prosum, invoice_balance_prosum,
                    rescheduling_plans, payments,
                )

            else:
                # Light refresh: reuse heavy data from previous refresh
                payments = prev.get("payments")
                invoices_prosum = prev.get("invoices_prosum")
                invoice_balance_prosum = prev.get("invoice_balance_prosum")
                rescheduling_plans = prev.get("rescheduling_plans")

            # ──────────────────────────────────────
            # Endpoints specific to contract type
            # ──────────────────────────────────────
            graphic_consumption = None
            meter_index = None
            consumption_convention = None
            meter_history = None
            subcontracts = None
            subcontracts_details = None
            subcontracts_conventions = None
            subcontracts_meter_index = None

            if not self.is_collective:
                # Individual contract: meter_index + consumption_convention on every refresh
                # graphic_consumption + meter_history only on heavy
                meter_essential_tasks = [
                    self.api_client.async_fetch_meter_index(cod),
                    self.api_client.async_fetch_consumption_convention(cod),
                ]

                (
                    meter_index,
                    consumption_convention,
                ) = await asyncio.gather(*meter_essential_tasks)

                if is_heavy:
                    meter_heavy_tasks = [
                        self.api_client.async_fetch_graphic_consumption(cod),
                        self.api_client.async_fetch_meter_history(cod),
                    ]
                    (
                        graphic_consumption,
                        meter_history,
                    ) = await asyncio.gather(*meter_heavy_tasks)
                else:
                    graphic_consumption = prev.get("graphic_consumption")
                    meter_history = prev.get("meter_history")

                _LOGGER.debug(
                    "Meter data (contract=%s): meter_index=%s, consumption_convention=%s, "
                    "graphic_consumption=%s, meter_history=%s.",
                    cod,
                    type(meter_index).__name__ if meter_index else None,
                    type(consumption_convention).__name__ if consumption_convention else None,
                    "fresh" if is_heavy and graphic_consumption else ("cached" if graphic_consumption else None),
                    "fresh" if is_heavy and meter_history else ("cached" if meter_history else None),
                )

            else:
                # Collective/DUO contract: subcontracts
                _LOGGER.debug(
                    "Collective/DUO contract (contract=%s). Querying subcontracts.",
                    cod,
                )
                raw_subs = await self.api_client.async_fetch_contracts_list(
                    collective_contract=cod
                )

                if raw_subs and isinstance(raw_subs, list):
                    subcontracts = [
                        s for s in raw_subs
                        if isinstance(s, dict) and s.get("accountContract")
                    ]

                    sub_codes = [s["accountContract"] for s in subcontracts]
                    if sub_codes:
                        # Essential per subcontract: details + convention + meter_index
                        detail_tasks = [
                            self.api_client.async_fetch_contract_details(sc)
                            for sc in sub_codes
                        ]
                        convention_tasks = [
                            self.api_client.async_fetch_consumption_convention(sc)
                            for sc in sub_codes
                        ]
                        meter_index_tasks = [
                            self.api_client.async_fetch_meter_index(sc)
                            for sc in sub_codes
                        ]
                        all_results = await asyncio.gather(
                            *detail_tasks, *convention_tasks, *meter_index_tasks
                        )

                        n = len(sub_codes)
                        detail_results = all_results[:n]
                        convention_results = all_results[n:2 * n]
                        meter_index_results = all_results[2 * n:]

                        subcontracts_details = [
                            d for d in detail_results if isinstance(d, dict)
                        ] or None

                        subcontracts_conventions = {}
                        for sc_code, conv_data in zip(sub_codes, convention_results):
                            if conv_data and isinstance(conv_data, list) and len(conv_data) > 0:
                                subcontracts_conventions[sc_code] = conv_data
                        subcontracts_conventions = subcontracts_conventions or None

                        subcontracts_meter_index = {}
                        for sc_code, mi_data in zip(sub_codes, meter_index_results):
                            if mi_data and isinstance(mi_data, dict):
                                subcontracts_meter_index[sc_code] = mi_data
                        subcontracts_meter_index = subcontracts_meter_index or None

                        _LOGGER.debug(
                            "DUO (contract=%s): %s subcontracts, details=%s, conventions=%s, meter_index=%s.",
                            cod, n,
                            len(subcontracts_details) if subcontracts_details else 0,
                            len(subcontracts_conventions) if subcontracts_conventions else 0,
                            len(subcontracts_meter_index) if subcontracts_meter_index else 0,
                        )

                    if not subcontracts:
                        subcontracts = None
                else:
                    _LOGGER.warning(
                        "DUO list (collective) invalid (contract=%s): %s.",
                        cod, type(raw_subs).__name__,
                    )

        except asyncio.TimeoutError as err:
            _LOGGER.error(
                "Timeout updating E-ON data (contract=%s): %s.", cod, err
            )
            raise UpdateFailed("Timeout updating E-ON data.") from err

        except UpdateFailed:
            raise

        except Exception as err:
            _LOGGER.exception(
                "Unexpected error updating E-ON data (contract=%s): %s",
                cod, err,
            )
            raise UpdateFailed("Unexpected error updating E-ON data.") from err

        # Verify essential data
        if self.is_collective:
            if contract_details is None:
                _LOGGER.error(
                    "Essential data unavailable: contract_details is None (collective contract=%s).",
                    cod,
                )
                raise UpdateFailed(
                    "Could not fetch essential data from E-ON (contract_details)."
                )
        else:
            if contract_details is None and meter_index is None:
                _LOGGER.error(
                    "Essential data unavailable (contract_details + meter_index are None) (contract=%s).",
                    cod,
                )
                raise UpdateFailed(
                    "Could not fetch essential data from E-ON (contract_details + meter_index)."
                )

        # Detect unit of measurement
        um = self._detect_unit(graphic_consumption)

        # Increment refresh counter
        self._refresh_counter += 1

        # Persist current token in config_entry.data (for HA restart)
        self._persist_token()

        # Summary
        _LOGGER.debug(
            "E-ON update completed (contract=%s, collective=%s, refresh=#%s).",
            cod, self.is_collective, self._refresh_counter - 1,
        )

        return {
            # Contract
            "contract_details": contract_details,
            # Invoices
            "invoices_unpaid": invoices_unpaid,
            "invoices_prosum": invoices_prosum,
            "invoice_balance": invoice_balance,
            "invoice_balance_prosum": invoice_balance_prosum,
            "rescheduling_plans": rescheduling_plans,
            "graphic_consumption": graphic_consumption,
            # Meter
            "meter_index": meter_index,
            "consumption_convention": consumption_convention,
            "meter_history": meter_history,
            # Payments
            "payments": payments,
            # Subcontracts (only for collective/DUO contracts)
            "subcontracts": subcontracts,
            "subcontracts_details": subcontracts_details,
            "subcontracts_conventions": subcontracts_conventions,
            "subcontracts_meter_index": subcontracts_meter_index,
            # Metadata
            "um": um,
            "is_collective": self.is_collective,
        }

    async def _async_update_data_account_only(self) -> dict:
        """Simplified update: user-details only (accounts without contracts)."""
        _LOGGER.debug(
            "E-ON account_only update (refresh=#%s).",
            self._refresh_counter,
        )

        try:
            # Ensure valid token
            if not self.api_client.is_token_likely_valid():
                if self.api_client.mfa_blocked:
                    _LOGGER.warning("Login blocked — MFA required (account_only).")
                    self._create_reauth_notification()
                    raise UpdateFailed(
                        "Authentication requires MFA. "
                        "Reconfigure the integration from Settings → Devices & services → E-ON Energy."
                    )

                ok = await self.api_client.async_ensure_authenticated()
                if not ok:
                    if self.api_client.mfa_blocked:
                        self._create_reauth_notification()
                    raise UpdateFailed("Could not authenticate with E-ON API.")

            user_details = await self.api_client.async_fetch_user_details()

        except UpdateFailed:
            raise
        except Exception as err:
            _LOGGER.exception("Error updating account_only: %s", err)
            raise UpdateFailed("Error fetching personal data.") from err

        if not user_details or not isinstance(user_details, dict):
            raise UpdateFailed("Could not fetch personal data (user-details).")

        self._refresh_counter += 1
        self._persist_token()

        _LOGGER.debug(
            "Account_only update completed (refresh=#%s, user=%s).",
            self._refresh_counter - 1,
            user_details.get("email", "N/A"),
        )

        return {
            "account_only": True,
            "user_details": user_details,
        }

    def _persist_token(self) -> None:
        """Persist current token in config_entry.data for HA restart.

        Saves refresh_token + access_token so that on restart
        the coordinator can use refresh_token without requiring MFA.
        """
        if self._config_entry is None:
            return
        token_data = self.api_client.export_token_data()
        if token_data is None:
            return

        current_data = dict(self._config_entry.data)
        old_token = current_data.get("token_data", {})

        # Only update if something changed (avoid unnecessary writes)
        if (
            old_token.get("access_token") == token_data.get("access_token")
            and old_token.get("refresh_token") == token_data.get("refresh_token")
        ):
            return

        current_data["token_data"] = token_data
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=current_data
        )
        _LOGGER.debug(
            "Token persisted in config_entry (contract=%s, access=%s...).",
            self.cod_incasare,
            token_data["access_token"][:8] if token_data.get("access_token") else "None",
        )

    def _create_reauth_notification(self) -> None:
        """Create a persistent notification requesting MFA reconfiguration."""
        from homeassistant.components import persistent_notification

        notification_id = f"eonromania_reauth_{self.cod_incasare}"
        persistent_notification.async_create(
            self.hass,
            message=(
                f"The E-ON session for contract **{self.cod_incasare}** has expired "
                f"and re-authentication with MFA code is required.\n\n"
                f"Go to **Settings → Devices & services → E-ON Energy → "
                f"Reconfigure** to re-authenticate.\n\n"
                f"Until reconfiguration, the integration will NOT attempt login "
                f"(to avoid sending repeated MFA emails)."
            ),
            title="E-ON Energy — Authentication required",
            notification_id=notification_id,
        )
        _LOGGER.info(
            "Persistent notification created: reconfiguration required (contract=%s).",
            self.cod_incasare,
        )

    @staticmethod
    def _detect_unit(graphic_consumption_data) -> str:
        """Detect unit of measurement: m3 (gas) or kWh (electricity)."""
        if not graphic_consumption_data or not isinstance(graphic_consumption_data, dict):
            return "m3"
        um_raw = graphic_consumption_data.get("um")
        if um_raw:
            return um_raw.lower()
        return "m3"
