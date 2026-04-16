# Installation and Configuration Guide — E-ON Energy

This guide covers every step of installing and configuring the E-ON Energy integration for Home Assistant. If anything is unclear, open an issue on GitHub.

---

## Prerequisites

Before you begin, make sure you have:

- **Home Assistant** version 2024.x or newer (requires `entry.runtime_data` pattern)
- **Active E-ON Myline account** — with a working email and password on the E-ON Myline mobile app
- **Valid license** — from licensing-server.com/donate?ref=eonromania
- **HACS** installed (optional, but recommended) — HACS instructions

---

## Method 1: Installation via HACS (recommended)

### Step 1 — Add the custom repository

1. Open Home Assistant → sidebar → **HACS**
2. Click on the 3 dots (⋮) in the top right corner
3. Select **Custom repositories**
4. In the "Repository" field, enter: `https://github.com/developer/ha-eon-romania`
5. In the "Category" field, select: **Integration**
6. Click **Add**

### Step 2 — Install the integration

1. In HACS, search for "**E-ON Energy**"
2. Click on the result → **Download** (or **Install**)
3. Confirm the installation

### Step 3 — Restart Home Assistant

1. **Settings** → **System** → **Restart**
2. Or from the terminal: `ha core restart`

**Wait**: the restart takes 1–3 minutes. Do not proceed until the dashboard is fully loaded.

---

## Method 2: Manual installation

### Step 1 — Download the files

1. Go to Releases on GitHub
2. Download the latest version (zip or tar.gz)
3. Unzip it

### Step 2 — Copy the folder

Copy the entire `custom_components/eonromania/` folder into your Home Assistant configuration directory:

```
config/
└── custom_components/
    └── eonromania/
        ├── __init__.py
        ├── api.py
        ├── button.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── helpers.py
        ├── manifest.json
        ├── sensor.py
        ├── strings.json
        └── translations/
            └── ro.json
```

**Note**: the folder must be exactly `eonromania` (lowercase, no spaces).

If the `custom_components` folder does not exist, create it.

### Step 3 — Restart Home Assistant

Same as in Method 1.

---

## Initial Configuration

### Step 1 — Add the integration

1. **Settings** → **Devices & Services**
2. Click **+ Add Integration** (the blue button, bottom right)
3. Search for "**E-ON Energy**" — "E-ON Energy" will appear
4. Click on it

### Step 2 — Fill in the authentication form

You will see a form with 3 fields:

#### Field 1: Email address

- **What it does**: the email address for your E-ON Myline account
- **Format**: valid email (e.g., `user@example.com`)
- **Note**: it is also the unique identifier for the integration — you cannot add the same email twice

#### Field 2: Password

- **What it does**: the password for your E-ON Myline account
- **Note**: stored encrypted in the HA database

#### Field 3: Update interval (seconds)

- **What it does**: how often the data is refreshed from the API
- **Default**: `3600` (1 hour)
- **Recommendation**: leave it at 3600. E-ON data does not change frequently. Values below 600 seconds are not recommended.

### Step 3 — Select contracts

After successful authentication, contracts are automatically discovered. You will see a list of all contracts associated with the account, with full normalized addresses:

```
15 Flower Street, apt. 8, Cluj-Napoca, Cluj County ➜ RO123456789012 (Gas)
42 Independence Boulevard, Brașov, Brașov County ➜ RO987654321098 (Collective/DUO)
```

You have two options:
- **Individual selection** — check only the desired contracts
- **Select all** — check "Select all contracts"

**Note**: if you do not select any contract and do not check "all", you will get an error: "Please select at least one contract to continue."

**DUO Contracts**: collective contracts appear with the `(Collective/DUO)` label. When selected, the integration automatically discovers the subcontracts (gas + electricity) and creates dedicated sensors for each.

### Step 4 — License

The integration requires a **valid license** to function. Without a license:
- Only the `sensor.eonromania_{nlc}_license` sensor with the value "License required" is created
- All normal sensors and buttons are disabled

To enter the license:
1. **Settings** → **Devices & Services**
2. Find **E-ON Energy** → click on **Configure**
3. Select **License**
4. Enter the license key
5. Click **Save**

