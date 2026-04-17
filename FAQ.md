<a name="top"></a>
# Frequently Asked Questions

- [How do I add the integration to Home Assistant?](#how-do-i-add-the-integration-to-home-assistant)
- [I have a DUO account. Can I use the integration?](#i-have-a-duo-account-can-i-use-the-integration)
- [What sensors do I get for a DUO contract?](#what-sensors-do-i-get-for-a-duo-contract)
- [What does "current index" mean?](#what-does-current-index-mean)
- [The current index is not showing. Why?](#the-current-index-is-not-showing-why)
- [The "Reading allowed" sensor is not showing. Why?](#the-reading-allowed-sensor-is-not-showing-why)
- [The "Reading allowed" sensor shows "No" even though I'm in the reading period. Why?](#the-reading-allowed-sensor-shows-no-even-though-im-in-the-reading-period-why)
- [What does the "Prosumer invoice" sensor mean?](#what-does-the-prosumer-invoice-sensor-mean)
- [I'm not a prosumer. The prosumer sensor shows "No" — is that normal?](#im-not-a-prosumer-the-prosumer-sensor-shows-no--is-that-normal)
- [What does the "Invoice balance" sensor mean?](#what-does-the-invoice-balance-sensor-mean)
- [Why do entities have a long name with the billing code included?](#why-do-entities-have-a-long-name-with-the-billing-code-included)
- [Can I monitor multiple contracts simultaneously?](#can-i-monitor-multiple-contracts-simultaneously)
- [I want to submit the index automatically. What do I need?](#i-want-to-submit-the-index-automatically-what-do-i-need)
- [I have a gas meter reader. How do I set up the automation?](#i-have-a-gas-meter-reader-how-do-i-set-up-the-automation)
- [Why are values displayed with dots and commas (1.234,56)?](#why-are-values-displayed-with-dots-and-commas-123456)
- [I changed the integration options. Do I need to restart?](#i-changed-the-integration-options-do-i-need-to-restart)
- [Do I need to delete and re-add the integration when updating?](#do-i-need-to-delete-and-re-add-the-integration-when-updating)
- [I like the project. How can I support it?](#i-like-the-project-how-can-i-support-it)

---

## How do I add the integration to Home Assistant?

[↑ Back to top](#top)

You need HACS (Home Assistant Community Store) installed. If you don't have it, follow the [official HACS guide](https://hacs.xyz/docs/use).

1. In Home Assistant, go to **HACS** → the **three dots** in the top right → **Custom repositories**.
2. Enter the URL: `https://github.com/Alfy1080/eon` and select the type **Integration**.
3. Click **Add**, then search for **E-ON Energy** in HACS and install.
4. Restart Home Assistant.
5. Go to **Settings** → **Devices & Services** → **Add Integration** → search for **E-ON Energy** and follow the configuration steps.

Full details in [SETUP.md](./SETUP.md).

---

## I have a DUO account. Can I use the integration?

[↑ Back to top](#top)

Yes. The integration automatically detects collective/DUO contracts and handles them accordingly.

Here's how:

1. Add the integration with your E-ON Myline account email and password.
2. At step 2 (contract selection), you will see all contracts with complete addresses — including the DUO contract labeled with `(Collective/DUO)`.
3. Select it.

The integration will:
- Automatically discover subcontracts (gas + electricity) via the `account-contracts/list` endpoint
- Fetch details, meter index, and consumption convention **per subcontract**, in parallel
- Create dedicated sensors per subcontract (Gas index, Electricity index, Gas reading allowed, Electricity reading allowed)
- Display all DUO details in Contract Data: subcontracts, prices, OD, NLC, POD, meter readings

---

## What sensors do I get for a DUO contract?

[↑ Back to top](#top)

A DUO contract generates:

**Base sensors** (on the collective contract):
- Contract data — with detailed attributes per subcontract (gas + electricity)
- Invoice balance, Prosumer balance, Overdue invoice, Prosumer invoice
- Consumption agreement — with monthly values per utility (gas separate, electricity separate)

**Per-subcontract sensors** (on the individual gas and electricity codes):
- Gas index / Electricity index — the index value per subcontract
- Gas reading allowed / Electricity reading allowed — the reading period status per subcontract

Entity IDs for per-subcontract sensors use the subcontract code, not the collective code. Example: `sensor.eonenergy_002100234567_gas_index`.

---

## What does "current index" mean?

[↑ Back to top](#top)

It's the last read or submitted meter value — either by the distributor, by you (self-reading), or estimated by E-ON Energy. The term is generic and applies to both gas and electricity.

In the integration, the sensor is named **"Gas index"** or **"Electricity index"**, depending on the automatically detected contract type.

---

## The current index is not showing. Why?

[↑ Back to top](#top)

This is normal. The current index appears **only during the reading period** (usually a few days per month). When you are not in the reading period, the E-ON API returns an empty device list, so the integration has no data to extract.

Specifically, outside the reading period, the API response looks like this:
```json
{
    "readingPeriod": {
        "startDate": "2026-03-20",
        "endDate": "2026-03-28",
        "allowedReading": true,
        "inPeriod": false
    },
    "indexDetails": {
        "devices": []
    }
}
```

When the reading period arrives, `devices` is populated with meter data and the sensor displays its values. There is no issue with the integration — E-ON simply does not publish this data outside the reading period.

**Important note:** Index and reading allowed sensors are created when the integration starts. If the integration was started outside the reading period, the sensors will exist but will show `0` (index) or `No` (reading allowed). Data will populate automatically when the reading period begins, without requiring a restart.

---

## The "Reading allowed" sensor is not showing. Why?

[↑ Back to top](#top)

Same reason as the current index — the "Reading allowed" sensor depends on the same API data. If you are not in the reading period, the sensor will show **No** or have no available data. See the section [The current index is not showing](#the-current-index-is-not-showing-why) for details.

---

## The "Reading allowed" sensor shows "No" even though I'm in the reading period. Why?

[↑ Back to top](#top)

This was corrected in the current version. The sensor now uses the `readingPeriod.inPeriod` indicator directly from the API (most reliable), with fallback to `readingPeriod.allowedReading` and then to manual calculation with `startDate` / `endDate`.

If the sensor still shows "No" even though you are in the reading period:
1. Check the secondary attributes of the sensor — you should see "In reading period: Yes" and "Reading authorized: Yes"
2. If the attributes are missing, the E-ON API is not providing data for that contract — possibly an inactive contract
3. Enable debug logging ([DEBUG.md](DEBUG.md)) and check the response from the `meter_index` endpoint

---

## What does the "Prosumer invoice" sensor mean?

[↑ Back to top](#top)

This sensor monitors invoices associated with a **prosumer** contract (people who have solar panels or other generation sources and are connected to the grid).

The entity ID for this sensor is `sensor.eonenergy_{code}_prosumer_invoice`.

The difference from the normal "Overdue invoice" sensor:
- **Overdue invoice** — shows only if you have debts on your regular consumption account.
- **Prosumer invoice** — shows both **debts** and **credits** from the prosumer contract. If you produced more than you consumed, you will see a credit. The sensor also displays information about the overall balance, refund availability, and whether a refund is in progress.

---

## I'm not a prosumer. The prosumer sensor shows "No" — is that normal?

[↑ Back to top](#top)

Absolutely normal. If you don't have a prosumer contract, the E-ON API does not return data for this endpoint, and the sensor will show **No** with the attribute "No invoices available". You can ignore it or hide it from your dashboard.

---

## What does the "Invoice balance" sensor mean?

[↑ Back to top](#top)

The "Invoice balance" sensor (`sensor.eonenergy_{code}_invoice_balance`) indicates whether you have an active payment balance:

- **Yes** — you have an amount to pay (debt). Check the attributes for details.
- **No** — you have no payment balance (zero or credit).

The attributes include:

- **Balance** — the total amount to pay or credit (Romanian format: 1.234,56 lei)
- **Payment balance** — Yes/No (indicates if you need to pay)
- **Refund available** — Yes/No (if you can request a refund)
- **Active guarantee** — Yes/No
- **Balance date** — the date the balance was calculated

Boolean values (true/false) are automatically translated to Yes/No, and amounts are displayed in Romanian format.

---

## Why do entities have a long name with the billing code included?

[↑ Back to top](#top)

The integration manually sets the `entity_id` for each entity, including the billing code and contract type. The general format is:

- `sensor.eonenergy_{billing_code}_{sensor_type}`
- `button.eonenergy_{billing_code}_{button_type}`

For example, for a gas contract with code `004412345678`:
- `sensor.eonenergy_004412345678_gas_index`
- `sensor.eonenergy_004412345678_contract_data`
- `sensor.eonenergy_004412345678_invoice_balance`
- `button.eonenergy_004412345678_submit_gas_index`

The main advantage: if you have multiple contracts monitored simultaneously, each entity has a unique ID without conflicts.

---

## Can I monitor multiple contracts simultaneously?

[↑ Back to top](#top)

Yes. The integration supports **multi-contract**. A single E-ON account can monitor as many billing codes as you want, including DUO contracts.

During the configuration step, you select the desired contracts (or select all). Each contract generates a separate device with its own sensors, and data is updated in parallel.

---

## I want to submit the index automatically. What do I need?

[↑ Back to top](#top)

Two things:

**1. Hardware on the meter** — A sensor capable of reading meter pulses (reed / magnetic contact, typically). It must be compatible with your meter and not require permanent modifications. The sensor sends pulses to Home Assistant, where they are converted into a numeric value stored in `input_number`.

**2. Integration configured** — The index submission buttons in the integration read the value from the corresponding `input_number` and send it to the E-ON API:

- **Gas**: the button `Submit gas index` (`button.eonenergy_{code}_submit_gas_index`) reads from `input_number.gas_meter_reading`
- **Electricity**: the button `Submit electricity index` (`button.eonenergy_{code}_submit_electricity_index`) reads from `input_number.energy_meter_reading`

For DUO contracts, both buttons are created automatically (one per subcontract). For individual contracts, only one button appears corresponding to the utility type.

> **Note:** The buttons look for exactly the entities `input_number.gas_meter_reading` and/or `input_number.energy_meter_reading`. If these don't exist or have invalid values, the submission will fail. Check the logs if you encounter problems.

---

## I have a gas meter reader. How do I set up the automation?

[↑ Back to top](#top)

If you have the hardware installed and the value is updated in `input_number.gas_meter_reading`, you can use an automation like this:

```yaml
alias: "GAS: Automatic index submission"
description: >-
  Sends a notification in the morning and presses the submit button at noon,
  on the 9th day of each month.
triggers:
  - trigger: time
    at: "09:00:00"
  - trigger: time
    at: "12:00:00"
conditions:
  - condition: template
    value_template: "{{ now().day == 9 }}"
actions:
  - choose:
      - alias: "Notification at 09:00"
        conditions:
          - condition: template
            value_template: "{{ trigger.now.hour == 9 }}"
        sequence:
          - action: notify.mobile_app_my_phone
            data:
              title: "E-ON GAS — Index to submit"
              message: >-
                The new index for the current month is
                {{ states('input_number.gas_meter_reading') | float | round(0) | int }}.
      - alias: "Submit index at 12:00"
        conditions:
          - condition: template
            value_template: "{{ trigger.now.hour == 12 }}"
        sequence:
          - action: button.press
            target:
              entity_id: button.eonenergy_004412345678_submit_gas_index
```

**What it does:**
- On the **9th day** of each month, at **09:00**, you receive a notification with the current index.
- At **12:00**, the integration automatically submits the index to E-ON.

> **⚠️ Important:** Replace `004412345678` with your real billing code (12 digits) and `notify.mobile_app_my_phone` with your notification service entity_id. You can find exact entity_ids in **Settings** → **Devices & Services** → **E-ON Energy**.

---

## Why are values displayed with dots and commas (1.234,56)?

[↑ Back to top](#top)

The integration uses Romanian numeric format: the dot separates thousands, the comma separates decimals. Example: **1.234,56 lei** means one thousand two hundred thirty-four lei and fifty-six bani. This is the standard format used in Romania.

Also, in the "Consumption archive" sensor, consumption and daily average values use the comma as a decimal separator (e.g., **4,029 m³** instead of **4.029 m³**), to avoid confusion with the thousands separator.

---

## I changed the integration options. Do I need to restart?

[↑ Back to top](#top)

No. The integration automatically reloads when you save changes from the options flow. A manual restart of Home Assistant is not necessary.

Also, if you modify the credentials (username, password) from the options, the integration validates authentication before saving — if the new data is incorrect, you will receive an error and the existing configuration remains unchanged.

---

## Do I need to delete and re-add the integration when updating?

[↑ Back to top](#top)

Generally no. Settings are stored in the HA database, not in files. The update only overwrites the code.

**Exception v3.0.0:** If you update from v1/v2 to v3, the integration includes automatic migration that converts the old format (a single billing code) to the new format (list of contracts). You don't need to do anything manually. If issues arise, delete the integration and re-add it.

---

## I like the project. How can I support it?

[↑ Back to top](#top)

- ⭐ Give a **star** on [GitHub](https://github.com/Alfy1080/eon/)
- 🐛 **Report issues** — open an [issue](https://github.com/Alfy1080/eon/issues)
- 🔀 **Contribute code** — submit a pull request
- ☕ **Donate** via [Buy Me a Coffee](https://buymeacoffee.com/Alfy1080)
- 📢 **Share** the project with friends or your community
