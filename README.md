# Project Trading

A stocks-and-ETFs swing-trading project, starting with simulated trading through Interactive Brokers.

## Current status

Design stage. No trading bot, broker connection, historical test results, or automated data uploads have been implemented. Live trading is not enabled.

## Proposed approach

Evaluate an explicit daily trend-breakout strategy using completed trading-session data, next-session entries, protective exits, position limits, and realistic transaction costs. This is a research hypothesis, not a demonstrated profitable strategy.

## Online storage

This repository is the online home for project code and documentation. Small, sanitized paper-trading summaries and test reports can be added as the system is built. GitHub stores versioned snapshots; the operating bot will use a local database for live state and order reconciliation.

This repository is currently public. Personal trading records, account identifiers, balances, and detailed account exports must remain local until a private storage destination is established. Credentials, API keys, authentication tokens, and environment files must never be committed, even to a private repository. Raw broker market data must only be uploaded where the applicable data licence permits it.

Automated snapshot uploads are planned, not configured. No trading data has been uploaded.

## Skills source

Consult [Skills-Plugins](https://github.com/akhiljo189-web/Skills-Plugins) for relevant compatible development skills, reviewing their instructions and dependencies before use.
