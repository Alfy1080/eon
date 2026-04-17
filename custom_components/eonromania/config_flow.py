"""
ConfigFlow and OptionsFlow for the E-ON Energy integration.

The user enters email + password, then selects the desired contracts.
Contracts are discovered automatically via account-contracts/list.
Supports MFA (Two-Factor Authentication) — if enabled, an OTP code is requested.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AUTO_RELOAD,
    CONF_AUTO_RELOAD_INTERVAL,
    DEFAULT_AUTO_RELOAD_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DOMAIN_TOKEN_STORE,
)
from .api import EonApiClient
from .helpers import (
    build_contract_metadata,
    build_contract_options,
    mask_email,
    resolve_selection,
)

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Common helper: fetch contracts after successful authentication
# ------------------------------------------------------------------

async def _fetch_contracts_after_login(api: EonApiClient) -> list[dict] | None:
    """Fetch the contracts list after successful authentication.

    Returns the list of contracts or None if none were found.
    """
    contracts = await api.async_fetch_contracts_list()
    if contracts and isinstance(contracts, list) and len(contracts) > 0:
        return contracts
    return None


def _store_token(hass, username: str, api: EonApiClient) -> None:
    """Save the API token in hass.data for pickup by __init__.py.

    The token is saved per username (multiple accounts may exist).
    """
    token_data = api.export_token_data()
    if token_data is None:
        return
    store = hass.data.setdefault(DOMAIN_TOKEN_STORE, {})
    store[username.lower()] = token_data
    _LOGGER.debug(
        "Token saved in hass.data for %s (access=%s...).",
        username,
        token_data["access_token"][:8] if token_data.get("access_token") else "None",
    )


# ------------------------------------------------------------------
# ConfigFlow
# ------------------------------------------------------------------

class EonRomaniaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """ConfigFlow — authentication + MFA (optional) + contract selection."""

    VERSION = 3

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._update_interval: int = DEFAULT_UPDATE_INTERVAL
        self._contracts_raw: list[dict] = []
        self._api: EonApiClient | None = None
        # MFA state — saved when entering the MFA step, persistent after async_mfa_complete
        self._mfa_type: str = ""
        self._mfa_alt_type: str = ""
        self._mfa_recipient_display: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input["username"]
            self._password = user_input["password"]
            self._update_interval = user_input.get(
                "update_interval", DEFAULT_UPDATE_INTERVAL
            )

            await self.async_set_unique_id(self._username.lower())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            self._api = EonApiClient(session, self._username, self._password)

            if await self._api.async_login():
                # Login successful without MFA — save token and fetch contracts
                _store_token(self.hass, self._username, self._api)
                contracts = await _fetch_contracts_after_login(self._api)
                if contracts:
                    self._contracts_raw = contracts
                    return await self.async_step_select_contracts()
                # No contracts found — create entry without contracts (personal data only)
                _LOGGER.info(
                    "No contracts found for %s. Creating entry with personal data only.",
                    self._username,
                )
                return self._create_entry_no_contracts()
            elif self._api.mfa_required:
                # MFA required — save type and recipient NOW (before async_mfa_complete clears them)
                mfa_info = self._api.mfa_data or {}
                self._mfa_type = mfa_info.get("type", "EMAIL")
                self._mfa_alt_type = mfa_info.get("alternative_type", "")
                if self._mfa_type == "EMAIL":
                    self._mfa_recipient_display = mask_email(self._username)
                else:
                    self._mfa_recipient_display = mfa_info.get("recipient", "—")
                _LOGGER.debug(
                    "MFA required for %s. Type=%s, Alt=%s, Recipient=%s.",
                    self._username,
                    self._mfa_type,
                    self._mfa_alt_type,
                    self._mfa_recipient_display,
                )
                # If an alternative channel exists (phone set up) → let user choose
                if self._mfa_alt_type and self._mfa_alt_type != self._mfa_type:
                    return await self.async_step_mfa_method()
                return await self.async_step_mfa()
            else:
                errors["base"] = "auth_failed"

        schema = vol.Schema(
            {
                vol.Required("username"): str,
                vol.Required("password"): str,
                vol.Optional(
                    "update_interval", default=DEFAULT_UPDATE_INTERVAL
                ): vol.All(int, vol.Range(min=21600)),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_mfa_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1a: MFA channel selection (EMAIL or SMS).

        Shown only if the account has a phone number set (alternative_type available).
        If the user chooses the alternative channel, the code is resent on the new channel.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            chosen = user_input.get("mfa_method", self._mfa_type)

            if chosen != self._mfa_type:
                # User chose the alternative channel → resend code on new channel
                _LOGGER.debug(
                    "MFA: User chose alternative channel %s (default was %s). Resending code.",
                    chosen,
                    self._mfa_type,
                )
                if self._api and await self._api.async_mfa_resend(chosen):
                    self._mfa_type = chosen
                    # Update recipient from mfa_data (resend may return new recipient)
                    mfa_info = self._api.mfa_data or {}
                    if chosen == "EMAIL":
                        self._mfa_recipient_display = mask_email(self._username)
                    else:
                        self._mfa_recipient_display = mfa_info.get("recipient", "—")
                    _LOGGER.debug(
                        "MFA: Code resent via %s to %s.",
                        chosen,
                        self._mfa_recipient_display,
                    )
                else:
                    errors["base"] = "mfa_resend_failed"
                    _LOGGER.warning("MFA: Code resend via %s failed.", chosen)

            if not errors:
                return await self.async_step_mfa()

        # Build selection options
        # NOTE: mfa_data['recipient'] contains the recipient of the DEFAULT method (EMAIL → masked email).
        # The phone number is NOT available in the login response — it only appears after resending via SMS.
        # Therefore, for the alternative method we show only the channel type, without an address.
        mfa_info = (self._api.mfa_data or {}) if self._api else {}

        def _build_mfa_label(method_type: str, is_current: bool) -> str:
            """Build the label for an MFA method.

            is_current=True: the method on which the code was already sent (we have the recipient).
            is_current=False: the alternative method (we do NOT have the real recipient).
            """
            if method_type == "EMAIL":
                return f"Email ({mask_email(self._username)})"
            # SMS
            if is_current:
                # Code was already sent via SMS → recipient contains the phone number
                return f"SMS ({mfa_info.get('recipient', 'phone')})"
            # Alternative SMS — we don't have the phone number yet
            return "SMS (phone)"

        current_label = _build_mfa_label(self._mfa_type, is_current=True)
        alt_label = _build_mfa_label(self._mfa_alt_type, is_current=False)

        options_list = [
            {"value": self._mfa_type, "label": current_label},
            {"value": self._mfa_alt_type, "label": alt_label},
        ]

        schema = vol.Schema(
            {
                vol.Required("mfa_method", default=self._mfa_type): SelectSelector(
                    SelectSelectorConfig(
                        options=options_list,
                        multiple=False,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="mfa_method",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1b: Enter MFA code (Two-Factor Authentication)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("code", "").strip()

            if not code:
                errors["base"] = "mfa_invalid_code"
            elif self._api and await self._api.async_mfa_complete(code):
                # MFA completed — save token and fetch contracts
                _store_token(self.hass, self._username, self._api)
                contracts = await _fetch_contracts_after_login(self._api)
                if contracts:
                    self._contracts_raw = contracts
                    return await self.async_step_select_contracts()
                # No contracts found — create entry without contracts (personal data only)
                _LOGGER.info(
                    "No contracts found for %s (after MFA). Creating entry with personal data only.",
                    self._username,
                )
                return self._create_entry_no_contracts()
            else:
                errors["base"] = "mfa_failed"

        # Placeholders from instance variables (set when entering MFA, persistent)
        placeholders = {
            "mfa_type": "email" if self._mfa_type == "EMAIL" else "SMS",
            "mfa_recipient": self._mfa_recipient_display or "—",
        }

        schema = vol.Schema(
            {
                vol.Required("code"): str,
            }
        )

        return self.async_show_form(
            step_id="mfa",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_select_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Select contracts from list."""
        errors: dict[str, str] = {}

        if user_input is not None:
            select_all = user_input.get("select_all", False)
            selected = user_input.get("selected_contracts", [])

            if not select_all and not selected:
                errors["base"] = "no_contract_selected"
            else:
                final_selection = resolve_selection(
                    select_all, selected, self._contracts_raw
                )

                return self.async_create_entry(
                    title=f"E-ON Energy ({mask_email(self._username)})",
                    data={
                        "username": self._username,
                        "password": self._password,
                        "update_interval": self._update_interval,
                        "select_all": select_all,
                        "selected_contracts": final_selection,
                        "contract_metadata": build_contract_metadata(self._contracts_raw),
                    },
                )

        contract_options = build_contract_options(self._contracts_raw)

        schema = vol.Schema(
            {
                vol.Optional("select_all", default=False): bool,
                vol.Required(
                    "selected_contracts", default=[]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=contract_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_contracts",
            data_schema=schema,
            errors=errors,
        )

    def _create_entry_no_contracts(self) -> ConfigFlowResult:
        """Create entry without contracts (personal data sensor only).

        Used when the account is valid but has no associated contracts.
        """
        return self.async_create_entry(
            title=f"E-ON Energy ({mask_email(self._username)})",
            data={
                "username": self._username,
                "password": self._password,
                "update_interval": self._update_interval,
                "select_all": False,
                "selected_contracts": [],
                "contract_metadata": {},
                "token_data": self._api.export_token_data() if self._api else None,
                "account_only": True,  # Flag: account without contracts
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EonRomaniaOptionsFlow:
        return EonRomaniaOptionsFlow()


# ------------------------------------------------------------------
# OptionsFlow
# ------------------------------------------------------------------

class EonRomaniaOptionsFlow(config_entries.OptionsFlow):
    """OptionsFlow — modify settings + contract selection."""

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._update_interval: int = DEFAULT_UPDATE_INTERVAL
        self._contracts_raw: list[dict] = []
        self._api: EonApiClient | None = None
        # MFA state — saved when entering the MFA step, persistent after async_mfa_complete
        self._mfa_type: str = ""
        self._mfa_alt_type: str = ""
        self._mfa_recipient_display: str = ""
        self._auto_reload: bool = False
        self._auto_reload_interval: int = DEFAULT_AUTO_RELOAD_INTERVAL

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the main menu with available options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "settings",
                "auto_reload",
            ],
        )

    async def async_step_auto_reload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Form for auto-reload settings."""
        if user_input is not None:
            new_data = dict(self.config_entry.data)
            new_data.update({
                CONF_AUTO_RELOAD: user_input.get(CONF_AUTO_RELOAD, False),
                CONF_AUTO_RELOAD_INTERVAL: user_input.get(
                    CONF_AUTO_RELOAD_INTERVAL, DEFAULT_AUTO_RELOAD_INTERVAL
                ),
            })
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AUTO_RELOAD,
                    default=current.get(CONF_AUTO_RELOAD, False),
                ): bool,
                vol.Optional(
                    CONF_AUTO_RELOAD_INTERVAL,
                    default=current.get(
                        CONF_AUTO_RELOAD_INTERVAL, DEFAULT_AUTO_RELOAD_INTERVAL
                    ),
                ): vol.All(int, vol.Range(min=5)),
            }
        )

        return self.async_show_form(
            step_id="auto_reload",
            data_schema=schema,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Modify credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input["username"]
            password = user_input["password"]
            update_interval = user_input.get(
                "update_interval", DEFAULT_UPDATE_INTERVAL
            )
            self._auto_reload = self.config_entry.data.get(CONF_AUTO_RELOAD, False)
            self._auto_reload_interval = self.config_entry.data.get(CONF_AUTO_RELOAD_INTERVAL, DEFAULT_AUTO_RELOAD_INTERVAL)

            session = async_get_clientsession(self.hass)
            self._api = EonApiClient(session, username, password)

            if await self._api.async_login():
                _store_token(self.hass, username, self._api)
                contracts = await _fetch_contracts_after_login(self._api)
                if contracts:
                    self._contracts_raw = contracts
                    self._username = username
                    self._password = password
                    self._update_interval = update_interval
                    return await self.async_step_select_contracts()
                # No contracts found — save as account_only
                _LOGGER.info(
                    "No contracts found (options) for %s. Saving as account_only.",
                    username,
                )
                new_data = dict(self.config_entry.data)
                new_data.update({
                    "username": username,
                    "password": password,
                    "update_interval": update_interval,
                    "selected_contracts": [],
                    "contract_metadata": {},
                    "account_only": True,
                    CONF_AUTO_RELOAD: self._auto_reload,
                    CONF_AUTO_RELOAD_INTERVAL: self._auto_reload_interval,
                    "token_data": self._api.export_token_data() if self._api else None,
                })
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return self.async_create_entry(title="", data={})
            elif self._api.mfa_required:
                # MFA required — save credentials + MFA info NOW
                self._username = username
                self._password = password
                self._update_interval = update_interval
                mfa_info = self._api.mfa_data or {}
                self._mfa_type = mfa_info.get("type", "EMAIL")
                self._mfa_alt_type = mfa_info.get("alternative_type", "")
                if self._mfa_type == "EMAIL":
                    self._mfa_recipient_display = mask_email(username)
                else:
                    self._mfa_recipient_display = mfa_info.get("recipient", "—")
                # If an alternative channel exists (phone set up) → let user choose
                if self._mfa_alt_type and self._mfa_alt_type != self._mfa_type:
                    return await self.async_step_mfa_method()
                return await self.async_step_mfa()
            else:
                errors["base"] = "auth_failed"

        current = self.config_entry.data

        schema = vol.Schema(
            {
                vol.Required(
                    "username", default=current.get("username", "")
                ): str,
                vol.Required(
                    "password", default=current.get("password", "")
                ): str,
                vol.Required(
                    "update_interval",
                    default=current.get("update_interval", DEFAULT_UPDATE_INTERVAL),
                ): vol.All(int, vol.Range(min=21600)),
            }
        )

        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    async def async_step_mfa_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1a: MFA channel selection (EMAIL or SMS).

        Shown only if the account has a phone number set (alternative_type available).
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            chosen = user_input.get("mfa_method", self._mfa_type)

            if chosen != self._mfa_type:
                _LOGGER.debug(
                    "MFA: User chose alternative channel %s (default was %s). Resending code.",
                    chosen,
                    self._mfa_type,
                )
                if self._api and await self._api.async_mfa_resend(chosen):
                    self._mfa_type = chosen
                    mfa_info = self._api.mfa_data or {}
                    if chosen == "EMAIL":
                        self._mfa_recipient_display = mask_email(self._username)
                    else:
                        self._mfa_recipient_display = mfa_info.get("recipient", "—")
                else:
                    errors["base"] = "mfa_resend_failed"

            if not errors:
                return await self.async_step_mfa()

        # NOTE: mfa_data['recipient'] contains the recipient of the DEFAULT method.
        # The phone number is NOT available in the login response — it only appears after resending via SMS.
        mfa_info = (self._api.mfa_data or {}) if self._api else {}

        def _build_mfa_label(method_type: str, is_current: bool) -> str:
            if method_type == "EMAIL":
                return f"Email ({mask_email(self._username)})"
            if is_current:
                return f"SMS ({mfa_info.get('recipient', 'phone')})"
            return "SMS (phone)"

        current_label = _build_mfa_label(self._mfa_type, is_current=True)
        alt_label = _build_mfa_label(self._mfa_alt_type, is_current=False)

        options_list = [
            {"value": self._mfa_type, "label": current_label},
            {"value": self._mfa_alt_type, "label": alt_label},
        ]

        schema = vol.Schema(
            {
                vol.Required("mfa_method", default=self._mfa_type): SelectSelector(
                    SelectSelectorConfig(
                        options=options_list,
                        multiple=False,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="mfa_method",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1b: Enter MFA code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("code", "").strip()

            if not code:
                errors["base"] = "mfa_invalid_code"
            elif self._api and await self._api.async_mfa_complete(code):
                _store_token(self.hass, self._username, self._api)
                contracts = await _fetch_contracts_after_login(self._api)
                if contracts:
                    self._contracts_raw = contracts
                    return await self.async_step_select_contracts()
                # No contracts found — save as account_only
                _LOGGER.info(
                    "No contracts found (options MFA) for %s. Saving as account_only.",
                    self._username,
                )
                new_data = dict(self.config_entry.data)
                new_data.update({
                    "username": self._username,
                    "password": self._password,
                    "update_interval": self._update_interval,
                    "selected_contracts": [],
                    "contract_metadata": {},
                    "account_only": True,
                    CONF_AUTO_RELOAD: self._auto_reload,
                    CONF_AUTO_RELOAD_INTERVAL: self._auto_reload_interval,
                    "token_data": self._api.export_token_data() if self._api else None,
                })
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return self.async_create_entry(title="", data={})
            else:
                errors["base"] = "mfa_failed"

        # Placeholders from instance variables (set when entering MFA, persistent)
        placeholders = {
            "mfa_type": "email" if self._mfa_type == "EMAIL" else "SMS",
            "mfa_recipient": self._mfa_recipient_display or "—",
        }

        schema = vol.Schema(
            {
                vol.Required("code"): str,
            }
        )

        return self.async_show_form(
            step_id="mfa",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_select_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Modify contract selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            select_all = user_input.get("select_all", False)
            selected = user_input.get("selected_contracts", [])

            if not select_all and not selected:
                errors["base"] = "no_contract_selected"
            else:
                final_selection = resolve_selection(
                    select_all, selected, self._contracts_raw
                )

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        "username": self._username,
                        "password": self._password,
                        "update_interval": self._update_interval,
                        "select_all": select_all,
                        "selected_contracts": final_selection,
                        "contract_metadata": build_contract_metadata(self._contracts_raw),
                        CONF_AUTO_RELOAD: self._auto_reload,
                        CONF_AUTO_RELOAD_INTERVAL: self._auto_reload_interval,
                    },
                )

                await self.hass.config_entries.async_reload(
                    self.config_entry.entry_id
                )

                return self.async_create_entry(data={})

        current = self.config_entry.data

        schema = vol.Schema(
            {
                vol.Optional(
                    "select_all",
                    default=current.get("select_all", False),
                ): bool,
                vol.Required(
                    "selected_contracts",
                    default=current.get("selected_contracts", []),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=build_contract_options(self._contracts_raw),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_contracts",
            data_schema=schema,
            errors=errors,
        )