Licenses available at: licensing-server.com/license/eonromania

### Step 5 — Confirm

Click **Save**. The integration will install and create:
- 1 device per selected contract
- Sensors + index submission buttons per device (1 button for an individual contract, 2 buttons for DUO)

The first update takes a few seconds (API query for all endpoints per contract, in parallel).

---

## Reconfiguration (without reinstallation)

All settings can be modified from the UI, without deleting and re-adding the integration.

1. **Settings** → **Devices & Services**
2. Find **E-ON Energy** → click on **Configure** (⚙️)
3. Fill in the email, password, and interval again
4. In the next step, you can modify the contract selection
5. Click **Save**
6. The integration will automatically reload (no restart needed)

**Note**: upon reconfiguration, contracts are rediscovered. If new contracts have appeared, you will see them in the list.

**Validation**: if you modify the credentials and the new data is incorrect, you will receive an error and the existing configuration will remain unchanged.

---

## Quick Reference — Entity IDs

### Common sensors (gas and electricity):

| Sensor | Entity ID |
|---|---|
| Contract data | `sensor.eonromania_{billing_code}_contract_data` |
| Invoice balance | `sensor.eonromania_{billing_code}_invoice_balance` |
| Prosumer balance | `sensor.eonromania_{billing_code}_prosumer_balance` |
| Reading allowed | `sensor.eonromania_{billing_code}_reading_allowed` |
| Consumption agreement | `sensor.eonromania_{billing_code}_consumption_agreement` |
| Overdue invoice | `sensor.eonromania_{billing_code}_overdue_invoice` |
| Prosumer invoice | `sensor.eonromania_{billing_code}_prosumer_invoice` |
| Payment archive (year) | `sensor.eonromania_{billing_code}_payment_archive_{year}` |
| Submit gas index | `button.eonromania_{billing_code}_submit_gas_index` |
| Submit electricity index | `button.eonromania_{billing_code}_submit_electricity_index` |

### Contract-type specific sensors:

| Sensor | Entity ID (gas) | Entity ID (electricity) |
|---|---|---|
| Index | `…_{billing_code}_gas_index` | `…_{billing_code}_electricity_index` |
| Consumption archive (year) | `…_{billing_code}_gas_consumption_archive_{year}` | `…_{billing_code}_electricity_consumption_archive_{year}` |
| Index archive (year) | `…_{billing_code}_gas_index_archive_{year}` | `…_{billing_code}_electricity_index_archive_{year}` |

### DUO sensors (per subcontract):

| Sensor | Entity ID |
|---|---|
| Gas index (subcontract) | `sensor.eonromania_{subcontract_code}_gas_index` |
| Electricity index (subcontract) | `sensor.eonromania_{subcontract_code}_electricity_index` |
| Gas reading allowed | `sensor.eonromania_{subcontract_code}_reading_allowed` |
| Electricity reading allowed | `sensor.eonromania_{subcontract_code}_reading_allowed` |
| Submit gas index (subcontract) | `button.eonromania_{subcontract_code}_submit_gas_index` |
| Submit electricity index (subcontract) | `button.eonromania_{subcontract_code}_submit_electricity_index` |

---

## Preparing the Submit Index buttons

The index submission buttons require a manually defined `input_number` for each utility type. Add the following to your `configuration.yaml`:

### For gas

```yaml
input_number:
  gas_meter_reading:
    name: Gas meter index
    min: 0
    max: 999999
    step: 1
    mode: box
```

### For electricity

```yaml
input_number:
  energy_meter_reading:
    name: Electricity meter index
    min: 0
    max: 999999
    step: 1
    mode: box
```

> **DUO:** If you have a DUO contract, define **both** `input_number` entities (gas + electricity).

Restart HA after adding them. The buttons look for the exact entities `input_number.gas_meter_reading` and `input_number.energy_meter_reading`.

---

## Lovelace Card Examples

### General card — all entities

