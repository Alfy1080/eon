# Debugging Guide — E-ON Energy

This guide explains how to enable detailed logging, what messages to look for, and how to interpret each situation.

---

## 1. Enable debug logging

Edit `configuration.yaml` and add:

```yaml
logger:
  default: warning
  logs:
    custom_components.eonenergy: debug
```

Restart Home Assistant (**Settings** → **System** → **Restart**).

To reduce noise in the logs, you can add:

```yaml
logger:
  default: warning
  logs:
    custom_components.eonenergy: debug
    homeassistant.const: critical
    homeassistant.loader: critical
    homeassistant.helpers.frame: critical
```

**Important**: disable debug logging after you've resolved the issue (set `custom_components.eonenergy: info` or delete the block). Debug logging generates a lot of text and may contain personal data.

---

## 2. Where to find the logs

### From the UI

**Settings** → **System** → **Logs** → filter by `eonenergy`

### From file

```bash
# Default path
cat config/home-assistant.log | grep -i eonenergy

# Errors only
grep -E "(ERROR|WARNING).*eonenergy" config/home-assistant.log

# Last 100 lines
grep -i eonenergy config/home-assistant.log | tail -100
```

### From terminal (Docker/HAOS)

```bash
# Docker
docker logs homeassistant 2>&1 | grep -i eonenergy

# Home Assistant OS (SSH add-on)
ha core logs | grep -i eonenergy
```

---

## 3. How to read API logs

Each API request is labeled with a **descriptive label** that includes the endpoint name and billing code. The format is:

```
[label] METHOD URL
[label] Response OK (200). Size: XXX characters.
```

### Example of a normal update cycle (individual contract)

```
[LOGIN] Token obtained successfully (expires_in=3600).
[contract_details (004412345678)] GET https://api2.eon.ro/partners/v2/account-contracts/004412345678?includeMeterReading=true
[contract_details (004412345678)] Response OK (200). Size: 1523 characters.
[invoice_balance (004412345678)] GET https://api2.eon.ro/invoices/v1/invoices/invoice-balance?accountContract=004412345678
[invoice_balance (004412345678)] Response OK (200). Size: 245 characters.
[meter_index (004412345678)] GET https://api2.eon.ro/meterreadings/v1/meter-reading/004412345678/index
[meter_index (004412345678)] Response OK (200). Size: 892 characters.
[payments (004412345678)] Page 1: 10 items, has_next=true.
[payments (004412345678)] Page 2: 3 items, has_next=false.
[payments (004412345678)] Total: 13 items from 2 pages.
```

### Example of a normal update cycle (DUO contract)

```
Collective/DUO contract detected (contract=009900123456). Querying subcontracts via list?collectiveContract.
[contracts_list] Data received: type=list, len=2
DUO sub_codes (contract=009900123456): 2 codes → ['002100234567', '002200345678'].
[contract_details (002100234567)] Response OK (200). Size: 1823 characters.
[contract_details (002200345678)] Response OK (200). Size: 1456 characters.
[consumption_convention (002100234567)] Response OK (200). Size: 534 characters.
[consumption_convention (002200345678)] Response OK (200). Size: 478 characters.
[meter_index (002100234567)] Response OK (200). Size: 892 characters.
[meter_index (002200345678)] Response OK (200). Size: 756 characters.
DUO individual contract_details (contract=009900123456): 2/2 successful. Conventions: 2/2 successful. Meter index: 2/2 successful.
```

### Available labels

| Label | Endpoint | Associated sensor |
|-------|----------|-------------------|
| `LOGIN` | mobile-login | — (authentication) |
| `REFRESH` | mobile-refresh-token | — (token refresh) |
| `contracts_list` | account-contracts/list | — (config flow + DUO discovery) |
| `contract_details` | account-contracts/{code} | Contract data |
| `invoices_unpaid` | invoices/list | Overdue invoice |
| `invoices_prosum` | invoices/list-prosum | Prosumer invoice |
| `invoice_balance` | invoices/invoice-balance | Invoice balance |
| `invoice_balance_prosum` | invoices/invoice-balance-prosum | Prosumer balance |
| `rescheduling_plans` | rescheduling-plans | Rescheduling plans |
| `graphic_consumption` | invoices/graphic-consumption/{code} | Consumption archive |
| `meter_index` | meter-reading/{code}/index | Index + Reading allowed |
| `meter_history` | meter-reading/{code}/history | Index archive |
| `consumption_convention` | consumption-convention/{code} | Consumption agreement |
| `payments` | payments/payment-list | Payment archive |
| `submit_meter` | meter-reading/index (POST) | Submit index button |

**DUO note**: For collective contracts, `contract_details`, `consumption_convention` and `meter_index` are called per subcontract. In the logs you will see the subcontract code (e.g., `002100234567`), not the collective code.

---

## 4. Startup messages

At the first start of the integration (or after restart), you should see:

### Individual contract:
```
INFO  Setting up integration eonenergy (entry_id=01ABC...).
DEBUG Selected contracts: ['004412345678'], interval=3600s.
DEBUG [LOGIN] Token obtained successfully (expires_in=3600).
DEBUG Starting E-ON Energy data update (contract=004412345678, collective=False).
DEBUG E-ON Energy update complete (contract=004412345678, collective=False). Endpoints without data: 0/11.
INFO  1 active coordinators out of 1 selected contracts (entry_id=01ABC...).
```

