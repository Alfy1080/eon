"""Constants for the E·ON Romania integration."""

from homeassistant.const import Platform

DOMAIN = "eonenergy"
DOMAIN_TOKEN_STORE = f"{DOMAIN}_token_store"  # Key in hass.data for MFA tokens

# ──────────────────────────────────────────────
# API versions (configurable)
# ──────────────────────────────────────────────
API_VERSION_USERS = "v1"
API_VERSION_PARTNERS = "v2"
API_VERSION_INVOICES = "v1"
API_VERSION_METERREADINGS = "v1"

# ──────────────────────────────────────────────
# Default configuration
# ──────────────────────────────────────────────
DEFAULT_UPDATE_INTERVAL = 21600  # Update interval in seconds (6 hours)

# ──────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────
SUBSCRIPTION_KEY = "e43698af63d84daa9763bbef7918378f"
AUTH_VERIFY_SECRET = "zrAnQjN0bDjlTsKYmbpexjaBNY6wrCzuIqGWNgqoaJzlLrYiqd"

# ──────────────────────────────────────────────
# Token management
# ──────────────────────────────────────────────
TOKEN_REFRESH_THRESHOLD = 300  # Refresh 5 min before expiration
TOKEN_MAX_AGE = 3300           # Fallback 55 min (if expires_in is missing)

# ──────────────────────────────────────────────
# Default timeout for API requests (seconds)
# ──────────────────────────────────────────────
API_TIMEOUT = 30

# ──────────────────────────────────────────────
# HTTP Headers
# ──────────────────────────────────────────────
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
    "User-Agent": "EON Myline/Android",
}

# ──────────────────────────────────────────────
# API URLs — Base URL
# ──────────────────────────────────────────────
API_BASE = "https://api2.eon.ro"

# ──────────────────────────────────────────────
# API URLs — Authentication
# ──────────────────────────────────────────────
URL_LOGIN = f"{API_BASE}/users/{API_VERSION_USERS}/userauth/mobile-login"
URL_REFRESH_TOKEN = f"{API_BASE}/users/{API_VERSION_USERS}/userauth/mobile-refresh-token"

# ──────────────────────────────────────────────
# API URLs — MFA (Two-Factor Authentication)
# ──────────────────────────────────────────────
URL_MFA_LOGIN = f"{API_BASE}/users/{API_VERSION_USERS}/second-factor-auth/mobile-login"
URL_MFA_RESEND = f"{API_BASE}/users/{API_VERSION_USERS}/second-factor-auth/resend-code"
URL_USER_DETAILS = f"{API_BASE}/users/{API_VERSION_USERS}/users/user-details"
MFA_REQUIRED_CODE = "6054"

# ──────────────────────────────────────────────
# API URLs — Partners & Contracts
# ──────────────────────────────────────────────
URL_CONTRACTS_LIST = f"{API_BASE}/partners/{API_VERSION_PARTNERS}/account-contracts/list"
URL_CONTRACTS_WITH_SUBCONTRACTS = f"{API_BASE}/partners/{API_VERSION_PARTNERS}/account-contracts/list-with-subcontracts"
URL_CONTRACTS_DETAILS_LIST = f"{API_BASE}/partners/{API_VERSION_PARTNERS}/account-contracts/contracts-details-list"
URL_CONTRACT_DETAILS = f"{API_BASE}/partners/{API_VERSION_PARTNERS}/account-contracts/{{accountContract}}"

# ──────────────────────────────────────────────
# API URLs — Invoices & Payments
# ──────────────────────────────────────────────
URL_INVOICES_UNPAID = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/invoices/list"
URL_INVOICES_PROSUM = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/invoices/list-prosum"
URL_INVOICE_BALANCE = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/invoices/invoice-balance"
URL_INVOICE_BALANCE_PROSUM = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/invoices/invoice-balance-prosum"
URL_PAYMENT_LIST = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/payments/payment-list"
URL_RESCHEDULING_PLANS = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/rescheduling-plans"
URL_GRAPHIC_CONSUMPTION = f"{API_BASE}/invoices/{API_VERSION_INVOICES}/invoices/graphic-consumption/{{accountContract}}"

# ──────────────────────────────────────────────
# API URLs — Meter Readings & Conventions
# ──────────────────────────────────────────────
URL_METER_INDEX = f"{API_BASE}/meterreadings/{API_VERSION_METERREADINGS}/meter-reading/{{accountContract}}/index"
URL_METER_SUBMIT = f"{API_BASE}/meterreadings/{API_VERSION_METERREADINGS}/meter-reading/index"
URL_METER_HISTORY = f"{API_BASE}/meterreadings/{API_VERSION_METERREADINGS}/meter-reading/{{accountContract}}/history"
URL_CONSUMPTION_CONVENTION = f"{API_BASE}/meterreadings/{API_VERSION_METERREADINGS}/consumption-convention/{{accountContract}}"

# ──────────────────────────────────────────────
# Supported platforms
# ──────────────────────────────────────────────
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# ──────────────────────────────────────────────
# Attribution
# ──────────────────────────────────────────────
ATTRIBUTION = "Data provided by E·ON Romania"

CONF_AUTO_RELOAD = "auto_reload_on_failure"
CONF_AUTO_RELOAD_INTERVAL = "auto_reload_interval"
DEFAULT_AUTO_RELOAD_INTERVAL = 30
