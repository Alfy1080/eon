"""API client for communication with E-ON Energy."""

import asyncio
import logging
import time
import json

from aiohttp import ClientSession, ClientTimeout

from .const import (
    API_TIMEOUT,
    AUTH_VERIFY_SECRET,
    HEADERS,
    MFA_REQUIRED_CODE,
    TOKEN_MAX_AGE,
    TOKEN_REFRESH_THRESHOLD,
    URL_CONSUMPTION_CONVENTION,
    URL_CONTRACT_DETAILS,
    URL_CONTRACTS_DETAILS_LIST,
    URL_CONTRACTS_LIST,
    URL_CONTRACTS_WITH_SUBCONTRACTS,
    URL_GRAPHIC_CONSUMPTION,
    URL_INVOICE_BALANCE,
    URL_INVOICE_BALANCE_PROSUM,
    URL_INVOICES_PROSUM,
    URL_INVOICES_UNPAID,
    URL_LOGIN,
    URL_METER_HISTORY,
    URL_METER_INDEX,
    URL_METER_SUBMIT,
    URL_MFA_LOGIN,
    URL_MFA_RESEND,
    URL_PAYMENT_LIST,
    URL_REFRESH_TOKEN,
    URL_RESCHEDULING_PLANS,
    URL_USER_DETAILS,
)
from .helpers import generate_verify_hmac

_LOGGER = logging.getLogger(__name__)
_DEBUG = _LOGGER.isEnabledFor(logging.DEBUG)


def _safe_debug_sample(data, max_len: int = 500) -> str:
    """Return a safe JSON sample for logging (without unnecessary serialization)."""
    if data is None:
        return "None"
    try:
        return json.dumps(data, default=str)[:max_len]
    except Exception:  # noqa: BLE001
        return str(data)[:max_len]


