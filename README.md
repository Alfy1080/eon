# E-ON Energy — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom integration for Home Assistant that monitors contract data, consumption, and bills through the E-ON Myline API.

## Features

* **Multi-contract**: A single E-ON account can monitor multiple billing codes simultaneously.
* **Full DUO support**: Collective contracts (gas + electricity under a single code) are automatically detected, with separate sensors per subcontract.
* **MFA Support**: Fully supports Two-Factor Authentication (Email/SMS) during the initial login.
* **Prosumer Support**: Tracks prosumer invoices and balances (debts and credits).
* **Dedicated sensors**: Contract data, Invoice balance, Current index, Reading allowed, Overdue invoices, Consumption agreement, and historical archives.
* **Actionable buttons**: Submit your meter readings directly from Home Assistant using `input_number` entities.
* **Free and open source**: No license or subscription required.

## Installation & Setup

Please refer to the [Installation and Configuration Guide](SETUP.md) for step-by-step instructions on how to install via HACS and configure the integration.

## Prerequisites

* Home Assistant 2024.x or newer.
* Active **E-ON Myline** account (email and password).

## Documentation

Detailed information is available in the following guides:

* **[Setup Guide](SETUP.md)**: Installation, configuration, and Lovelace dashboard examples.
* **[FAQ](FAQ.md)**: Frequently asked questions about sensors, missing data, and automation examples.
* **[Debugging Guide](DEBUG.md)**: How to enable detailed logging and troubleshoot API or configuration issues.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

---