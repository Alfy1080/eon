"""Utility functions and constants for the E·ON Romania integration."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any

from homeassistant.helpers.selector import SelectOptionDict
from homeassistant.util import dt as dt_util


# ══════════════════════════════════════════════
# Month and reading type mappings
# ══════════════════════════════════════════════

MONTHS_EN_RO: dict[str, str] = {
    "January": "January",
    "February": "February",
    "March": "March",
    "April": "April",
    "May": "May",
    "June": "June",
    "July": "July",
    "August": "August",
    "September": "September",
    "October": "October",
    "November": "November",
    "December": "December",
}

MONTHS_NUM_RO: dict[int, str] = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

READING_TYPE_MAP: dict[str, str] = {
    "01": "distributor reading",
    "02": "self-reading",
    "03": "estimated",
}

# ══════════════════════════════════════════════
# County code mappings
# ══════════════════════════════════════════════
COUNTY_CODE_MAP: dict[str, str] = {
    "AB": "Alba",
    "AR": "Arad",
    "AG": "Argeș",
    "BC": "Bacău",
    "BH": "Bihor",
    "BN": "Bistrița-Năsăud",
    "BT": "Botoșani",
    "BR": "Brăila",
    "BV": "Brașov",
    "B": "București",
    "BZ": "Buzău",
    "CS": "Caraș-Severin",
    "CL": "Călărași",
    "CJ": "Cluj",
    "CT": "Constanța",
    "CV": "Covasna",
    "DB": "Dâmbovița",
    "DJ": "Dolj",
    "GL": "Galați",
    "GR": "Giurgiu",
    "GJ": "Gorj",
    "HR": "Harghita",
    "HD": "Hunedoara",
    "IL": "Ialomița",
    "IS": "Iași",
    "IF": "Ilfov",
    "MM": "Maramureș",
    "MH": "Mehedinți",
    "MS": "Mureș",
    "NT": "Neamț",
    "OT": "Olt",
    "PH": "Prahova",
    "SM": "Satu Mare",
    "SJ": "Sălaj",
    "SB": "Sibiu",
    "SV": "Suceava",
    "TR": "Teleorman",
    "TM": "Timiș",
    "TL": "Tulcea",
    "VS": "Vaslui",
    "VL": "Vâlcea",
    "VN": "Vrancea",
}

# ══════════════════════════════════════════════
# Utility and unit of measurement mappings
# ══════════════════════════════════════════════

UTILITY_TYPE_LABEL: dict[str, str] = {
    "01": "Electricity",
    "02": "Gas",
}

UTILITY_TYPE_SENSOR_LABEL: dict[str, tuple[str, str, str, str]] = {
    "01": ("Electricity", "Electricity Index", "mdi:lightning-bolt", "electricity_index"),
    "02": ("Gas", "Gas Index", "mdi:gauge", "gas_index"),
}

PORTFOLIO_LABEL: dict[str, str] = {
    "GN": "Natural Gas",
    "EE": "Electrical Energy",
}

UNIT_NORMALIZE: dict[str, str] = {
    "M3": "m³",
    "m3": "m³",
    "KWH": "kWh",
    "kwh": "kWh",
    "MWH": "MWh",
    "mwh": "MWh",
}

CONVENTION_MONTH_MAPPING: dict[str, str] = {
    "valueMonth1": "January", "valueMonth2": "February", "valueMonth3": "March",
    "valueMonth4": "April", "valueMonth5": "May", "valueMonth6": "June",
    "valueMonth7": "July", "valueMonth8": "August", "valueMonth9": "September",
    "valueMonth10": "October", "valueMonth11": "November", "valueMonth12": "December",
}


# ══════════════════════════════════════════════
# API attribute → English label translation mappings
# ══════════════════════════════════════════════

INVOICE_BALANCE_KEY_MAP: dict[str, str] = {
    "balance": "Balance",
    "total": "Total",
    "totalBalance": "Total balance",
    "invoiceValue": "Invoice value",
    "issuedValue": "Issued value",
    "balanceValue": "Remaining balance",
    "paidValue": "Paid amount",
    "maturityDate": "Due date",
    "invoiceNumber": "Invoice number",
    "emissionDate": "Issue date",
    "paymentDate": "Payment date",
    "currency": "Currency",
    "status": "Status",
    "type": "Type",
    "accountContract": "Billing code",
    "refund": "Refund available",
    "date": "Balance date",
    "refundInProcess": "Refund in progress",
    "hasGuarantee": "Active guarantee",
    "hasUnpaidGuarantee": "Unpaid guarantee",
    "balancePay": "Payment balance",
    "refundDocumentsRequired": "Refund documents required",
    "isAssociation": "Association",
}

INVOICE_BALANCE_MONEY_KEYS: set[str] = {
    "balance",
    "total",
    "totalBalance",
    "invoiceValue",
    "issuedValue",
    "balanceValue",
    "paidValue",
}


# ══════════════════════════════════════════════
# Formatting functions
# ══════════════════════════════════════════════

def format_ron(value: float) -> str:
    """Format a numeric value in Romanian style (1.234,56)."""
    formatted = f"{value:,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def format_number_ro(value: float | int | str) -> str:
    """Format a number with Romanian decimal separator (comma).

    Examples:
        4.029   → '4,029'
        124.91  → '124,91'
        11.9    → '11,9'
        0.424   → '0,424'
        100     → '100'
        100.0   → '100'
    """
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value)
    if num == int(num):
        return str(int(num))
    text = str(num)
    return text.replace(".", ",")


def format_invoice_due_message(display_value: float, raw_date: str, date_format: str = "%d.%m.%Y") -> str:
    """Format the due date message for an invoice.

    Returns a message like:
    - "Overdue amount of X lei, deadline exceeded by N days"
    - "Due today: X lei"
    - "Amount of X lei due in MONTH (N days)"

    Raises ValueError if the date cannot be parsed.
    """
    parsed_date = datetime.strptime(raw_date, date_format)
    month_name = parsed_date.strftime("%B")
    days_until_due = (parsed_date.date() - dt_util.now().date()).days

    if days_until_due < 0:
        day_unit = "day" if abs(days_until_due) == 1 else "days"
        return f"Overdue amount of {format_ron(display_value)} lei, deadline exceeded by {abs(days_until_due)} {day_unit}"
    if days_until_due == 0:
        return f"Due today, {dt_util.now().strftime('%d.%m.%Y')}: {format_ron(display_value)} lei"
    day_unit = "day" if days_until_due == 1 else "days"
    return f"Amount of {format_ron(display_value)} lei due in {month_name} ({days_until_due} {day_unit})"


# ══════════════════════════════════════════════
# Authentication functions
# ══════════════════════════════════════════════

def mask_email(email: str) -> str:
    """Mask email address: a*****b@gmail.com.

    Keeps the first and last character of the local part,
    replaces the rest with asterisks. Domain remains visible.
    If local part has 1-2 characters, masking is minimal.
    """
    if not email or "@" not in email:
        return email or "—"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 1:
        masked = local
    elif len(local) == 2:
        masked = f"{local[0]}*"
    else:
        masked = f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"
    return f"{masked}@{domain}"


def generate_verify_hmac(username: str, secret: str) -> str:
    """Generate HMAC-MD5 signature for the verify field in mobile-login."""
    return hmac.new(
        secret.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()


# ══════════════════════════════════════════════
# Config flow functions (contract selection)
# ══════════════════════════════════════════════

def build_address_consum(address_obj: dict) -> str:
    """Build a properly formatted full address for Romania."""
    if not isinstance(address_obj, dict):
        return ""

    def safe_str(value: Any) -> str:
        return str(value).strip() if value else ""

    def clean_parentheses(text: str) -> str:
        """Remove any content of type '(XX)' from text."""
        if "(" in text:
            text = text.split("(")[0]
        return " ".join(text.split())

    parts: list[str] = []

    # ─────────────────────────────
    # Street
    # ─────────────────────────────
    street_obj = address_obj.get("street")
    if isinstance(street_obj, dict):

        street_type = safe_str(
            (street_obj.get("streetType") or {}).get("label")
        )
        street_name = safe_str(street_obj.get("streetName"))

        full_street = " ".join(
            filter(None, [street_type, street_name])
        ).strip()

        if full_street:
            # Capitalize only the street, not everything
            full_street = " ".join(word.capitalize() for word in full_street.split())

            nr = safe_str(address_obj.get("streetNumber"))
            if nr:
                parts.append(f"{full_street} {nr}")
            else:
                parts.append(full_street)

    # Apartment
    apartment = safe_str(address_obj.get("apartment"))
    if apartment and apartment != "0":
        parts.append(f"apt. {apartment}")

    # ─────────────────────────────
    # Locality + county
    # ─────────────────────────────
    locality_obj = address_obj.get("locality")
    if isinstance(locality_obj, dict):

        raw_city = clean_parentheses(
            safe_str(locality_obj.get("localityName"))
        )

        city = raw_city.strip()

        county_code = safe_str(locality_obj.get("countyCode")).upper()
        county_name = COUNTY_CODE_MAP.get(county_code)

        if city:
            if county_name:
                parts.append(f"{city}, county {county_name}")
            else:
                parts.append(city)

    return ", ".join(parts)

def build_contract_options(contracts: list[dict]) -> list[SelectOptionDict]:
    """Build the options list for the contract selector."""
    options: list[SelectOptionDict] = []
    seen: set[str] = set()

    def safe_str(value: Any) -> str:
        return str(value).strip() if value else ""

    for c in contracts or []:
        if not isinstance(c, dict):
            continue

        ac = safe_str(c.get("accountContract"))
        if not ac or ac in seen:
            continue

        seen.add(ac)

        # Address — delegated to helper
        addr = c.get("consumptionPointAddress")
        address = build_address_consum(addr) if addr else "No address"

        # Utility type
        utility = safe_str(c.get("utilityType"))
        utility_label = {
            "00": "DUO (gas + electricity)",
            "01": "Electricity",
            "02": "Gas",
        }.get(utility, "")

        # Final label (without account holder)
        label = f"{address} ➜ {ac}"

        if utility_label:
            label += f" ({utility_label})"

        options.append(
            SelectOptionDict(
                value=ac,
                label=label,
            )
        )

    options.sort(key=lambda x: x["label"].lower())

    return options


def extract_all_contracts(contracts: list[dict]) -> list[str]:
    """Extract all unique contract codes."""
    result: list[str] = []
    for c in contracts:
        if isinstance(c, dict):
            ac = c.get("accountContract", "")
            if ac and ac not in result:
                result.append(ac)
    return result


def build_contract_metadata(contracts: list[dict]) -> dict[str, dict]:
    """Build a dict with relevant metadata per contract.

    Returns: {accountContract: {"utility_type": "00"|"01"|"02", "is_collective": bool}}
    """
    metadata: dict[str, dict] = {}
    for c in contracts or []:
        if not isinstance(c, dict):
            continue
        ac = (c.get("accountContract") or "").strip()
        if not ac:
            continue
        utility_type = (c.get("utilityType") or "").strip()
        # Collective/DUO contract: utilityType "00", type "98", or isCollectiveContract true
        is_collective = (
            utility_type == "00"
            or str(c.get("type", "")).strip() == "98"
            or c.get("isCollectiveContract") is True
            or c.get("collectiveContract") is True
        )
        metadata[ac] = {
            "utility_type": utility_type,
            "is_collective": is_collective,
        }
    return metadata


def resolve_selection(
    select_all: bool,
    selected: list[str],
    contracts: list[dict],
) -> list[str]:
    """Return the final list of contracts."""
    if select_all:
        return extract_all_contracts(contracts)
    return selected


# ══════════════════════════════════════════════
# Constants and helpers for buttons (meter index submission)
# ══════════════════════════════════════════════

# Mapping utility_type → button configuration
# utility_type "02" = Gas, "01" = Electricity
UTILITY_BUTTON_CONFIG: dict[str, dict[str, str]] = {
    "02": {
        "suffix": "submit_gas_index",
        "label": "Submit gas index",
        "icon": "mdi:fire",
        "input_number": "input_number.gas_meter_reading",
        "translation_key": "submit_gas_index",
    },
    "01": {
        "suffix": "submit_electricity_index",
        "label": "Submit electricity index",
        "icon": "mdi:flash",
        "input_number": "input_number.energy_meter_reading",
        "translation_key": "submit_electricity_index",
    },
}

# Fallback for individual contracts (detection from unit of measurement)
UNIT_TO_UTILITY: dict[str, str] = {
    "m3": "02",    # gas
    "kwh": "01",   # electricity
}


def detect_utility_type_individual(coordinator_data: dict | None) -> str:
    """Detect utility_type for an individual contract from coordinator data.

    Uses the unit of measurement from graphic_consumption (um).
    Returns "02" (gas) as fallback.
    """
    if not coordinator_data:
        return "02"
    um = coordinator_data.get("um", "m3")
    return UNIT_TO_UTILITY.get(um.lower(), "02")


def get_subcontract_utility_type(
    subcontracts_list: list[dict] | None, sc_code: str
) -> str | None:
    """Extract utility_type for a subcontract from the subcontracts list."""
    if not subcontracts_list or not isinstance(subcontracts_list, list):
        return None
    for s in subcontracts_list:
        if isinstance(s, dict) and s.get("accountContract") == sc_code:
            return s.get("utilityType")
    return None


def get_meter_data(coordinator_data: dict | None, account_contract: str, is_subcontract: bool = False) -> dict | None:
    """Get meter_index data for a contract or subcontract.

    Args:
        coordinator_data: The complete data dict from coordinator.
        account_contract: The contract / subcontract code.
        is_subcontract: True if searching in subcontracts_meter_index.

    Returns:
        The meter_index dict or None.
    """
    if not coordinator_data:
        return None
    if is_subcontract:
        smi = coordinator_data.get("subcontracts_meter_index")
        if smi and isinstance(smi, dict):
            return smi.get(account_contract)
        return None
    return coordinator_data.get("meter_index")


def extract_ablbelnr(meter_data: dict | None) -> str | None:
    """Extract ablbelnr (internal meter ID) from meter_index data.

    Traverses devices → indexes and returns the first ablbelnr found.
    """
    if not meter_data or not isinstance(meter_data, dict):
        return None
    devices = meter_data.get("indexDetails", {}).get("devices", [])
    for device in devices:
        indexes = device.get("indexes", [])
        if indexes:
            ablbelnr = indexes[0].get("ablbelnr")
            if ablbelnr:
                return ablbelnr
    return None