class EonApiClient:
    """Class for communicating with the E-ON Energy API."""

    def __init__(self, session: ClientSession, username: str, password: str):
        """Initialize the API client with a ClientSession."""
        self._session = session
        self._username = username
        self._password = password

        # Token management
        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._expires_in: int = 3600
        self._refresh_token: str | None = None
        self._id_token: str | None = None
        self._uuid: str | None = None
        self._token_obtained_at: float = 0.0

        self._timeout = ClientTimeout(total=API_TIMEOUT)
        self._auth_lock = asyncio.Lock()
        self._token_generation: int = 0

        # MFA state (set by async_login when MFA is required)
        self._mfa_data: dict | None = None

        # ── MFA guard ──
        # When login requires MFA in background (not in config_flow),
        # we block any login retry to prevent:
        # 1. Flood of MFA emails on every update cycle
        # 2. Parallel logins when multiple requests receive 401 simultaneously
        # Reset on inject_token() (after reconfiguration via UI)
        self._mfa_blocked: bool = False

    # ──────────────────────────────────────────
    # Public properties
    # ──────────────────────────────────────────

    @property
    def has_token(self) -> bool:
        """Check if a token is set (does not guarantee validity)."""
        return self._access_token is not None

    @property
    def uuid(self) -> str | None:
        """Return the UUID of the authenticated user."""
        return self._uuid

    @property
    def mfa_required(self) -> bool:
        """Check if login returned an MFA requirement (2FA)."""
        return self._mfa_data is not None

    @property
    def mfa_data(self) -> dict | None:
        """Return MFA data (uuid, type, recipient, etc.) or None."""
        return self._mfa_data

    @property
    def mfa_blocked(self) -> bool:
        """True if login is blocked due to MFA required in background."""
        return self._mfa_blocked

    def clear_mfa_block(self) -> None:
        """Reset MFA block (called after reconfiguration via UI)."""
        self._mfa_blocked = False
        self._mfa_data = None
        _LOGGER.debug("[AUTH] MFA block reset.")

    def is_token_likely_valid(self) -> bool:
        """Check if token exists AND has not exceeded the estimated maximum duration."""
        if self._access_token is None:
            return False
        age = time.monotonic() - self._token_obtained_at
        # Use expires_in from API response, with fallback to TOKEN_MAX_AGE
        effective_max = self._expires_in - TOKEN_REFRESH_THRESHOLD if self._expires_in > TOKEN_REFRESH_THRESHOLD else TOKEN_MAX_AGE
        return age < effective_max

    def export_token_data(self) -> dict | None:
        """Export token data to be re-injected in another instance.

        Used by config_flow to save the token after MFA authentication,
        so that __init__.py can inject it into the coordinator API client.

        Also saves the real timestamp (wall clock) of when the token was obtained,
        so that inject_token() can correctly calculate the token age
        even after HA restart (time.monotonic() resets on reboot).
        """
        if self._access_token is None:
            return None
        return {
            "access_token": self._access_token,
            "token_type": self._token_type,
            "expires_in": self._expires_in,
            "refresh_token": self._refresh_token,
            "id_token": self._id_token,
            "uuid": self._uuid,
            "obtained_at_wallclock": time.time() - (time.monotonic() - self._token_obtained_at),
        }

    def inject_token(self, token_data: dict) -> None:
        """Inject an existing token (previously obtained, e.g. from config_flow).

        Calculates the real age of the token using obtained_at_wallclock
        (wall clock saved at export). If the token is clearly expired,
        is_token_likely_valid() will return False immediately → will do
        refresh_token directly, without wasting a request on 401.

        Resets MFA block (new token comes from reconfiguration via UI).
        """
        self._access_token = token_data.get("access_token")
        self._token_type = token_data.get("token_type", "Bearer")
        self._expires_in = token_data.get("expires_in", 3600)
        self._refresh_token = token_data.get("refresh_token")
        self._id_token = token_data.get("id_token")
        self._uuid = token_data.get("uuid")

        # Calculate the real age of the token
        wallclock_obtained = token_data.get("obtained_at_wallclock")
        if wallclock_obtained:
            # How long has passed since the token was obtained (real seconds)
            age_seconds = time.time() - wallclock_obtained
            if age_seconds < 0:
                age_seconds = 0  # Disordered clock — treat as fresh
            # Set _token_obtained_at in the past by the real age
            self._token_obtained_at = time.monotonic() - age_seconds
            _LOGGER.debug(
                "Token injected with real age: %.0fs (expires_in=%s).",
                age_seconds, self._expires_in,
            )
        else:
            # No wallclock (old format) — force immediate refresh
            # Set token_obtained_at to 0 → is_token_likely_valid() returns False
            # → _ensure_token_valid() will try refresh_token (without MFA!)
            self._token_obtained_at = 0.0
            _LOGGER.debug(
                "Token injected without wallclock (old format) — will refresh on first request.",
            )

        self._token_generation += 1
        # Reset MFA block — new token comes from config_flow with MFA completed
        self._mfa_blocked = False
        self._mfa_data = None
        _LOGGER.debug(
            "Token injectat (access=%s..., refresh=%s, gen=%s, valid=%s).",
            f"***({len(self._access_token)}ch)" if self._access_token else "None",
            "yes" if self._refresh_token else "no",
            self._token_generation,
            self.is_token_likely_valid(),
        )

    # ──────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────

    async def async_login(self) -> bool:
        """Obtain a new authentication token via mobile-login.

        Returns True if the token was obtained successfully.
        Returns False if authentication failed OR if MFA is required.

        When MFA is required (HTTP 400, code 6054):
        - Stores MFA data in self._mfa_data
        - Returns False (no token yet)
        - Config flow checks self.mfa_required and shows MFA form
        - Coordinator (runtime) will raise UpdateFailed — MFA cannot be handled automatically
        """
        self._mfa_data = None  # Reset MFA state on every login attempt

        verify = generate_verify_hmac(self._username, AUTH_VERIFY_SECRET)
        payload = {
            "username": self._username,
            "password": self._password,
            "verify": verify,
        }

        _LOGGER.debug("[LOGIN] Sending request: URL=%s, user=%s", URL_LOGIN, self._username)

        try:
            async with self._session.post(
                URL_LOGIN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[LOGIN] Response: Status=%s", resp.status)

                if resp.status == 200:
                    data = json.loads(response_text)
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "[LOGIN] Data received: type=%s, keys=%s",
                            type(data).__name__,
                            list(data.keys()) if isinstance(data, dict) else "N/A",
                        )
                    self._apply_token_data(data)
                    _LOGGER.debug("[LOGIN] Token obtained successfully (expires_in=%s).", self._expires_in)
                    return True

                # ── MFA required: HTTP 400 with code "6054" ──
                if resp.status == 400:
                    try:
                        data = json.loads(response_text)
                    except (json.JSONDecodeError, ValueError):
                        data = {}

                    if str(data.get("code")) == MFA_REQUIRED_CODE:
                        self._mfa_data = {
                            "uuid": data.get("description"),  # UUID sesiune MFA
                            "type": data.get("secondFactorType", "EMAIL"),
                            "alternative_type": data.get("secondFactorAlternativeType", "SMS"),
                            "recipient": data.get("secondFactorRecipient", ""),
                            "validity": data.get("secondFactorValidity", 60),
                        }
                        _LOGGER.warning(
                            "[LOGIN] MFA required (2FA active). Type=%s, Recipient=%s, Validity=%ss.",
                            self._mfa_data["type"],
                            self._mfa_data["recipient"],
                            self._mfa_data["validity"],
                        )
                        return False  # No token, but MFA is available

                _LOGGER.error(
                    "[LOGIN] Authentication error. HTTP code=%s, Response=%s",
                    resp.status,
                    response_text,
                )
                self._invalidate_tokens()
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[LOGIN] Timeout.")
            self._invalidate_tokens()
            return False
        except Exception as e:
            _LOGGER.error("[LOGIN] Error: %s", e)
            self._invalidate_tokens()
            return False

    async def async_mfa_complete(self, code: str) -> bool:
        """Complete MFA authentication with the received OTP code.

        Sends the code to second-factor-auth/mobile-login.
        Returns True if the token was obtained.
        """
        if not self._mfa_data or not self._mfa_data.get("uuid"):
            _LOGGER.error("[MFA] No active MFA session (uuid missing).")
            return False

        payload = {
            "uuid": self._mfa_data["uuid"],
            "code": code,
            "interval": None,
            "type": None,
        }

        _LOGGER.debug("[MFA] Completing 2FA login: URL=%s", URL_MFA_LOGIN)

        try:
            async with self._session.post(
                URL_MFA_LOGIN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[MFA] Response: Status=%s", resp.status)

                if resp.status == 200:
                    data = json.loads(response_text)
                    access_token = data.get("access_token")
                    if access_token:
                        self._apply_token_data(data)
                        self._mfa_data = None  # MFA completed successfully
                        _LOGGER.debug("[MFA] 2FA login successful (expires_in=%s).", self._expires_in)
                        return True

                _LOGGER.error(
                    "[MFA] 2FA authentication failed. HTTP code=%s, Response=%s",
                    resp.status,
                    response_text,
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[MFA] Timeout.")
            return False
        except Exception as e:
            _LOGGER.error("[MFA] Error: %s", e)
            return False

    async def async_mfa_resend(self, mfa_type: str | None = None) -> bool:
        """Resend the MFA code on the specified channel.

        Args:
            mfa_type: "SMS" or "EMAIL". If None, uses the current type.

        Returns True if the code was resent successfully.
        Updates the MFA session UUID if the server returns a new one.
        """
        if not self._mfa_data or not self._mfa_data.get("uuid"):
            _LOGGER.error("[MFA-RESEND] No active MFA session.")
            return False

        send_type = mfa_type or self._mfa_data.get("type", "EMAIL")

        payload = {
            "uuid": self._mfa_data["uuid"],
            "secondFactorValidity": None,
            "type": send_type,
            "action": "AUTHORIZATION",
            "recipient": None,
        }

        _LOGGER.debug("[MFA-RESEND] Resending code (%s): URL=%s", send_type, URL_MFA_RESEND)

        try:
            async with self._session.post(
                URL_MFA_RESEND, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[MFA-RESEND] Response: Status=%s, Body=%s", resp.status, response_text)

                if resp.status == 200:
                    try:
                        data = json.loads(response_text)
                    except (json.JSONDecodeError, ValueError):
                        data = {}
                    # Update UUID if server sends a new one
                    new_uuid = data.get("uuid")
                    if new_uuid:
                        self._mfa_data["uuid"] = new_uuid
                    new_recipient = data.get("recipient")
                    if new_recipient:
                        self._mfa_data["recipient"] = new_recipient
                    _LOGGER.debug("[MFA-RESEND] Code resent successfully (%s).", send_type)
                    return True

                _LOGGER.error(
                    "[MFA-RESEND] Resend failed. HTTP code=%s, Response=%s",
                    resp.status,
                    response_text,
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[MFA-RESEND] Timeout.")
            return False
        except Exception as e:
            _LOGGER.error("[MFA-RESEND] Error: %s", e)
            return False

    async def async_refresh_token(self) -> bool:
        """Refresh the access token using refresh_token (without lock — called from _ensure_token_valid)."""
        if not self._refresh_token:
            _LOGGER.debug("[REFRESH] No refresh_token available. Will perform full login.")
            return False

        payload = {"refreshToken": self._refresh_token}

        _LOGGER.debug("[REFRESH] Sending request: URL=%s", URL_REFRESH_TOKEN)

        try:
            async with self._session.post(
                URL_REFRESH_TOKEN, json=payload, headers=HEADERS, timeout=self._timeout
            ) as resp:
                _LOGGER.debug("[REFRESH] Response: Status=%s", resp.status)

                if resp.status == 200:
                    data = await resp.json()
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "[REFRESH] Data received: type=%s, keys=%s",
                            type(data).__name__,
                            list(data.keys()) if isinstance(data, dict) else "N/A",
                        )
                    self._apply_token_data(data)
                    _LOGGER.debug("[REFRESH] Token refreshed successfully (expires_in=%s).", self._expires_in)
                    return True

                _LOGGER.warning(
                    "[REFRESH] Refresh error. HTTP code=%s, Response=%s",
                    resp.status,
                    response_text,
                )
                return False

        except asyncio.TimeoutError:
            _LOGGER.error("[REFRESH] Timeout.")
            return False
        except Exception as e:
            _LOGGER.error("[REFRESH] Error: %s", e)
            return False

    def _apply_token_data(self, data: dict) -> None:
        """Apply token data from API response (login or refresh)."""
        self._access_token = data.get("access_token")
        self._token_type = data.get("token_type", "Bearer")
        self._expires_in = data.get("expires_in", 3600)
        self._refresh_token = data.get("refresh_token")
        self._id_token = data.get("idToken")  # camelCase conform API real
        self._uuid = data.get("uuid")
        self._token_obtained_at = time.monotonic()
        self._token_generation += 1

    def invalidate_token(self) -> None:
        """Invalidate the current token (to force re-authentication)."""
        self._access_token = None
        self._token_obtained_at = 0.0

    def _invalidate_tokens(self) -> None:
        """Invalidate all tokens (access + refresh)."""
        self._access_token = None
        self._refresh_token = None
        self._id_token = None
        self._uuid = None
        self._token_obtained_at = 0.0

    async def async_ensure_authenticated(self) -> bool:
        """Public method for ensuring authentication (STAB-01).

        Public wrapper for _ensure_token_valid — used by coordinator
        instead of direct call to the private method.
        """
        return await self._ensure_token_valid()

    async def _ensure_token_valid(self) -> bool:
        """
        Ensure a valid token exists — refresh or full login.

        Thread-safe: uses _auth_lock to prevent concurrent refresh/login
        operations. When multiple parallel requests need a new token,
        only the first does refresh/login, the rest reuse the result.

        If MFA was previously detected in background (not in config_flow),
        login is no longer attempted — returns False immediately. This prevents
        MFA email flooding and repeated logins.
        """
        # Fast path without lock: token already valid
        if self.is_token_likely_valid():
            return True

        # Guard: if MFA is blocked, do not try anything
        if self._mfa_blocked:
            _LOGGER.debug("[AUTH] Login blocked — MFA required. Reconfigure integration from UI.")
            return False

        async with self._auth_lock:
            # Double-check after obtaining the lock:
            # another concurrent call may have already renewed the token
            if self.is_token_likely_valid():
                _LOGGER.debug("[AUTH] Token already available (obtained by another concurrent call).")
                return True

            # Double-check MFA block (another caller set it in the meantime)
            if self._mfa_blocked:
                _LOGGER.debug("[AUTH] Login blocked by another concurrent call — MFA required.")
                return False

            # Try refresh if we have refresh_token
            if self._refresh_token:
                if await self.async_refresh_token():
                    return True
                _LOGGER.debug("[AUTH] Refresh token failed. Attempting full login.")

            # Fallback to full login
            self._invalidate_tokens()
            result = await self.async_login()

            # If login required MFA, block any future attempt
            # until reconfiguration via UI (inject_token will reset the block)
            if not result and self._mfa_data is not None:
                self._mfa_blocked = True
                _LOGGER.error(
                    "[AUTH] ══════════════════════════════════════════════════════════════"
                )
                _LOGGER.error(
                    "[AUTH] MFA REQUIRED — Automatic login cannot continue."
                )
                _LOGGER.error(
                    "[AUTH] Reconfigure the E-ON Energy integration from:"
                )
                _LOGGER.error(
                    "[AUTH]   Settings → Devices & services → E-ON Energy → Reconfigure"
                )
                _LOGGER.error(
                    "[AUTH] No more MFA emails will be sent until reconfiguration."
                )
                _LOGGER.error(
                    "[AUTH] ══════════════════════════════════════════════════════════════"
                )

            return result

    # ──────────────────────────────────────────
    # User data
    # ──────────────────────────────────────────

    async def async_fetch_user_details(self):
        """Fetch authenticated user personal data (user-details)."""
        result = await self._request_with_token(
            method="GET",
            url=URL_USER_DETAILS,
            label="user_details",
        )
        _LOGGER.debug(
            "[user_details] Data received: type=%s, keys=%s",
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
        )
        return result

    # ──────────────────────────────────────────
    # Contracts
    # ──────────────────────────────────────────

    async def async_fetch_contracts_list(self, partner_code: str | None = None, collective_contract: str | None = None, limit: int | None = None):
        """Fetch the contracts list for a partner."""
        params = {}
        if partner_code:
            params["partnerCode"] = partner_code
        if collective_contract:
            params["collectiveContract"] = collective_contract
        if limit is not None:
            params["limit"] = str(limit)
        url = URL_CONTRACTS_LIST
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        result = await self._request_with_token(
            method="GET",
            url=url,
            label="contracts_list",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[contracts_list] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_contract_details(self, account_contract: str, include_meter_reading: bool = True):
        """Fetch details for a specific contract."""
        url = URL_CONTRACT_DETAILS.format(accountContract=account_contract)
        if include_meter_reading:
            url = f"{url}?includeMeterReading=true"
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=f"contract_details ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[contract_details %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_contracts_with_subcontracts(self, account_contract: str | None = None):
        """Fetch contracts list with subcontracts (for collective/DUO contracts).

        Calls WITHOUT parameter (returns all contracts with subcontracts
        of the authenticated user). If account_contract is specified,
        results are filtered locally afterwards.
        """
        # Call without filter — API returns all contracts with subcontracts
        url = URL_CONTRACTS_WITH_SUBCONTRACTS
        label = f"contracts_with_subcontracts ({account_contract or 'all'})"
        _LOGGER.debug("[%s] URL complet: %s", label, url)
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=label,
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[%s] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            label,
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_contracts_details_list(self, account_contracts: list[str]):
        """Fetch details for multiple contracts simultaneously (DUO subcontracts).

        Request body: ContractDetailsRequest — object with accountContracts[] + includeMeterReading.
        Response: List<ElectronicInvoiceStatusResponse>
        """
        if not account_contracts:
            return None
        payload = {
            "accountContracts": account_contracts,
            "includeMeterReading": True,
        }
        label = f"contracts_details_list ({len(account_contracts)} subcontracte)"
        result = await self._request_with_token_post(
            url=URL_CONTRACTS_DETAILS_LIST,
            payload=payload,
            label=label,
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[%s] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            label,
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    # ──────────────────────────────────────────
    # Invoices & Payments
    # ──────────────────────────────────────────

    async def async_fetch_invoices_unpaid(self, account_contract: str, include_subcontracts: bool = False):
        """Fetch unpaid invoices."""
        params = f"?accountContract={account_contract}&status=unpaid"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        result = await self._request_with_token(
            method="GET",
            url=f"{URL_INVOICES_UNPAID}{params}",
            label=f"invoices_unpaid ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[invoices_unpaid %s] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            account_contract,
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_invoices_prosum(self, account_contract: str, max_pages: int | None = None):
        """Fetch prosumer invoices (paginated)."""
        result = await self._paginated_request(
            base_url=URL_INVOICES_PROSUM,
            params={"accountContract": account_contract},
            list_key="list",
            label=f"invoices_prosum ({account_contract})",
            max_pages=max_pages,
        )
        # Clear debug for accumulated data
        _LOGGER.debug(
            "[invoices_prosum %s] Data accumulated: type=%s, len=%s, sample keys=%s, sample content=%s",
            account_contract,
            type(result).__name__,
            len(result) if isinstance(result, list) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_invoice_balance(self, account_contract: str, include_subcontracts: bool = False):
        """Fetch invoice balance."""
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        result = await self._request_with_token(
            method="GET",
            url=f"{URL_INVOICE_BALANCE}{params}",
            label=f"invoice_balance ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[invoice_balance %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_invoice_balance_prosum(self, account_contract: str, include_subcontracts: bool = False):
        """Fetch prosumer invoice balance."""
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        result = await self._request_with_token(
            method="GET",
            url=f"{URL_INVOICE_BALANCE_PROSUM}{params}",
            label=f"invoice_balance_prosum ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[invoice_balance_prosum %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_payments(self, account_contract: str, max_pages: int | None = None):
        """Fetch payment records (paginated)."""
        result = await self._paginated_request(
            base_url=URL_PAYMENT_LIST,
            params={"accountContract": account_contract},
            list_key="list",
            label=f"payments ({account_contract})",
            max_pages=max_pages,
        )
        # Clear debug for accumulated data
        _LOGGER.debug(
            "[payments %s] Data accumulated: type=%s, len=%s, sample keys=%s, sample content=%s",
            account_contract,
            type(result).__name__,
            len(result) if isinstance(result, list) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_rescheduling_plans(self, account_contract: str, include_subcontracts: bool = False, status: str | None = None):
        """Fetch rescheduling plans."""
        params = f"?accountContract={account_contract}"
        if include_subcontracts:
            params += "&includeSubcontracts=true"
        if status:
            params += f"&status={status}"
        result = await self._request_with_token(
            method="GET",
            url=f"{URL_RESCHEDULING_PLANS}{params}",
            label=f"rescheduling_plans ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[rescheduling_plans %s] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            account_contract,
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_graphic_consumption(self, account_contract: str):
        """Fetch billed consumption chart data."""
        url = URL_GRAPHIC_CONSUMPTION.format(accountContract=account_contract)
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=f"graphic_consumption ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[graphic_consumption %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    # ──────────────────────────────────────────
    # Meter Readings & Conventions
    # ──────────────────────────────────────────

    async def async_fetch_meter_index(self, account_contract: str):
        """Fetch current meter index data."""
        url = URL_METER_INDEX.format(accountContract=account_contract)
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=f"meter_index ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[meter_index %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_meter_history(self, account_contract: str):
        """Fetch meter reading history."""
        url = URL_METER_HISTORY.format(accountContract=account_contract)
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=f"meter_history ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[meter_history %s] Data received: type=%s, len=%s, sample keys=%s, sample content=%s",
            account_contract,
            type(result).__name__,
            len(result) if isinstance(result, (list, dict)) else "N/A",
            list(result[0].keys()) if isinstance(result, list) and result else list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_fetch_consumption_convention(self, account_contract: str):
        """Fetch current consumption convention."""
        url = URL_CONSUMPTION_CONVENTION.format(accountContract=account_contract)
        result = await self._request_with_token(
            method="GET",
            url=url,
            label=f"consumption_convention ({account_contract})",
        )
        # Clear debug for received data
        _LOGGER.debug(
            "[consumption_convention %s] Data received: type=%s, keys=%s, sample=%s",
            account_contract,
            type(result).__name__,
            list(result.keys()) if isinstance(result, dict) else "N/A",
            json.dumps(result, default=str)[:500] if result else "None"
        )
        return result

    async def async_submit_meter_index(
        self, account_contract: str, indexes: list[dict]
    ):
        """Submit meter index to the E-ON API."""
        label = f"submit_meter ({account_contract})"

        if not account_contract or not indexes:
            _LOGGER.error("[%s] Invalid parameters for index submission.", label)
            return None

        payload = {
            "accountContract": account_contract,
            "channel": "MOBILE",
            "indexes": indexes,
        }

        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Invalid token. Submission cannot be performed.", label)
            return None

        gen_before = self._token_generation
        headers = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}

        _LOGGER.debug("[%s] Sending request: URL=%s, Payload=%s", label, URL_METER_SUBMIT, json.dumps(payload))

        try:
            async with self._session.post(
                URL_METER_SUBMIT,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                response_text = await resp.text()
                _LOGGER.debug("[%s] Response: Status=%s, Body=%s", label, resp.status, response_text)

                if resp.status == 200:
                    data = await resp.json()
                    # Clear debug for received data on submit
                    _LOGGER.debug(
                        "[%s] Data received: type=%s, keys=%s, sample=%s",
                        label,
                        type(data).__name__,
                        list(data.keys()) if isinstance(data, dict) else "N/A",
                        json.dumps(data, default=str)[:500] if data else "None"
                    )
                    _LOGGER.debug("[%s] Index submitted successfully.", label)
                    return data

                if resp.status == 401:
                    # Check if another call already renewed the token
                    if self._token_generation != gen_before:
                        _LOGGER.debug("[%s] Token renewed by another call. Retrying.", label)
                    else:
                        _LOGGER.warning("[%s] Invalid token (401). Retrying...", label)
                        self.invalidate_token()
                        if not await self._ensure_token_valid():
                            _LOGGER.error("[%s] Re-authentication failed.", label)
                            return None

                    headers_retry = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}
                    async with self._session.post(
                        URL_METER_SUBMIT,
                        json=payload,
                        headers=headers_retry,
                        timeout=self._timeout,
                    ) as resp_retry:
                        response_text_retry = await resp_retry.text()
                        _LOGGER.debug("[%s] Retry: Status=%s, Body=%s", label, resp_retry.status, response_text_retry)
                        if resp_retry.status == 200:
                            data_retry = await resp_retry.json()
                            # Clear debug for received data on retry
                            _LOGGER.debug(
                                "[%s] Data received (retry): type=%s, keys=%s, sample=%s",
                                label,
                                type(data_retry).__name__,
                                list(data_retry.keys()) if isinstance(data_retry, dict) else "N/A",
                                json.dumps(data_retry, default=str)[:500] if data_retry else "None"
                            )
                            _LOGGER.debug("[%s] Index submitted successfully (after re-authentication).", label)
                            return data_retry
                        _LOGGER.error("[%s] Retry failed. HTTP code=%s", label, resp_retry.status)
                        return None

                _LOGGER.error("[%s] Error. HTTP code=%s, Response=%s", label, resp.status, response_text)
                return None

        except asyncio.TimeoutError:
            _LOGGER.error("[%s] Timeout.", label)
            return None
        except Exception as e:
            _LOGGER.exception("[%s] Error: %s", label, e)
            return None

    # ──────────────────────────────────────────
    # Internal methods
    # ──────────────────────────────────────────

    async def _request_with_token(self, method: str, url: str, label: str = "request"):
        """
        Request with automatic token management.

        1. Ensure valid token (protected by _auth_lock)
        2. Execute the request
        3. On 401: check if another call already renewed the token, otherwise refresh/login + retry
        """
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Could not obtain a valid token.", label)
            return None

        # Remember token generation before request
        gen_before = self._token_generation

        # First attempt
        resp_data, status = await self._do_request(method, url, label)
        if status != 401:
            return resp_data

        # 401 → check if another concurrent call already renewed the token
        if self._token_generation != gen_before:
            _LOGGER.debug("[%s] HTTP 401, but token already renewed (gen %s→%s). Retrying.", label, gen_before, self._token_generation)
        else:
            # Token was not renewed — force refresh/login
            _LOGGER.warning("[%s] HTTP 401 → retrying with refresh token.", label)
            self.invalidate_token()
            if not await self._ensure_token_valid():
                _LOGGER.error("[%s] Re-authentication failed.", label)
                return None

        # Second attempt
        resp_data, status = await self._do_request(method, url, label)
        if status == 401:
            _LOGGER.error("[%s] Second attempt failed (401). Giving up.", label)
            return None

        return resp_data

    async def _request_with_token_post(self, url: str, payload, label: str = "request_post"):
        """
        POST request with JSON body and automatic token management.

        Similar to _request_with_token, but sends JSON payload.
        """
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Could not obtain a valid token.", label)
            return None

        gen_before = self._token_generation

        # First attempt
        resp_data, status = await self._do_request("POST", url, label, json_payload=payload)
        if status != 401:
            return resp_data

        # 401 → check if another concurrent call already renewed the token
        if self._token_generation != gen_before:
            _LOGGER.debug("[%s] HTTP 401, but token already renewed (gen %s→%s). Retrying.", label, gen_before, self._token_generation)
        else:
            _LOGGER.warning("[%s] HTTP 401 → retrying with refresh token.", label)
            self.invalidate_token()
            if not await self._ensure_token_valid():
                _LOGGER.error("[%s] Re-authentication failed.", label)
                return None

        # Second attempt
        resp_data, status = await self._do_request("POST", url, label, json_payload=payload)
        if status == 401:
            _LOGGER.error("[%s] Second attempt failed (401). Giving up.", label)
            return None

        return resp_data

    async def _do_request(self, method: str, url: str, label: str = "request", json_payload=None):
        """Perform an HTTP request with the current token. Returns (data, status)."""
        headers = {**HEADERS}
        if self._access_token:
            headers["Authorization"] = f"{self._token_type} {self._access_token}"

        _LOGGER.debug("[%s] %s %s, Payload=%s", label, method, url, json.dumps(json_payload) if json_payload else "None")

        try:
            kwargs = {"headers": headers, "timeout": self._timeout}
            if json_payload is not None:
                kwargs["json"] = json_payload

            async with self._session.request(method, url, **kwargs) as resp:
                response_text = await resp.text()

                if resp.status == 200:
                    data = await resp.json()
                    # Clear debug for received data in _do_request
                    _LOGGER.debug(
                        "[%s] Response OK (200). Size: %s chars. JSON data: type=%s, len=%s, sample keys=%s, sample content=%s",
                        label,
                        len(response_text),
                        type(data).__name__,
                        len(data) if isinstance(data, (list, dict)) else "N/A",
                        list(data[0].keys()) if isinstance(data, list) and data else list(data.keys()) if isinstance(data, dict) else "N/A",
                        json.dumps(data, default=str)[:500] if data else "None"
                    )
                    return data, resp.status

                _LOGGER.error("[%s] Error: %s %s → HTTP code=%s, Response=%s", label, method, url, resp.status, response_text)
                return None, resp.status

        except asyncio.TimeoutError:
            _LOGGER.error("[%s] Timeout: %s %s.", label, method, url)
            return None, 0
        except Exception as e:
            _LOGGER.error("[%s] Error: %s %s → %s", label, method, url, e)
            return None, 0

    async def _paginated_request(
        self,
        base_url: str,
        params: dict,
        list_key: str = "list",
        label: str = "paginated",
        max_pages: int | None = None,
    ):
        """Fetch pages from a paginated endpoint. Returns accumulated list.

        Args:
            max_pages: Maximum number of pages to fetch. None = all pages.
        """
        if not await self._ensure_token_valid():
            _LOGGER.error("[%s] Could not obtain a valid token.", label)
            return None

        results: list = []
        page = 1
        retried = False

        while True:
            query_parts = [f"{k}={v}" for k, v in params.items()]
            query_parts.append(f"page={page}")
            url = f"{base_url}?{'&'.join(query_parts)}"

            gen_before = self._token_generation
            headers = {**HEADERS, "Authorization": f"{self._token_type} {self._access_token}"}

            _LOGGER.debug("[%s] Page %s: %s", label, page, url)

            try:
                async with self._session.get(
                    url, headers=headers, timeout=self._timeout
                ) as resp:
                    response_text = await resp.text()

                    if resp.status == 200:
                        data = await resp.json()
                        # Clear debug for received data per page
                        _LOGGER.debug(
                            "[%s] Page %s: JSON data: type=%s, keys=%s, list len=%s, sample list keys=%s, sample content=%s",
                            label, page,
                            type(data).__name__,
                            list(data.keys()) if isinstance(data, dict) else "N/A",
                            len(data.get(list_key, [])),
                            list(data.get(list_key, [])[0].keys()) if data.get(list_key) and isinstance(data.get(list_key), list) and data.get(list_key) else "N/A",
                            json.dumps(data, default=str)[:500] if data else "None"
                        )
                        chunk = data.get(list_key, [])
                        results.extend(chunk)
                        retried = False

                        has_next = data.get("hasNext", False)
                        _LOGGER.debug(
                            "[%s] Page %s: %s items, has_next=%s.",
                            label, page, len(chunk), has_next,
                        )

                        if not has_next:
                            break
                        if max_pages is not None and page >= max_pages:
                            _LOGGER.debug("[%s] Pagination limit reached (%s pages).", label, max_pages)
                            break
                        page += 1
                        continue

                    if resp.status == 401 and not retried:
                        # Check if another call already renewed the token
                        if self._token_generation != gen_before:
                            _LOGGER.debug("[%s] Token renewed by another call (page %s). Retrying.", label, page)
                        else:
                            _LOGGER.warning("[%s] Token expired (page %s). Retrying...", label, page)
                            self.invalidate_token()
                            if not await self._ensure_token_valid():
                                return results if results else None
                        retried = True
                        continue

                    _LOGGER.error("[%s] Error: HTTP code=%s (page %s), Response=%s", label, resp.status, page, response_text)
                    break

            except asyncio.TimeoutError:
                _LOGGER.error("[%s] Timeout (page %s).", label, page)
                break
            except Exception as e:
                _LOGGER.error("[%s] Error: %s", label, e)
                break

        _LOGGER.debug("[%s] Total: %s items from %s pages.", label, len(results), page)
        return results