```yaml
type: entities
title: E-ON Energy
entities:
  - entity: sensor.eonromania_ro123456789012_contract_data
  - entity: sensor.eonromania_ro123456789012_invoice_balance
  - entity: sensor.eonromania_ro123456789012_gas_index
  - entity: sensor.eonromania_ro123456789012_reading_allowed
  - entity: sensor.eonromania_ro123456789012_consumption_agreement
  - entity: sensor.eonromania_ro123456789012_overdue_invoice
  - entity: sensor.eonromania_ro123456789012_prosumer_invoice
  - entity: button.eonromania_ro123456789012_submit_gas_index
```

### Card — Invoice balance

```yaml
type: entities
title: Invoice Balance
entities:
  - entity: sensor.eonromania_ro123456789012_invoice_balance
    name: Balance
  - type: attribute
    entity: sensor.eonromania_ro123456789012_invoice_balance
    attribute: Sold de plată
    name: To pay
  - type: attribute
    entity: sensor.eonromania_ro123456789012_invoice_balance
    attribute: Rambursare disponibilă
    name: Refund
```

### Card — Overdue invoice

```yaml
type: entities
title: Overdue Invoices
entities:
  - entity: sensor.eonromania_ro123456789012_overdue_invoice
    name: Overdue invoice
  - type: attribute
    entity: sensor.eonromania_ro123456789012_overdue_invoice
    attribute: Total neachitat
    name: Total unpaid
```

### Card — Submit gas index with input_number

```yaml
type: vertical-stack
title: Submit Gas Index
cards:
  - type: entities
    entities:
      - entity: input_number.gas_meter_reading
        name: Index to submit
      - entity: sensor.eonromania_ro123456789012_reading_allowed
        name: Reading allowed
  - type: button
    entity: button.eonromania_ro123456789012_submit_gas_index
    name: Submit gas index
    icon: mdi:fire
    tap_action:
      action: toggle
```

### Card — Submit electricity index with input_number

```yaml
type: vertical-stack
title: Submit Electricity Index
cards:
  - type: entities
    entities:
      - entity: input_number.energy_meter_reading
        name: Index to submit
      - entity: sensor.eonromania_ro345678901234_reading_allowed
        name: Electricity reading allowed
  - type: button
    entity: button.eonromania_ro345678901234_submit_electricity_index
    name: Submit electricity index
    icon: mdi:flash
    tap_action:
      action: toggle
```

### Conditional card — Overdue invoice alert

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.eonromania_ro123456789012_overdue_invoice
    state: "Yes"
card:
  type: markdown
  content: >-
    ## ⚠️ You have an overdue invoice!

    **Total unpaid:** {{ state_attr('sensor.eonromania_ro123456789012_overdue_invoice', 'Total neachitat') }}

    Check the details in the Invoices section of the dashboard.
```

---

## Post-installation check

### Check that the devices exist

1. **Settings** → **Devices & Services** → click on **E·ON Energy**
2. You should see one device per selected contract (e.g., "E-ON Energy (RO123456789012)")

### Check the sensors

1. **Developer Tools** → **States**
2. Filter by `eonromania`
3. You should see the entities with values (e.g., `Yes`, `No`, `6030`, etc.)

### Check the logs (if something is not working)

1. **Settings** → **System** → **Logs**
2. Search for messages with `eonromania`
3. For details, enable debug logging — see DEBUG.md

---

## Uninstallation

### Via HACS

1. HACS → find "E-ON Energy" → **Remove**
2. Restart Home Assistant

### Manual

1. **Settings** → **Devices & Services** → E-ON Energy → **Delete**
2. Delete the `config/custom_components/eonromania/` folder
3. Restart Home Assistant

---

## General Notes

- **Replace `RO123456789012`** with your real 12-digit billing code in all the examples above.
- **Entity IDs are manually set** by the integration based on the billing code and contract type. Consult the reference table at the beginning of the cards section.
- **Attributes appear only when E-ON Energy provides the data.** If an attribute is not visible, it means the API did not return that information — it is not an error.
- **Index and reading allowed sensors** show data only during the reading period. Otherwise, they display `0` or `No`.
- **DUO contracts** generate index and reading allowed sensors per subcontract, with entity IDs based on the subcontract code, not the collective code.
- If you encounter problems, consult DEBUG.md to enable detailed logging.