### DUO contract:
```
INFO  Setting up integration eonenergy (entry_id=01ABC...).
DEBUG Selected contracts: ['009900123456'], interval=3600s.
DEBUG [LOGIN] Token obtained successfully (expires_in=3600).
DEBUG Starting E-ON Energy data update (contract=009900123456, collective=True).
DEBUG Collective/DUO contract detected (contract=009900123456). Querying subcontracts via list?collectiveContract.
DEBUG DUO sub_codes (contract=009900123456): 2 codes → ['002100234567', '002200345678'].
DEBUG DUO individual contract_details (contract=009900123456): 2/2 successful. Conventions: 2/2 successful. Meter index: 2/2 successful.
DEBUG E-ON Energy update complete (contract=009900123456, collective=True). Endpoints without data: 1/11.
```

---

## 5. Normal situations (not errors)

### Token renewed automatically

```
[invoice_balance (004412345678)] Error: GET ... → HTTP code=401
[invoice_balance (004412345678)] HTTP 401 → retrying with refresh token.
[REFRESH] Token refreshed successfully (expires_in=3600).
[invoice_balance (004412345678)] Response OK (200). Size: 245 characters.
```

**Cause**: the API token expired. The integration re-authenticates automatically and retries the request. Normal behavior.

### Prosumer endpoints without data

If you are not a prosumer, it's normal for `invoices_prosum` and `invoice_balance_prosum` to return `None` or empty lists. This is not an error — the API simply has no data for that contract.

### Concurrent login

```
[LOGIN] Token already available (obtained by another concurrent call).
```

**Cause**: multiple parallel calls tried to authenticate simultaneously. The internal lock allowed only one — the rest reused the obtained token. Normal behavior.

### DUO — endpoints without data on subcontracts

```
DUO individual contract_details (contract=009900123456): 2/2 successful. Conventions: 1/2 successful. Meter index: 1/2 successful.
```

**Cause**: a subcontract (usually electricity) may not have a consumption convention or available meter data. This depends on the actual contract — not necessarily an error.

---

## 6. Error situations

### Authentication failed

```
[LOGIN] Authentication error. HTTP code=401, Response=...
```

**Cause**: incorrect email or password, or blocked account.

**Resolution**:
1. Verify credentials on the E-ON Myline app
2. If the account is blocked, wait and retry
3. Reconfigure the integration with correct credentials

### Network error / timeout

```
[contract_details (004412345678)] Timeout: GET https://api2.eon.ro/...
```

**Cause**: the E-ON API is not responding or the HA internet connection is interrupted.

**Resolution**:
1. Check the internet connection from HA
2. The integration retries automatically at the next cycle — usually resolves itself
3. If persistent, increase the update interval

### First update failed

```
ERROR First update failed (entry_id=..., contract=004412345678): Could not authenticate with the E-ON API.
```

**Cause**: incorrect credentials or API unavailable at startup.

**Resolution**: check previous logs (`[LOGIN]` messages) for the exact cause. Restart HA after resolving.

### Endpoints without data

```
DEBUG E-ON Energy update complete (contract=004412345678, collective=False). Endpoints without data: 3/11.
```

**Interpretation**:
- **0/11** — everything is working perfectly
- **1-2/11** — normal if you're not a prosumer or don't have rescheduling plans
- **3+/11** — possible issue with the E-ON API or credentials; check preceding errors

### DUO — subcontracts not discovered

```
DUO list (collective) returned None or invalid structure (contract=009900123456): NoneType.
```

**Cause**: the `account-contracts/list?collectiveContract=...` endpoint did not return data.

**Resolution**: check if the collective code is correct, if the account actually has subcontracts, or if the E-ON API is available.

### Error submitting index

```
[submit_meter (004412345678)] Invalid token. Submission cannot be performed.
```

or

```
ERROR Entity input_number.gas_meter_reading does not exist. Cannot submit index (contract=004412345678, type=Submit gas index).
```

or (for electricity):

```
ERROR Entity input_number.energy_meter_reading does not exist. Cannot submit index (contract=002200345678, type=Submit electricity index).
```

**Possible causes**:
1. `input_number.gas_meter_reading` or `input_number.energy_meter_reading` does not exist — must be created manually (see [SETUP.md](SETUP.md))
2. `input_number` has an invalid value
3. You are not in the reading period (meter data is missing)
4. The token is invalid and re-authentication failed

---

## 7. API data logging

At debug level, the integration logs the size of responses (not the full content):

```
[contract_details (004412345678)] Response OK (200). Size: 1523 characters.
```

For login and refresh, the full response is logged (includes the token):

```
[LOGIN] Response: Status=200, Body={"access_token":"...","expires_in":3600,...}
```

**Warning**: these logs contain personal data (tokens, contract codes). **Do not post them publicly without anonymizing.**

---

## 8. How to report a bug

1. Enable debug logging (section 1)
2. Reproduce the problem
3. Open an [issue on GitHub](https://github.com/Alfy1080/eon/issues) with:
   - **Problem description** — what you expected vs. what happened
   - **Relevant logs** — filter by `eonenergy` and include 20–50 relevant lines
   - **HA version** — from **Settings** → **About**
   - **Integration version** — from `manifest.json` or HACS
   - **Contract type** — individual or DUO/collective

### How to post logs on GitHub

Use code blocks delimited by 3 backticks:

````
```
2026-03-04 10:15:12 DEBUG custom_components.eonenergy [contract_details (004412345678)] GET https://api2.eon.ro/...
2026-03-04 10:15:13 DEBUG custom_components.eonenergy [contract_details (004412345678)] Response OK (200). Size: 1523 characters.
2026-03-04 10:15:14 ERROR custom_components.eonenergy [LOGIN] Authentication error. HTTP code=401
```
````

If the log is very long (over 50 lines), use the collapsible section:

````
<details>
<summary>Full log (click to expand)</summary>

```
... log here ...
```

</details>
````

> **Do not post your password, token, or personal data in logs.** The integration logs tokens in login/refresh messages — anonymize them before posting.
