# IBKR paper readiness

This workbench runs its own local simulation. The optional IBKR check verifies an authenticated paper-session handshake and disconnects. **Connected order execution is not implemented.** The checker sends no order, position, account-summary or market-data requests.

## Prepare the account locally

1. Create or enable your IBKR paper account in the broker portal. Account approval and funding may be prerequisites for a full paper account; a trial login has additional limitations. See [IBKR paper-trading requirements](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading).
2. Install [Trader Workstation or IB Gateway](https://www.interactivebrokers.com/docs/tws-api/doc/download-tws-or-ib-gateway/download-tws-or-ib-gateway). Sign in yourself and explicitly choose **Paper Trading**. Complete authentication in the broker application; this workbench never handles login credentials. Broker resets or session expiry can require another login.
3. In TWS API settings, enable **ActiveX and Socket Clients**, keep **Read-Only API** enabled, and allow local connections only. Use port **7497** for TWS paper or **4002** for Gateway paper. The checker rejects the default live ports and remote hosts. See [IBKR configuration instructions](https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/).
4. Identify your exact paper account locally. It must begin `DU` followed by digits. The checker requires this explicit allowlist and rejects a session exposing any other account, including multiple paper accounts. A port number alone never proves the account is paper.

## Install the optional official Python SDK

Download the TWS API installer/archive from the [official IBKR download instructions](https://www.interactivebrokers.com/docs/tws-api/doc/download-the-tws-api/introduction). Use the SDK supplied there, and follow its Python installation instructions; package-index copies are not the official distribution channel.

From the downloaded SDK's `source/pythonclient` directory, install into the **same Python environment** used for this workbench:

```powershell
python -m pip install .
python -c "import ibapi; print('IBKR SDK import successful')"
```

The SDK can have its own dependencies (such as protobuf); use the versions supplied or required by that SDK release. Offline simulation and its dashboard do not require the SDK.

## Run the check

From the project directory, this command prompts privately for the account and prints only the redacted result:

```powershell
python -c "import getpass,json; from trading.ibkr import check_connection; print(json.dumps(check_connection(account=getpass.getpass('Paper DU account: ')), indent=2))"
```

For Gateway, add `port=4002` to the `check_connection(...)` call. Do not put credentials or account identifiers in repository files, shared terminal transcripts, or chat. No account identifier is returned or persisted by this checker.

`connected` means both `managedAccounts` and `nextValidId` arrived and the account list exactly matched the allowed paper account. This is a one-shot check, not a persistent connection. `read_only: true` describes the checker's behavior; `broker_read_only_setting_verified: false` explicitly records that the broker's GUI setting cannot be established by this handshake. The user must check that setting in TWS/Gateway.

Other statuses: `invalid_configuration`, `sdk_unavailable`, `account_rejected`, `timeout`, `connection_lost`, `connection_failed`, or `broker_error`. Raw broker messages are intentionally excluded from the result. Inspect the broker application locally for error details. Informational market-data-farm notices are allowed; other errors fail closed. The default wait is 10 seconds, configurable up to 60 seconds, plus up to 0.2 seconds of cleanup. If the SDK blocks inside connection setup, the daemon worker is cancelled and disconnected best-effort, and disconnects again when setup returns; Python cannot forcibly stop a blocked SDK call.

## What still needs validation

An actual authenticated check must be performed on the user's machine after installation and account preparation. Passing this check does not validate market-data entitlements, price quality, contracts, order behavior, broker reconciliation, or broker-side risk controls.

Real-time and historical API data may require an approved, funded account and paid exchange subscriptions; availability depends on the instrument and account. Review [IBKR API market-data requirements](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/introduction) and [paper delayed-data information](https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php). The simulation does not fetch IBKR market data.
