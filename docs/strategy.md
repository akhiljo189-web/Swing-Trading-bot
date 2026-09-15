# Strategy and simulation assumptions

## Hypothesis

The strategy tests whether breakouts within established uptrends can produce useful results after costs. It has no established edge. A sideways market can cause repeated small losses, and overnight gaps can exceed a planned stop loss.

## Entry and exit

Use completed daily bars. A close must exceed the highest high of the preceding 20 sessions, exceed its 200-session simple moving average, and have the 50-session average above the 200-session average. Rank simultaneous signals by breakout percentage, with symbol as the tie-breaker.

The following session's entry cap is the signal close plus one quarter of the mean true range over 14 sessions. The simulator assumes a fill at the limit if the daily low touches it; this is a simplified daily-bar model. It does not model a queue, partial execution, or available volume. The protective stop begins two such ranges below the fill. When both entry and stop are touched in one bar, the simulator uses the adverse ordering.

Close below the preceding 10-session low or reaching 20 held sessions schedules an exit for the following session. Stop exits can happen sooner. Market/stop exits include an illustrative five-basis-point adverse slippage; stop gaps use the worse opening price.

## Risk and costs

Example defaults use 0.5% of equity as the planned risk budget including round-trip fees, 25% per-position notional, three positions maximum and 1.5% aggregate planned risk. Fees must not exceed 20% of the per-trade risk budget. These defaults may reject many trades in a small portfolio; rejection is part of the test.

The default per-side fee is an **illustrative GBP 0.35**, not a verified IBKR tariff. Actual fees vary by exchange, pricing plan and instrument. Taxes, FX conversion, market-data subscriptions and currency exposure are not implemented. GBP-normalized data is required. Adjusting the test assumptions to actual instruments is necessary before assessing economic viability.

Sales proceeds become available on the next supplied session, a simplified settlement model that does not resolve exchange holidays or different settlement conventions. No leverage or shorting is allowed.

New entries pause at a 5% cash-flow-adjusted drawdown. Existing exposure continues through exit rules. Contributions are accounted for separately from profit. A pause is not a guarantee against further loss.

## Data and validation boundaries

Synthetic demo data checks that the software operates. It is not a historical backtest. Imported data is supplied by the operator; the application validates row consistency but cannot establish licensing, point-in-time membership, genuine earnings safety, adjusted prices or completeness of exchange calendars.

Before connected paper order execution, test contract eligibility, tick-size rounding, bracket behavior on partial fills, rejected protective orders, stale quotes, disconnects, process restarts and reconciliation against IBKR. Historical strategy evaluation also needs an untouched evaluation period, cash/buy-and-hold benchmarks and sensitivity tests for costs.

## References

- [IBKR paper trading](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading)
- [IBKR commission schedules](https://www.interactivebrokers.co.uk/en/index.php?f=49808)
- [IB Gateway](https://www.interactivebrokers.com/docs/tws-api/doc/architecture/the-trader-workstation/the-ib-gateway)
- [Official TWS API installation](https://www.interactivebrokers.com/campus/trading-lessons/accessing-the-tws-python-api-source-code/)

Documentation checked 15 September 2026. Broker requirements can change.
