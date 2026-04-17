"""Sensor platform for E-ON Energy."""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import UnitOfVolume, UnitOfEnergy
from homeassistant.util import dt as dt_util

from .const import DOMAIN, ATTRIBUTION
from .coordinator import EonEnergyCoordinator
from .helpers import (
    CONVENTION_MONTH_MAPPING,
    INVOICE_BALANCE_KEY_MAP,
    INVOICE_BALANCE_MONEY_KEYS,
    MONTHS_NUM_RO,
    PORTFOLIO_LABEL,
    READING_TYPE_MAP,
    UNIT_NORMALIZE,
    UTILITY_TYPE_LABEL,
    UTILITY_TYPE_SENSOR_LABEL,
    build_address_consum,
    format_invoice_due_message,
    format_number_ro,
    format_ron,
    get_meter_data,
)

_LOGGER = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Base class
# ──────────────────────────────────────────────
class EonEnergyEntity(CoordinatorEntity[EonEnergyCoordinator], SensorEntity):
    """Base class for E-ON Energy entities."""

    _attr_has_entity_name = False

    def __init__(self, coordinator: EonEnergyCoordinator, config_entry: ConfigEntry):
        """Initialize with coordinator and config_entry."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._cod_incasare = coordinator.cod_incasare
        self._custom_entity_id: str | None = None

    @property
    def entity_id(self) -> str | None:
        return self._custom_entity_id

    @entity_id.setter
    def entity_id(self, value: str) -> None:
        self._custom_entity_id = value

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._cod_incasare)},
            name=f"E-ON Energy ({self._cod_incasare})",
            manufacturer="E-ON Energy Integration",
            model="E-ON Energy",
            entry_type=DeviceEntryType.SERVICE,
        )


# ──────────────────────────────────────────────
# async_setup_entry
# ──────────────────────────────────────────────
def _build_sensors_for_coordinator(
    coordinator: EonEnergyCoordinator,
    config_entry: ConfigEntry,
) -> list[SensorEntity]:
    """Build the list of sensors for a single coordinator (contract)."""
    sensors: list[SensorEntity] = []
    cod_incasare = coordinator.cod_incasare

    is_collective = coordinator.is_collective

    # ── 1. Base sensors (always present) ──
    sensors.append(ContractDetailsSensor(coordinator, config_entry))
    sensors.append(OverdueInvoiceSensor(coordinator, config_entry))
    sensors.append(InvoiceBalanceSensor(coordinator, config_entry))

    # Consumption agreement — works on both individual and collective/DUO contracts
    sensors.append(ConsumptionAgreementSensor(coordinator, config_entry))

    # ── 2. Conditional sensors (based on detected capabilities) ──
    caps = coordinator.capabilities

    # Prosum — only if the user has prosum
    if caps and caps.get("has_prosum"):
        sensors.append(FacturaProsumSensor(coordinator, config_entry))
        sensors.append(InvoiceBalanceProsumSensor(coordinator, config_entry))

    # Rescheduling — only if the user has rescheduling plans
    if caps and caps.get("has_rescheduling"):
        sensors.append(ReschedulingPlansSensor(coordinator, config_entry))

    # ── 3. MeterIndexSensor + ReadingAllowedSensor (per device) ──
    if not is_collective:
        # Individual contract: a single meter_index
        citireindex_data = coordinator.data.get("meter_index") if coordinator.data else None
        if citireindex_data:
            devices = citireindex_data.get("indexDetails", {}).get("devices", [])
            seen_devices: set[str] = set()

            for device in devices:
                device_number = device.get("deviceNumber", "unknown_device")
                if device_number not in seen_devices:
                    sensors.append(MeterIndexSensor(coordinator, config_entry, device_number))
                    sensors.append(ReadingAllowedSensor(coordinator, config_entry, device_number))
                    seen_devices.add(device_number)
                else:
                    _LOGGER.warning("Duplicate device ignored (contract=%s): %s", cod_incasare, device_number)

            if not devices:
                sensors.append(CitireIndexSensor(coordinator, config_entry, device_number=None))
                sensors.append(CitirePermisaSensor(coordinator, config_entry, device_number=None))
    else:
        # Collective/DUO contract: meter_index per subcontract
        smi = coordinator.data.get("subcontracts_meter_index") if coordinator.data else None
        subcontracts_list = coordinator.data.get("subcontracts") if coordinator.data else None
        if smi and isinstance(smi, dict):
            for sc_code, mi_data in smi.items():
                if not isinstance(mi_data, dict):
                    continue
                # Determine utility_type from the subcontracts list
                utility_type = None
                if subcontracts_list and isinstance(subcontracts_list, list):
                    for s in subcontracts_list:
                        if isinstance(s, dict) and s.get("accountContract") == sc_code:
                            utility_type = s.get("utilityType")
                            break
                devices = mi_data.get("indexDetails", {}).get("devices", [])
                if devices:
                    seen_devices_duo: set[str] = set()
                    for device in devices:
                        device_number = device.get("deviceNumber", "unknown_device")
                        if device_number not in seen_devices_duo:
                            sensors.append(MeterIndexSensor(
                                coordinator, config_entry, device_number,
                                subcontract_code=sc_code, utility_type=utility_type,
                            ))
                            sensors.append(ReadingAllowedSensor(
                                coordinator, config_entry, device_number,
                                subcontract_code=sc_code, utility_type=utility_type,
                            ))
                            seen_devices_duo.add(device_number)
                else:
                    sensors.append(MeterIndexSensor(
                        coordinator, config_entry, device_number=None,
                        subcontract_code=sc_code, utility_type=utility_type,
                    ))
                    sensors.append(ReadingAllowedSensor(
                        coordinator, config_entry, device_number=None,
                        subcontract_code=sc_code, utility_type=utility_type,
                    ))

    # ── 4. IndexArchiveSensor (history by year) ──
    # (not created for collective contracts — endpoint does not work)
    arhiva_data = coordinator.data.get("meter_history") if coordinator.data else None
    if arhiva_data and not is_collective:
        history_list = arhiva_data.get("history", [])
        valid_years = {item.get("year") for item in history_list if item.get("year")}
        if valid_years:
            for year in valid_years:
                sensors.append(IndexArchiveSensor(coordinator, config_entry, year))

    # ── 5. PaymentArchiveSensor (history by year) ──
    payments_list = coordinator.data.get("payments", []) if coordinator.data else []
    if payments_list:
        payments_by_year: dict[int, list] = defaultdict(list)
        for payment in payments_list:
            raw_date = payment.get("paymentDate")
            if not raw_date:
                continue
            try:
                year = int(raw_date.split("-")[0])
                payments_by_year[year].append(payment)
            except ValueError:
                continue
        if payments_by_year:
            for year in payments_by_year:
                sensors.append(PaymentArchiveSensor(coordinator, config_entry, year))

    # ── 6. ConsumptionArchiveSensor (history by year) ──
    # (not created for collective contracts — endpoint does not work)
    comparareanualagrafic_data = coordinator.data.get("graphic_consumption", {}) if (coordinator.data and not is_collective) else {}
    if isinstance(comparareanualagrafic_data, dict) and "consumption" in comparareanualagrafic_data:
        yearly_data: dict[int, dict] = defaultdict(dict)
        for item in comparareanualagrafic_data["consumption"]:
            year = item.get("year")
            month = item.get("month")
            consumption_value = item.get("consumptionValue")
            consumption_day_value = item.get("consumptionValueDayValue")
            if not year or not month or consumption_value is None or consumption_day_value is None:
                continue
            yearly_data[year][month] = {
                "consumptionValue": consumption_value,
                "consumptionValueDayValue": consumption_day_value,
            }

        cleaned_yearly_data = {
            year: monthly_values
            for year, monthly_values in yearly_data.items()
            if any(v["consumptionValue"] > 0 or v["consumptionValueDayValue"] > 0 for v in monthly_values.values())
        }
        if cleaned_yearly_data:
            for year, monthly_values in cleaned_yearly_data.items():
                sensors.append(
                    ConsumptionArchiveSensor(
                        coordinator, config_entry, year, monthly_values
                    )
                )

    return sensors


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
):
    """Set up sensors for all selected contracts."""
    coordinators: dict[str, EonEnergyCoordinator] = config_entry.runtime_data.coordinators

    _LOGGER.debug(
        "Initializing sensor platform for %s (entry_id=%s, contracts=%s).",
        DOMAIN, config_entry.entry_id, list(coordinators.keys()),
    )

    all_sensors: list[SensorEntity] = []

    for cod_incasare, coordinator in coordinators.items():
        if coordinator.account_only:
            # Account without contracts — personal data sensor only
            sensors = [UserDetailsSensor(coordinator, config_entry)]
            _LOGGER.debug(
                "Adding personal data sensor (account_only) for %s.", cod_incasare,
            )
        else:
            sensors = _build_sensors_for_coordinator(coordinator, config_entry)
            _LOGGER.debug(
                "Adding %s sensors for contract %s.", len(sensors), cod_incasare,
            )
        all_sensors.extend(sensors)

    _LOGGER.info(
        "Total %s sensors added for %s (entry_id=%s).",
        len(all_sensors), DOMAIN, config_entry.entry_id,
    )

    async_add_entities(all_sensors)


# ══════════════════════════════════════════════
# NEW SENSORS
# ══════════════════════════════════════════════


# ──────────────────────────────────────────────
# UserDetailsSensor (for accounts without contracts)
# ──────────────────────────────────────────────
class UserDetailsSensor(CoordinatorEntity[EonEnergyCoordinator], SensorEntity):
    """Sensor with user personal data (for accounts without contracts)."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:account-circle"

    def __init__(self, coordinator: EonEnergyCoordinator, config_entry: ConfigEntry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        username = config_entry.data.get("username", "unknown")
        safe_username = username.replace("@", "_").replace(".", "_")
        self._attr_name = "E-ON Energy Personal Details"
        self._attr_unique_id = f"{DOMAIN}_user_details_{safe_username}"
        self._custom_entity_id: str | None = f"sensor.{DOMAIN}_{safe_username}_personal_details"

    @property
    def entity_id(self) -> str | None:
        return self._custom_entity_id

    @entity_id.setter
    def entity_id(self, value: str) -> None:
        self._custom_entity_id = value

    @property
    def device_info(self) -> DeviceInfo:
        username = self._config_entry.data.get("username", "unknown")
        return DeviceInfo(
            identifiers={(DOMAIN, f"account_{username}")},
            name=f"E-ON Energy ({username})",
            manufacturer="E-ON Energy Integration",
            model="E-ON Energy — Personal Account",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None
        user = data.get("user_details")
        if not user or not isinstance(user, dict):
            return None
        first = user.get("firstName", "")
        last = user.get("lastName", "")
        return f"{first} {last}".strip() or user.get("email", "Unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        user = data.get("user_details")
        if not user or not isinstance(user, dict):
            return None

        attrs: dict[str, Any] = {
            "first_name": user.get("firstName", ""),
            "last_name": user.get("lastName", ""),
            "email": user.get("email", ""),
            "mobile_phone": user.get("mobilePhoneNumber", ""),
            "landline_phone": user.get("fixPhoneNumber", ""),
            "user_type": user.get("userType", ""),
            "mfa_active": user.get("secondFactorAuth", False),
            "mfa_method": user.get("secondFactorAuthMethod") or "—",
            "mfa_alert": user.get("mfaAlert", ""),
            "migrated": user.get("migrated", False),
            "gdpr_shown": user.get("showGDPR", False),
            "wallet_active": user.get("showWallet", False),
            "contracts": "No associated contracts",
            "attribution": ATTRIBUTION,
        }
        return attrs


# ──────────────────────────────────────────────
# ContractDetailsSensor
# ──────────────────────────────────────────────
class ContractDetailsSensor(EonEnergyEntity):
    """Sensor for displaying contract data."""

    _attr_icon = "mdi:file-document-edit-outline"
    _attr_translation_key = "contract_data"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Contract Data"
        self._attr_unique_id = f"{DOMAIN}_contract_data_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_contract_data"

    @property
    def native_value(self):
        data = self.coordinator.data.get("contract_details") if self.coordinator.data else None
        if not data:
            return None
        return data.get("accountContract")

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data.get("contract_details")
        if not isinstance(data, dict):
            return {}

        is_collective = self.coordinator.data.get("is_collective", False)

        if is_collective:
            return self._build_collective_attributes(data)
        return self._build_individual_attributes(data)

    def _build_individual_attributes(self, data: dict) -> dict[str, Any]:
        """Build attributes for an individual contract (gas or electricity)."""
        attributes: dict[str, Any] = {}

        # ─────────────────────────────
        # General contract data
        # ─────────────────────────────
        if data.get("accountContract"):
            attributes["Billing code"] = data["accountContract"]

        if data.get("consumptionPointCode"):
            attributes["Consumption point code (NLC)"] = data["consumptionPointCode"]

        if data.get("pod"):
            attributes["Metering point code (POD)"] = data["pod"]

        if data.get("distributorName"):
            attributes["Distribution Operator (DO)"] = data["distributorName"]

        # ─────────────────────────────
        # Prices
        # ─────────────────────────────
        price_data = data.get("supplierAndDistributionPrice")
        if isinstance(price_data, dict):

            if price_data.get("contractualPrice") is not None:
                attributes["Final price (excl. VAT)"] = f"{price_data['contractualPrice']} lei"

            if price_data.get("contractualPriceWithVat") is not None:
                attributes["Final price (incl. VAT)"] = f"{price_data['contractualPriceWithVat']} lei"

            components = price_data.get("priceComponents")
            if isinstance(components, dict):

                if components.get("supplierPrice") is not None:
                    attributes["Supply price"] = f"{components['supplierPrice']} lei/kWh"

                if components.get("distributionPrice") is not None:
                    attributes["Regulated distribution tariff"] = f"{components['distributionPrice']} lei/kWh"

                if components.get("transportPrice") is not None:
                    attributes["Regulated transport tariff"] = f"{components['transportPrice']} lei/kWh"

            if price_data.get("pcs") is not None:
                attributes["PCS"] = str(price_data["pcs"])

        # ─────────────────────────────
        # Address (uses the helper!)
        # ─────────────────────────────
        address_obj = data.get("consumptionPointAddress")
        if isinstance(address_obj, dict):
            formatted_address = build_address_consum(address_obj)
            if formatted_address:
                attributes["Consumption address"] = formatted_address

        # ─────────────────────────────
        # Verification / revision dates
        # ─────────────────────────────
        if data.get("verificationExpirationDate"):
            attributes["Next installation verification"] = data["verificationExpirationDate"]

        if data.get("revisionStartDate"):
            attributes["Revision start date"] = data["revisionStartDate"]

        if data.get("revisionExpirationDate"):
            attributes["Next technical revision"] = data["revisionExpirationDate"]

        attributes["attribution"] = ATTRIBUTION

        return attributes

    def _build_collective_attributes(self, data: dict) -> dict[str, Any]:
        """Build attributes for a collective/DUO contract (gas + electricity)."""
        attributes: dict[str, Any] = {}

        # ─────────────────────────────
        # Collective contract data (from contract_details)
        # ─────────────────────────────
        if data.get("accountContract"):
            attributes["Billing code (DUO)"] = data["accountContract"]

        attributes["Contract type"] = "Collective / DUO (gas + electricity)"

        if data.get("contractName"):
            attributes["Contract name"] = data["contractName"]

        # Mailing address (from the main contract)
        mailing = data.get("mailingAddress")
        if isinstance(mailing, dict):
            formatted = build_address_consum(mailing)
            if formatted:
                attributes["Mailing address"] = formatted

        # ─────────────────────────────
        # Subcontracts from list-with-subcontracts
        # (now a flat list of sub-contracts, extracted from subContracts[])
        # ─────────────────────────────
        subcontracts = self.coordinator.data.get("subcontracts")
        subcontracts_details = self.coordinator.data.get("subcontracts_details")

        if subcontracts and isinstance(subcontracts, list):
            attributes["────"] = ""
            attributes["Number of subcontracts"] = len(subcontracts)

            for idx, sub in enumerate(subcontracts, start=1):
                if not isinstance(sub, dict):
                    continue

                sub_ac = sub.get("accountContract", "N/A")
                utility = sub.get("utilityType", "")
                utility_label = UTILITY_TYPE_LABEL.get(utility, utility or "Unknown")

                prefix = utility_label
                attributes[f"{prefix} — Billing code"] = sub_ac

                if sub.get("consumptionPointCode"):
                    attributes[f"{prefix} — Consumption point code (NLC)"] = sub["consumptionPointCode"]

                if sub.get("pod"):
                    attributes[f"{prefix} — Metering point code (POD)"] = sub["pod"]

                sub_addr = sub.get("consumptionPointAddress")
                if isinstance(sub_addr, dict):
                    formatted_sub = build_address_consum(sub_addr)
                    if formatted_sub:
                        attributes[f"{prefix} — Consumption address"] = formatted_sub

        # ─────────────────────────────
        # Subcontract details from contracts-details-list
        # (flat structure: each element has prices, meter readings, revision dates, etc.)
        # ─────────────────────────────
        if subcontracts_details and isinstance(subcontracts_details, list):
            for detail in subcontracts_details:
                if not isinstance(detail, dict):
                    continue

                detail_ac = detail.get("accountContract", "N/A")
                # Prefer portfolioName (GN/EE) if present, otherwise utilityType
                portfolio = detail.get("portfolioName", "")
                utility = detail.get("utilityType", "")
                if portfolio and portfolio in PORTFOLIO_LABEL:
                    utility_label = PORTFOLIO_LABEL[portfolio]
                elif utility in UTILITY_TYPE_LABEL:
                    utility_label = UTILITY_TYPE_LABEL[utility]
                else:
                    utility_label = portfolio or utility or "Unknown"

                prefix = utility_label

                attributes[f"──── {utility_label} ────"] = ""

                attributes[f"{prefix} — Billing code"] = detail_ac

                if detail.get("distributorName"):
                    attributes[f"{prefix} — Distribution Operator (DO)"] = detail["distributorName"]

                if detail.get("contractName"):
                    attributes[f"{prefix} — Contract name"] = detail["contractName"]

                if detail.get("productName"):
                    attributes[f"{prefix} — Product"] = detail["productName"]

                if detail.get("consumptionPointCode"):
                    attributes[f"{prefix} — Consumption point code (NLC)"] = detail["consumptionPointCode"]

                if detail.get("pod"):
                    attributes[f"{prefix} — Metering point code (POD)"] = detail["pod"]

                # Subcontract prices
                price_data = detail.get("supplierAndDistributionPrice")
                if isinstance(price_data, dict):
                    if price_data.get("contractualPrice") is not None:
                        attributes[f"{prefix} — Final price (excl. VAT)"] = f"{price_data['contractualPrice']} lei"

                    if price_data.get("contractualPriceWithVat") is not None:
                        attributes[f"{prefix} — Final price (incl. VAT)"] = f"{price_data['contractualPriceWithVat']} lei"

                    components = price_data.get("priceComponents")
                    if isinstance(components, dict):
                        if components.get("supplierPrice") is not None:
                            attributes[f"{prefix} — Supply price"] = f"{components['supplierPrice']} lei"
                        if components.get("distributionPrice") is not None:
                            attributes[f"{prefix} — Distribution tariff"] = f"{components['distributionPrice']} lei"
                        if components.get("transportPrice") is not None:
                            attributes[f"{prefix} — Transport tariff"] = f"{components['transportPrice']} lei"

                    if price_data.get("pcs") is not None:
                        attributes[f"{prefix} — PCS"] = str(price_data["pcs"])

                # Meter readings (meterReadings)
                meter_readings = detail.get("meterReadings")
                if isinstance(meter_readings, list) and meter_readings:
                    for mr in meter_readings:
                        if not isinstance(mr, dict):
                            continue
                        meter_num = mr.get("meterNumber", "")
                        if mr.get("currentIndex") is not None:
                            attributes[f"{prefix} — Current index ({meter_num})"] = format_number_ro(mr["currentIndex"])
                        if mr.get("oldIndex") is not None:
                            attributes[f"{prefix} — Previous index ({meter_num})"] = format_number_ro(mr["oldIndex"])
                        reading_type = mr.get("readingType", "")
                        if reading_type:
                            attributes[f"{prefix} — Reading type ({meter_num})"] = READING_TYPE_MAP.get(reading_type, reading_type)

                # Subcontract consumption address
                sub_addr = detail.get("consumptionPointAddress")
                if isinstance(sub_addr, dict):
                    formatted_sub = build_address_consum(sub_addr)
                    if formatted_sub:
                        attributes[f"{prefix} — Consumption address"] = formatted_sub

                # Installation verification / revision
                if detail.get("verificationExpirationDate"):
                    attributes[f"{prefix} — Installation verification"] = detail["verificationExpirationDate"]

                if detail.get("revisionExpirationDate"):
                    attributes[f"{prefix} — Technical revision"] = detail["revisionExpirationDate"]

                if detail.get("revisionStartDate"):
                    attributes[f"{prefix} — Revision start date"] = detail["revisionStartDate"]

        attributes["attribution"] = ATTRIBUTION

        return attributes


# ──────────────────────────────────────────────
# InvoiceBalanceSensor
# ──────────────────────────────────────────────
class InvoiceBalanceSensor(EonEnergyEntity):
    """Sensor for invoice balance per contract."""

    _attr_icon = "mdi:cash"
    _attr_translation_key = "invoice_balance"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Invoice Balance"
        self._attr_unique_id = f"{DOMAIN}_invoice_balance_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_invoice_balance"

    @property
    def native_value(self):
        data = self.coordinator.data.get("invoice_balance") if self.coordinator.data else None
        if not data or not isinstance(data, dict):
            return "No"
        balance = data.get("balance", data.get("total", data.get("totalBalance")))
        if balance is not None and float(balance) > 0:
            return "Yes"
        return "No"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get("invoice_balance") if self.coordinator.data else None
        if not data:
            return {"attribution": ATTRIBUTION}

        attributes = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    continue
                label = INVOICE_BALANCE_KEY_MAP.get(key, key)
                if isinstance(value, (int, float)) and key in INVOICE_BALANCE_MONEY_KEYS:
                    attributes[label] = f"{format_ron(float(value))} lei"
                elif isinstance(value, bool) or (isinstance(value, str) and value.lower() in ("true", "false")):
                    bool_val = value if isinstance(value, bool) else value.lower() == "true"
                    attributes[label] = "Yes" if bool_val else "No"
                else:
                    attributes[label] = value
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# InvoiceBalanceProsumSensor
# ──────────────────────────────────────────────
class InvoiceBalanceProsumSensor(EonEnergyEntity):
    """Sensor for prosumer invoice balance."""

    _attr_icon = "mdi:solar-power-variant"
    _attr_translation_key = "prosumer_balance"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Prosumer Balance"
        self._attr_unique_id = f"{DOMAIN}_prosumer_balance_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_prosumer_balance"

    @property
    def native_value(self):
        data = self.coordinator.data.get("invoice_balance_prosum") if self.coordinator.data else None
        if not data or not isinstance(data, dict):
            return "No"
        balance = float(data.get("balance", 0))
        if balance > 0:
            return "Yes"
        return "No"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get("invoice_balance_prosum") if self.coordinator.data else None
        if not data or not isinstance(data, dict):
            return {"attribution": ATTRIBUTION}
        attributes = {}
        balance = float(data.get("balance", 0))
        if balance > 0:
            attributes["Balance"] = f"{format_ron(balance)} lei (debit)"
        elif balance < 0:
            attributes["Balance"] = f"{format_ron(abs(balance))} lei (credit)"
        else:
            attributes["Balance"] = "0,00 lei"
        if data.get("refund"):
            attributes["Refund available"] = "Yes"
        if data.get("refundInProcess"):
            attributes["Refund in progress"] = "Yes"
        if data.get("date"):
            attributes["Balance date"] = data.get("date")
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# ReschedulingPlansSensor
# ──────────────────────────────────────────────
class ReschedulingPlansSensor(EonEnergyEntity):
    """Sensor for rescheduling plans."""

    _attr_icon = "mdi:calendar-clock"
    _attr_translation_key = "rescheduling_plans" # already in English

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Rescheduling Plans"
        self._attr_unique_id = f"{DOMAIN}_rescheduling_plans_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_rescheduling_plans"

    @property
    def native_value(self):
        data = self.coordinator.data.get("rescheduling_plans") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            return 0
        return len(data)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get("rescheduling_plans") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            return {"attribution": ATTRIBUTION}
        attributes = {}
        for idx, plan in enumerate(data, start=1):
            attributes[f"Plan {idx}"] = str(plan)
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ══════════════════════════════════════════════
# EXISTING SENSORS (UPDATED)
# ══════════════════════════════════════════════


# ──────────────────────────────────────────────
# MeterIndexSensor
# ──────────────────────────────────────────────
class MeterIndexSensor(EonEnergyEntity):
    """Sensor for displaying current meter index data."""

    def __init__(self, coordinator, config_entry, device_number, subcontract_code=None, utility_type=None):
        super().__init__(coordinator, config_entry)
        self.device_number = device_number
        self._subcontract_code = subcontract_code

        if subcontract_code and utility_type:
            # DUO mode: utility type comes from subcontract
            label_info = UTILITY_TYPE_SENSOR_LABEL.get(utility_type)
            if label_info:
                _, name, icon, tkey = label_info
            else:
                name = "Index"
                icon = "mdi:gauge"
                tkey = "current_index"
            self._attr_name = name
            self._attr_icon = icon
            self._attr_translation_key = tkey
            self._attr_unique_id = f"{DOMAIN}_current_index_{subcontract_code}"
            self._custom_entity_id = f"sensor.{DOMAIN}_{subcontract_code}_{tkey.replace(' ', '_')}"
        else:
            # Individual mode: determined from unit of measurement
            um = coordinator.data.get("um", "m3") if coordinator.data else "m3"
            is_gaz = um.lower().startswith("m")
            self._attr_name = "Gas Index" if is_gaz else "Electricity Index"
            self._attr_icon = "mdi:gauge" if is_gaz else "mdi:lightning-bolt"
            self._attr_translation_key = "gas_index" if is_gaz else "electricity_index"
            self._attr_unique_id = f"{DOMAIN}_current_index_{self._cod_incasare}"
            self._custom_entity_id = (
                f"sensor.{DOMAIN}_{self._cod_incasare}_gas_index"
                if is_gaz
                else f"sensor.{DOMAIN}_{self._cod_incasare}_electricity_index"
            )

    @property
    def native_unit_of_measurement(self) -> str:
        if self._subcontract_code:
            # DUO: determine from utility_type stored at init
            return UnitOfVolume.CUBIC_METERS if "gaz" in self._attr_name.lower() else UnitOfEnergy.KILO_WATT_HOUR
        um = self.coordinator.data.get("um", "m3") if self.coordinator.data else "m3"
        return UnitOfVolume.CUBIC_METERS if um.lower().startswith("m") else UnitOfEnergy.KILO_WATT_HOUR

    @property
    def native_value(self):
        citireindex_data = get_meter_data(
            self.coordinator.data, self._subcontract_code or self._cod_incasare,
            is_subcontract=bool(self._subcontract_code),
        )
        if not citireindex_data:
            return 0
        devices = citireindex_data.get("indexDetails", {}).get("devices", [])
        if not devices:
            return 0
        for dev in devices:
            if dev.get("deviceNumber") == self.device_number:
                indexes = dev.get("indexes", [])
                if indexes:
                    current_value = indexes[0].get("currentValue")
                    if current_value is not None:
                        return int(current_value)
                    old_value = indexes[0].get("oldValue")
                    if old_value is not None:
                        return int(old_value)
        return 0

    @property
    def extra_state_attributes(self):
        citireindex_data = get_meter_data(
            self.coordinator.data, self._subcontract_code or self._cod_incasare,
            is_subcontract=bool(self._subcontract_code),
        )
        if not citireindex_data:
            return {}

        index_details = citireindex_data.get("indexDetails", {})
        devices = index_details.get("devices", [])
        reading_period = citireindex_data.get("readingPeriod", {})

        if not devices:
            return {"Updating": ""}

        for dev in devices:
            if dev.get("deviceNumber") == self.device_number:
                indexes = dev.get("indexes", [])
                if not indexes:
                    continue

                first_index = indexes[0]
                attributes = {}

                if dev.get("deviceNumber") is not None:
                    attributes["Device number"] = dev.get("deviceNumber")
                if first_index.get("ablbelnr") is not None:
                    attributes["Internal meter reading ID"] = first_index.get("ablbelnr")
                if reading_period.get("startDate") is not None:
                    attributes["Next reading start date"] = reading_period.get("startDate")
                if reading_period.get("endDate") is not None:
                    attributes["Reading end date"] = reading_period.get("endDate")
                if reading_period.get("allowedReading") is not None:
                    attributes["Authorized to read meter"] = "Yes" if reading_period.get("allowedReading") else "No"
                if reading_period.get("allowChange") is not None:
                    attributes["Allows reading modification"] = "Yes" if reading_period.get("allowChange") else "No"
                if reading_period.get("smartDevice") is not None:
                    attributes["Smart device"] = "Yes" if reading_period.get("smartDevice") else "No"

                crt_reading_type = reading_period.get("currentReadingType")
                if crt_reading_type is not None:
                    reading_type_labels = {"01": "Distributor reading", "02": "Self-reading", "03": "Estimated"}
                    attributes["Current reading type"] = reading_type_labels.get(crt_reading_type, "Unknown")

                if first_index.get("minValue") is not None:
                    attributes["Previous reading"] = first_index.get("minValue")
                if first_index.get("oldValue") is not None:
                    attributes["Last validated reading"] = first_index.get("oldValue")
                if first_index.get("currentValue") is not None:
                    attributes["Index proposed for billing"] = first_index.get("currentValue")
                if first_index.get("sentAt") is not None:
                    attributes["Sent at"] = first_index.get("sentAt")
                if first_index.get("canBeChangedTill") is not None:
                    attributes["Can be changed until"] = first_index.get("canBeChangedTill")

                attributes["attribution"] = ATTRIBUTION
                return attributes

        return {}


# ──────────────────────────────────────────────
# ReadingAllowedSensor
# ──────────────────────────────────────────────
class ReadingAllowedSensor(EonEnergyEntity):
    """Sensor for checking meter index reading permission."""

    _attr_translation_key = "reading_allowed"

    def __init__(self, coordinator, config_entry, device_number, subcontract_code=None, utility_type=None):
        super().__init__(coordinator, config_entry)
        self.device_number = device_number
        self._subcontract_code = subcontract_code

        if subcontract_code and utility_type:
            # DUO mode
            ut_labels = {"01": "electricity", "02": "gas"}
            ut_label = ut_labels.get(utility_type, "")
            suffix = f" {ut_label}" if ut_label else ""
            self._attr_name = f"Reading Allowed{suffix}"
            self._attr_unique_id = f"{DOMAIN}_reading_allowed_{subcontract_code}"
            self._custom_entity_id = f"sensor.{DOMAIN}_{subcontract_code}_reading_allowed"
        else:
            self._attr_name = "Reading Allowed"
            self._attr_unique_id = f"{DOMAIN}_reading_allowed_{self._cod_incasare}"
            self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_reading_allowed"

    @property
    def native_value(self):
        citireindex_data = get_meter_data(
            self.coordinator.data, self._subcontract_code or self._cod_incasare,
            is_subcontract=bool(self._subcontract_code),
        )
        if not citireindex_data:
            return "No"

        reading_period = citireindex_data.get("readingPeriod", {})

        # 1. Most reliable indicator: inPeriod (set by API)
        in_period = reading_period.get("inPeriod")
        if in_period is not None:
            return "Yes" if in_period else "No"

        # 2. Fallback: allowedReading
        allowed = reading_period.get("allowedReading")
        if allowed is not None:
            return "Yes" if allowed else "No"

        # 3. Final fallback: manual date check
        start_date_str = reading_period.get("startDate")
        end_date_str = reading_period.get("endDate")

        # Fallback: manual check on endDate from readingPeriod
        try:
            today = dt_util.now().replace(tzinfo=None)
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d") if start_date_str else None
            # Upper limit: endDate from readingPeriod (not canBeChangedTill which is the modification limit)
            upper_str = end_date_str
            upper_date = None
            if upper_str:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        upper_date = datetime.strptime(upper_str, fmt)
                        break
                    except ValueError:
                        continue

            if start_date and upper_date:
                if start_date <= today <= upper_date:
                    return "Yes"
                return "No"
            if start_date and today >= start_date:
                return "Yes"
            return "No"
        except Exception as e:
            _LOGGER.exception(
                "Error determining ReadingAllowed state (contract=%s): %s",
                self._subcontract_code or self._cod_incasare, e,
            )
            return "Error"

    @property
    def extra_state_attributes(self):
        citireindex_data = get_meter_data(
            self.coordinator.data, self._subcontract_code or self._cod_incasare,
            is_subcontract=bool(self._subcontract_code),
        )
        if not citireindex_data:
            return {}

        reading_period = citireindex_data.get("readingPeriod", {})
        index_details = citireindex_data.get("indexDetails", {})
        devices = index_details.get("devices", [])

        if not devices:
            return {"Updating": ""}

        for dev in devices:
            if dev.get("deviceNumber") == self.device_number:
                indexes = dev.get("indexes", [{}])[0]
                can_be_changed_till = indexes.get("canBeChangedTill")
                end_date = reading_period.get("endDate")
                start_date = reading_period.get("startDate")

                # endDate = index submission deadline; canBeChangedTill = modification deadline for submitted index
                deadline = f"{end_date} 23:59:59" if end_date else None

                attributes = {}
                attributes["Internal meter reading ID (SAP)"] = indexes.get("ablbelnr", "Unknown")
                attributes["Index can be submitted until"] = deadline or "Period not established"

                if can_be_changed_till:
                    attributes["Index can be modified until"] = can_be_changed_till

                if start_date and end_date:
                    attributes["Index submission period"] = f"{start_date} — {end_date}"

                in_period = reading_period.get("inPeriod")
                if in_period is not None:
                    attributes["In reading period"] = "Yes" if in_period else "No"

                allowed = reading_period.get("allowedReading")
                if allowed is not None:
                    attributes["Authorized reading"] = "Yes" if allowed else "No"

                attributes["Billing code"] = self._subcontract_code or self._cod_incasare
                return attributes
        return {}

    @property
    def icon(self):
        value = self.native_value
        if value == "Yes":
            return "mdi:clock-check-outline"
        if value == "No":
            return "mdi:clock-alert-outline"
        return "mdi:cog-stop-outline"


# ──────────────────────────────────────────────
# OverdueInvoiceSensor
# ──────────────────────────────────────────────
class OverdueInvoiceSensor(EonEnergyEntity):
    """Sensor for displaying outstanding invoice balances."""

    _attr_icon = "mdi:invoice-text-arrow-left"
    _attr_translation_key = "overdue_invoice"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Overdue Invoice"
        self._attr_unique_id = f"{DOMAIN}_overdue_invoice_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_overdue_invoice"

    @property
    def native_value(self):
        data = self.coordinator.data.get("invoices_unpaid") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            return "No"
        return "Yes" if any(item.get("issuedValue", 0) > 0 for item in data) else "No"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get("invoices_unpaid") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            return {
                "Total unpaid": "0,00 lei",
                "Details": "No invoices available",
                "attribution": ATTRIBUTION,
            }

        attributes = {}
        total_sold = 0.0

        for idx, item in enumerate(data, start=1):
            issued_value = float(item.get("issuedValue", 0))
            balance_value = float(item.get("balanceValue", 0))
            display_value = issued_value if issued_value == balance_value else balance_value

            if display_value > 0:
                total_sold += display_value
                raw_date = item.get("maturityDate", "Unknown")
                try:
                    msg = format_invoice_due_message(display_value, raw_date)
                    attributes[f"Invoice {idx}"] = msg
                except ValueError:
                    attributes[f"Invoice {idx}"] = "Due date unknown"

        attributes["Total unpaid"] = f"{format_ron(total_sold)} lei" if total_sold > 0 else "0,00 lei"
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# ProsumerInvoiceSensor
# ──────────────────────────────────────────────
class ProsumerInvoiceSensor(EonEnergyEntity):
    """Sensor for displaying outstanding prosumer invoice balances."""

    _attr_icon = "mdi:invoice-text-arrow-left"
    _attr_translation_key = "prosumer_invoice"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Overdue Prosumer Invoice"
        self._attr_unique_id = f"{DOMAIN}_prosumer_invoice_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_prosumer_invoice"

    @property
    def native_value(self):
        data = self.coordinator.data.get("invoices_prosum") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            balance_data = self.coordinator.data.get("invoice_balance_prosum") if self.coordinator.data else None
            if balance_data and isinstance(balance_data, dict):
                balance = float(balance_data.get("balance", 0))
                return "Yes" if balance > 0 else "No"
            return "No"
        return "Yes" if any(float(item.get("issuedValue", 0)) > 0 for item in data) else "No"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data.get("invoices_prosum") if self.coordinator.data else None
        if not data or not isinstance(data, list):
            return {
                "Total neachitat": "0,00 lei",
                "Details": "No invoices available",
                "attribution": ATTRIBUTION,
            }

        attributes = {}
        total_sold = 0.0
        total_credit = 0.0

        for idx, item in enumerate(data, start=1):
            issued_value = float(item.get("issuedValue", 0))
            balance_value = float(item.get("balanceValue", 0))
            display_value = issued_value if issued_value == balance_value else balance_value
            raw_date = item.get("maturityDate", "Unknown")
            invoice_number = item.get("invoiceNumber", "N/A")
            invoice_type = item.get("type", "Unknown")

            try:
                if display_value > 0:
                    total_sold += display_value
                    msg = format_invoice_due_message(display_value, raw_date)
                    attributes[f"Invoice {idx} ({invoice_number})"] = msg
                elif display_value < 0:
                    total_credit += abs(display_value)
                    msg = f"Credit of {format_ron(abs(display_value))} lei for {invoice_type.lower()} (due {raw_date})"
                    attributes[f"Credit {idx} ({invoice_number})"] = msg
                else:
                    attributes[f"Invoice {idx} ({invoice_number})"] = f"No balance (due {raw_date})"
            except ValueError:
                if display_value > 0:
                    attributes[f"Invoice {idx} ({invoice_number})"] = f"Debt of {format_ron(display_value)} lei"
                elif display_value < 0:
                    attributes[f"Credit {idx} ({invoice_number})"] = f"Credit of {format_ron(abs(display_value))} lei"

        if total_sold > 0:
            attributes["Total debt"] = f"{format_ron(total_sold)} lei"
        if total_credit > 0:
            attributes["Total credit"] = f"{format_ron(total_credit)} lei"
        attributes["Total unpaid"] = f"{format_ron(total_sold)} lei" if total_sold > 0 else "0,00 lei"
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# ConsumptionAgreementSensor
# ──────────────────────────────────────────────
class ConsumptionAgreementSensor(EonEnergyEntity):
    """Sensor for displaying consumption agreement data."""

    _attr_icon = "mdi:chart-bar"
    _attr_translation_key = "consumption_agreement"

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._attr_name = "Consumption Agreement"
        self._attr_unique_id = f"{DOMAIN}_consumption_agreement_{self._cod_incasare}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_consumption_agreement"

    @property
    def native_value(self):
        is_collective = self.coordinator.data.get("is_collective", False) if self.coordinator.data else False

        if is_collective:
            return self._native_value_collective()
        return self._native_value_individual()

    def _native_value_individual(self):
        data = self.coordinator.data.get("consumption_convention") if self.coordinator.data else None
        if not data or not isinstance(data, list) or len(data) == 0:
            return "No"
        convention_line = data[0].get("conventionLine", {})
        months_with_values = sum(
            1 for key in convention_line
            if key.startswith("valueMonth") and convention_line.get(key, 0) > 0
        )
        return "Yes" if months_with_values > 0 else "No"

    def _native_value_collective(self):
        conventions = self.coordinator.data.get("subcontracts_conventions") if self.coordinator.data else None
        if not conventions or not isinstance(conventions, dict):
            return "No"
        # "Yes" if at least one subcontract has agreement with values > 0
        for conv_data in conventions.values():
            if not isinstance(conv_data, list) or not conv_data:
                continue
            convention_line = conv_data[0].get("conventionLine", {})
            if any(
                convention_line.get(key, 0) > 0
                for key in convention_line
                if key.startswith("valueMonth")
            ):
                return "Yes"
        return "No"

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}

        is_collective = self.coordinator.data.get("is_collective", False)

        if is_collective:
            return self._attributes_collective()
        return self._attributes_individual()

    def _attributes_individual(self):
        data = self.coordinator.data.get("consumption_convention") if self.coordinator.data else None
        if not data or not isinstance(data, list) or len(data) == 0:
            return {}
        convention_line = data[0].get("conventionLine", {})
        um = self.coordinator.data.get("um", "m3") if self.coordinator.data else "m3"
        is_gaz = um.lower().startswith("m")
        unit = "m³" if is_gaz else "kWh"
        attributes = {
            f"Agreement from {month}": f"{convention_line.get(key, 0)} {unit}"
            for key, month in CONVENTION_MONTH_MAPPING.items()
        }
        attributes["attribution"] = ATTRIBUTION
        return attributes

    def _attributes_collective(self):
        """Build attributes for collective/DUO contracts — agreements per subcontract."""
        conventions = self.coordinator.data.get("subcontracts_conventions")
        subcontracts = self.coordinator.data.get("subcontracts")
        if not conventions or not isinstance(conventions, dict):
            return {}

        attributes: dict[str, Any] = {}

        for sc_code, conv_data in conventions.items():
            if not isinstance(conv_data, list) or not conv_data:
                continue

            conv = conv_data[0]
            convention_line = conv.get("conventionLine", {})

            # Detect utility type from subcontracts
            utility_label = sc_code
            if subcontracts and isinstance(subcontracts, list):
                for s in subcontracts:
                    if isinstance(s, dict) and s.get("accountContract") == sc_code:
                        ut = s.get("utilityType", "")
                        utility_label = UTILITY_TYPE_LABEL.get(ut, sc_code)
                        break

            # Detect unit of measurement from agreement (normalized)
            um_raw = conv.get("unitMeasure", "")
            unit = UNIT_NORMALIZE.get(um_raw, um_raw) if um_raw else "m³"

            attributes[f"──── {utility_label} ────"] = ""

            for key, month in CONVENTION_MONTH_MAPPING.items():
                value = convention_line.get(key, 0)
                attributes[f"{utility_label} — {month}"] = f"{value} {unit}"

            # Additional agreement data
            if conv.get("fromDate"):
                attributes[f"{utility_label} — Valid from"] = conv["fromDate"]

            if conv.get("validUntil"):
                attributes[f"{utility_label} — Valid until"] = conv["validUntil"]

            price_data = conv.get("accountContractPrice")
            if isinstance(price_data, dict):
                if price_data.get("contractualPrice") is not None:
                    attributes[f"{utility_label} — Contractual price"] = f"{price_data['contractualPrice']} lei"
                if price_data.get("pcs") is not None:
                    attributes[f"{utility_label} — PCS"] = str(price_data["pcs"])

        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# IndexArchiveSensor
# ──────────────────────────────────────────────
class IndexArchiveSensor(EonEnergyEntity):
    """Sensor for displaying historical meter index data."""

    def __init__(self, coordinator, config_entry, year):
        super().__init__(coordinator, config_entry)
        self.year = year
        um = coordinator.data.get("um", "m3") if coordinator.data else "m3"
        is_gaz = um.lower().startswith("m")
        self._attr_name = f"{year} → Gas Index Archive" if is_gaz else f"{year} → Electricity Index Archive"
        self._attr_icon = "mdi:clipboard-text-clock" if is_gaz else "mdi:clipboard-text-clock-outline"
        self._attr_translation_key = "gas_index_archive" if is_gaz else "electricity_index_archive"
        self._attr_unique_id = f"{DOMAIN}_index_archive_{self._cod_incasare}_{year}"
        self._custom_entity_id = (
            f"sensor.{DOMAIN}_{self._cod_incasare}_gas_index_archive_{year}"
            if is_gaz
            else f"sensor.{DOMAIN}_{self._cod_incasare}_electricity_index_archive_{year}"
        )

    @property
    def native_value(self):
        arhiva_data = self.coordinator.data.get("meter_history", {}) if self.coordinator.data else {}
        history_list = arhiva_data.get("history", [])
        year_data = next((y for y in history_list if y.get("year") == self.year), None)
        if not year_data:
            return None
        meters = year_data.get("meters", [])
        if not meters:
            return 0
        indexes = meters[0].get("indexes", [])
        if not indexes:
            return 0
        readings = indexes[0].get("readings", [])
        return len(readings)

    @property
    def extra_state_attributes(self):
        arhiva_data = self.coordinator.data.get("meter_history", {}) if self.coordinator.data else {}
        history_list = arhiva_data.get("history", [])
        year_data = next((y for y in history_list if y.get("year") == self.year), None)
        if not year_data:
            return {}
        unit = self.coordinator.data.get("um", "m3") if self.coordinator.data else "m3"
        attributes = {}
        readings_list = []
        for meter in year_data.get("meters", []):
            for index in meter.get("indexes", []):
                for reading in index.get("readings", []):
                    month_num = reading.get("month")
                    month_name = MONTHS_NUM_RO.get(month_num, "Unknown")
                    value = int(reading.get("value", 0))
                    reading_type_code = reading.get("readingType", "99")
                    reading_type_str = READING_TYPE_MAP.get(reading_type_code, "Unknown")
                    readings_list.append((month_num, reading_type_str, month_name, value))
        readings_list.sort(key=lambda r: r[0])
        for _, reading_type_str, month_name, value in readings_list:
            attributes[f"Index ({reading_type_str}) {month_name}"] = f"{value} {unit}"
        attributes["attribution"] = ATTRIBUTION
        return attributes


# ──────────────────────────────────────────────
# PaymentArchiveSensor
# ──────────────────────────────────────────────
class PaymentArchiveSensor(EonEnergyEntity):
    """Sensor for displaying payment history (grouped by year)."""

    _attr_icon = "mdi:cash-register"
    _attr_translation_key = "payment_archive"

    def __init__(self, coordinator, config_entry, year):
        super().__init__(coordinator, config_entry)
        self.year = year
        self._attr_name = f"{year} → Payment Archive"
        self._attr_unique_id = f"{DOMAIN}_payment_archive_{self._cod_incasare}_{year}"
        self._custom_entity_id = f"sensor.{DOMAIN}_{self._cod_incasare}_payment_archive_{year}"

    @property
    def native_value(self):
        return len(self._payments_for_year())

    @property
    def extra_state_attributes(self):
        attributes = {}
        payments_list = sorted(
            self._payments_for_year(),
            key=lambda p: int(p["paymentDate"][5:7]),
        )
        total_value = sum(p.get("value", 0) for p in payments_list)
        for idx, payment in enumerate(payments_list, start=1):
            raw_date = payment.get("paymentDate", "N/A")
            payment_value = payment.get("value", 0)
            if raw_date != "N/A":
                try:
                    parsed_date = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S")
                    month_name = MONTHS_NUM_RO.get(parsed_date.month, "unknown")
                except ValueError:
                    month_name = "unknown"
            else:
                month_name = "unknown"
            attributes[f"Payment {idx} invoice {month_name}"] = f"{format_ron(payment_value)} lei"
        attributes["Payments made"] = len(payments_list)
        attributes["Total amount"] = f"{format_ron(total_value)} lei"
        attributes["attribution"] = ATTRIBUTION
        return attributes

    def _payments_for_year(self) -> list:
        all_payments = self.coordinator.data.get("payments", []) if self.coordinator.data else []
        return [p for p in all_payments if p.get("paymentDate", "").startswith(str(self.year))]



# ──────────────────────────────────────────────
# ConsumptionArchiveSensor
# ──────────────────────────────────────────────
class ConsumptionArchiveSensor(EonEnergyEntity):
    """Sensor for displaying historical consumption data."""

    def __init__(self, coordinator, config_entry, year, monthly_values):
        super().__init__(coordinator, config_entry)
        self._year = year
        self._monthly_values = monthly_values
        um = coordinator.data.get("um", "m3") if coordinator.data else "m3"
        is_gaz = um.lower().startswith("m")
        self._attr_name = f"{year} → Gas Consumption Archive" if is_gaz else f"{year} → Electricity Consumption Archive"
        self._attr_icon = "mdi:chart-bar" if is_gaz else "mdi:lightning-bolt"
        self._attr_translation_key = "gas_consumption_archive" if is_gaz else "electricity_consumption_archive"
        self._attr_unique_id = f"{DOMAIN}_consumption_archive_{self._cod_incasare}_{year}"
        self._custom_entity_id = (
            f"sensor.{DOMAIN}_{self._cod_incasare}_gas_consumption_archive_{year}"
            if is_gaz
            else f"sensor.{DOMAIN}_{self._cod_incasare}_electricity_consumption_archive_{year}"
        )

    @property
    def native_value(self):
        total = sum(v["consumptionValue"] for v in self._monthly_values.values())
        return round(total, 2)

    @property
    def native_unit_of_measurement(self):
        return None

    @property
    def extra_state_attributes(self):
        unit = self.coordinator.data.get("um", "m3") if self.coordinator.data else "m3"
        attributes = {"attribution": ATTRIBUTION}
        attributes.update(
            {
                f"Monthly consumption {MONTHS_NUM_RO.get(int(month), 'unknown')}": f"{format_number_ro(value['consumptionValue'])} {unit}"
                for month, value in sorted(self._monthly_values.items(), key=lambda item: int(item[0]))
            }
        )
        attributes["────"] = ""
        attributes.update(
            {
                f"Average daily consumption in {MONTHS_NUM_RO.get(int(month), 'unknown')}": f"{format_number_ro(value['consumptionValueDayValue'])} {unit}"
                for month, value in sorted(self._monthly_values.items(), key=lambda item: int(item[0]))
            }
        )
        return attributes
