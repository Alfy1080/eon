"""Module for managing buttons in the E-ON Energy integration."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LICENSE_DATA_KEY
from .coordinator import EonRomaniaCoordinator
from .helpers import (
    UTILITY_BUTTON_CONFIG,
    detect_utility_type_individual,
    extract_ablbelnr,
    get_meter_data,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up buttons for the given config_entry."""
    # License check
    mgr = hass.data.get(DOMAIN, {}).get(LICENSE_DATA_KEY)
    is_license_valid = mgr.is_valid if mgr else False
    if not is_license_valid:
        _LOGGER.debug(
            "Button platform for %s not initialized — invalid license (entry_id=%s).",
            DOMAIN,
            config_entry.entry_id,
        )
        return

    _LOGGER.debug(
        "Initializing button platform for %s (entry_id=%s).",
        DOMAIN,
        config_entry.entry_id,
    )

    entities: list[ButtonEntity] = []

    for cod_incasare, coordinator in config_entry.runtime_data.coordinators.items():
        # Account without contracts → no meters, no buttons created
        if coordinator.account_only:
            _LOGGER.debug(
                "Coordinator account_only (%s) — no buttons created.", cod_incasare,
            )
            continue

        if coordinator.is_collective:
            # ── Contract colectiv/DUO: un buton per subcontract ──
            subcontracts_list = coordinator.data.get("subcontracts") if coordinator.data else None

            if subcontracts_list and isinstance(subcontracts_list, list):
                for s in subcontracts_list:
                    if not isinstance(s, dict):
                        continue
                    sc_code = s.get("accountContract")
                    utility_type = s.get("utilityType")
                    if not sc_code or not utility_type:
                        continue

                    btn_config = UTILITY_BUTTON_CONFIG.get(utility_type)
                    if not btn_config:
                        _LOGGER.warning(
                            "Tip utilitate necunoscut '%s' pentru subcontract %s (DUO %s). Buton ignorat.",
                            utility_type, sc_code, cod_incasare,
                        )
                        continue

                    entities.append(
                        TrimiteIndexButton(
                            coordinator=coordinator,
                            config_entry=config_entry,
                            account_contract=sc_code,
                            utility_type=utility_type,
                            is_subcontract=True,
                        )
                    )
                    _LOGGER.debug(
                        "Buton DUO creat: %s → %s (contract_principal=%s).",
                        btn_config["label"], sc_code, cod_incasare,
                    )
            else:
                _LOGGER.warning(
                    "DUO contract without available subcontracts (contract=%s). No buttons created.",
                    cod_incasare,
                )
        else:
            # ── Contract individual: un singur buton ──
            utility_type = detect_utility_type_individual(coordinator.data)
            btn_config = UTILITY_BUTTON_CONFIG.get(utility_type)
            if not btn_config:
                _LOGGER.warning(
                    "Unknown utility type '%s' for individual contract %s. Using gas fallback.",
                    utility_type, cod_incasare,
                )
                utility_type = "02"

            entities.append(
                TrimiteIndexButton(
                    coordinator=coordinator,
                    config_entry=config_entry,
                    account_contract=cod_incasare,
                    utility_type=utility_type,
                    is_subcontract=False,
                )
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.debug(
            "Platforma button: %s butoane create (entry_id=%s).",
            len(entities), config_entry.entry_id,
        )


class TrimiteIndexButton(CoordinatorEntity[EonRomaniaCoordinator], ButtonEntity):
    """Button for submitting meter index — supports both individual and DUO contracts."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: EonRomaniaCoordinator,
        config_entry: ConfigEntry,
        account_contract: str,
        utility_type: str,
        is_subcontract: bool = False,
    ):
        """Initialize the button.

        Args:
            coordinator: Coordinatorul E·ON pentru contractul principal.
            config_entry: Intrarea de configurare.
            account_contract: Billing code (main contract or subcontract).
            utility_type: Utility type ("01" = electricity, "02" = gas).
            is_subcontract: True if the button is for a DUO subcontract.
        """
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._account_contract = account_contract
        self._utility_type = utility_type
        self._is_subcontract = is_subcontract
        self._cod_incasare = coordinator.cod_incasare  # contractul principal (pentru device)

        # Configuration from mapping
        btn_config = UTILITY_BUTTON_CONFIG.get(utility_type, UTILITY_BUTTON_CONFIG["02"])
        self._input_number_entity = btn_config["input_number"]
        self._attr_name = btn_config["label"]
        self._attr_icon = btn_config["icon"]
        self._attr_translation_key = btn_config["translation_key"]

        # Entity ID and unique_id
        self._attr_unique_id = f"{DOMAIN}_trimite_index_{account_contract}"
        self._custom_entity_id = f"button.{DOMAIN}_{account_contract}_{btn_config['suffix']}"

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
            manufacturer="Ciprian Nicolae (cnecrea)",
            model="E-ON Energy",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self):
        """Execute meter index submission."""
        ac = self._account_contract
        utility_label = UTILITY_BUTTON_CONFIG.get(self._utility_type, {}).get("label", "necunoscut")

        try:
            # 1. Read value from input_number
            input_state = self.hass.states.get(self._input_number_entity)
            if not input_state:
                _LOGGER.error(
                    "Entity %s does not exist. Cannot submit index "
                    "(contract=%s, tip=%s).",
                    self._input_number_entity, ac, utility_label,
                )
                return

            try:
                index_value = int(float(input_state.state))
            except (TypeError, ValueError):
                _LOGGER.error(
                    "Invalid value for %s: '%s' (contract=%s, type=%s).",
                    self._input_number_entity, input_state.state,
                    ac, utility_label,
                )
                return

            # 2. Get meter data (ablbelnr)
            meter_data = get_meter_data(
                self.coordinator.data, ac, is_subcontract=self._is_subcontract
            )
            ablbelnr = extract_ablbelnr(meter_data)

            if not ablbelnr:
                _LOGGER.error(
                    "Internal meter ID (ablbelnr) not found. "
                    "Nu se poate trimite indexul (contract=%s, tip=%s).",
                    ac, utility_label,
                )
                return

            _LOGGER.debug(
                "Se trimite indexul: valoare=%s (contract=%s, tip=%s, ablbelnr=%s).",
                index_value, ac, utility_label, ablbelnr,
            )

            # 3. Build payload and submit
            indexes_payload = [
                {
                    "ablbelnr": ablbelnr,
                    "indexValue": index_value,
                }
            ]

            result = await self.coordinator.api_client.async_submit_meter_index(
                account_contract=ac,
                indexes=indexes_payload,
            )

            if result is None:
                _LOGGER.error(
                    "Index submission failed (contract=%s, type=%s).",
                    ac, utility_label,
                )
                return

            # 4. Refresh date
            await self.coordinator.async_request_refresh()

            _LOGGER.info(
                "Index trimis cu succes: valoare=%s (contract=%s, tip=%s).",
                index_value, ac, utility_label,
            )

        except Exception:
            _LOGGER.exception(
                "Unexpected error during index submission (contract=%s, type=%s).",
                ac, utility_label,
            )
