# BTC 5-minute paper platform

This is the implementation plan for a paper-trading platform that follows the live Polymarket BTC 5-minute Up/Down market. It is a plan for a later engineer. It does not place orders, ask for a private key, or claim a profit target. There is no profit target. The read-only recorder and the local dashboard are already in the package. PolyBolt and the paper loop are what is not built. This document does not start a trading process.

The venue is the international Polymarket CLOB documented at `docs.polymarket.com`. Polymarket US is a different venue. The terms forbid wash trading, spoofing, self-matching, layering, and evading the geo block. This plan does none of those. It does not copy a leaderboard wallet, chase a rebate tier, or treat an X profit screenshot or a closed-source bot as the specification.

Paper capital is imaginary. Live capital stays out until the paper record in Phase 2 exists, and Phase 3 is a later goal.

## Where the code already is

The repository package is `fivemin` 0.1.0 (`src/fivemin/`). It requires Python 3.10+ (the suite passes on the VPS's 3.10.12). Two modules open the network, and neither signs an order. `recorder.py` calls `urllib.request.urlopen` for Gamma (`https://gamma-api.polymarket.com/events?slug=...`) and for the CLOB book (`https://clob.polymarket.com/book?token_id=...`). `dashboard.py` listens on `127.0.0.1` (default port 8765) and only reads the log. The package docstring still says live orders are not placed from this package. What is not built is the PolyBolt adapter and the paper loop. `recorder.main` passes `NullFeed`, so spot, the 60-second TWAP, and sigma stay empty and `fair_up` is not called. `paper.py` is the pure fill model. Nothing calls it on a live book yet. Tests live in `tests/` and are the specification of these functions. Later work calls them. It does not reimplement them and does not weaken the tests.

| Module | Role |
| --- | --- |
| `fivemin.market` | `window_start`, `slug_for`, `parse_event`. Window identity, Up and Down token ids, fee schedule, minimum size, tick, lookback, version. |
| `fivemin.fees` | Taker fee curve. `fee_per_share`, `fee_usdc`, `taker_breakeven`, `buy_edge`, `pair_cost`. |
| `fivemin.fair` | `fair_up` and `brier`. Returns `None` when the 60-second formula does not apply. |
| `fivemin.session` | `min_edge(now)`. 3¢, or 5¢ on Saturday, Sunday, and 09:00–12:00 UTC. |
| `fivemin.strategy` | `decide(...)`. Pure. One take candidate, passive prices, `split_ok`, and a log-only `pair_lock`. |
| `fivemin.book` | `parse_book`. Sorts by price; the wire lists the best price last on both sides. |
| `fivemin.paper` | `walk_asks`, `rest`, `RestingOrder.on_trade`, markouts, rebate line, `sleeve`. The fill model below, pure. Not wired to an engine yet. |
| `fivemin.recorder` | Phase 1 recorder: Gamma discovery, CLOB book by REST poll, official outcome. `ReferenceFeed` is the PolyBolt seam; `NullFeed` records no fair value. |
| `fivemin.polybolt` | Chainlink spot and 60 s TWAP from PolyBolt. Rejects non-Chainlink points, keys prints by source second, treats seq gaps as holes. |
| `fivemin.report` | Reduces the log to the Phase 1 gates and the dashboard state. |
| `fivemin.dashboard` | Read-only page over the log on 127.0.0.1. Every number is `report.state(log)`. |

Run the suite from the workspace root with the project virtualenv's pytest (`pyproject.toml` sets the path). The recorder is a one-second poll. The unbuilt PolyBolt adapter and the unbuilt paper loop sit in the same path:

```
window clock
    -> Gamma discovery (slug, tokens, fees, lookback, version)
    -> PolyBolt: Chainlink spot and 60-second TWAP
    -> CLOB market websocket: both books
    -> fair_up, min_edge, decide
    -> risk gate
    -> paper fills (no live execution in the first two phases)
    -> merge leftovers, redeem from the official outcome
    -> append-only log
```

The reference-price client is the official `polymarket-client` SDK, version 0.11 or newer. Do not use the archived `py-clob-client` or the deprecated `py-clob-client-v2`. Phase 1 must authenticate before it can read the settlement feed. Open `wss://ws-live-v2.polymarket.com/ws` and send `op: auth` with the CLOB API `apiKey`, `secret`, and `passphrase`. Wait for `op: authed`. Then subscribe to channel `price.crypto` with filter `{"symbol": "btcusd"}` and to channel `price.crypto.twap` with filter `{"symbol": "btcusd", "window_seconds": 60}`. A subscribe sent before that auth is rejected. Failed credentials close the socket with 4001, and the client does not reconnect until the credentials are fixed. Those three values are CLOB API credentials. They are not a private key, and they are not committed. They are read from a gitignored env file. This plan does not ask for them in chat, and it does not ask for a private key. Gamma discovery stays a public read. Phase 1 and Phase 2 still place no orders.

Config, not code, for: clip size, the daily-loss dollar cap, the open-exposure cap, and the paper/live flag. The paper/live flag defaults to paper. The 3¢ and 5¢ residuals, the 95% cap, and the 120-second switch stay in `session.py` and `fair.py` so the tests keep owning them.

## What is copied from the live venue

The paper platform follows the live Polymarket environment. Every number below that is a market field is read from that market on that window. The 9 Nov 2026 observation is a check that the parser and the fee math still match a market we have already seen. It is not a default to write into the engine.

### Discovery

A new window opens every 300 seconds. Window identity is the slug `btc-updown-5m-{unix_open}`. `unix_open` is the current Unix time floored to a multiple of 300, which is `window_start` in `fivemin.market`. `slug_for("btc", now)` builds the slug. The check locked by `tests/test_strategy.py` is the window that contains 2026-11-09 07:37:00 UTC: open `1794209700`, slug `btc-updown-5m-1794209700`, the market titled for 2:35–2:40 AM ET.

Discover through Gamma: `https://gamma-api.polymarket.com/events?slug={slug}`. Refresh the current slug and the next slug about 30 seconds before each open. `parse_event` reads the first market on the event.

Up and Down token ids come from `outcomes` and `clobTokenIds`. Those fields may be JSON strings. Match the token by outcome name, not by array index. A payload that lists Down first still binds the Up token to the Up outcome. The same parser reads `conditionId`, `negRisk`, and `version`.

Refuse any slug that is not `btc-updown-5m-*`. ETH, SOL, XRP, and 15-minute markets are out of this plan. If `cryptoMarketConfig.asset` or `duration` is present and is not BTC and the 5-minute duration, skip the window and record the raw values.

### Books

Subscribe to the CLOB market websocket documented for the international venue, one stream for the Up token and one for the Down token. Keep the full displayed depth, not the mid. Each side of the book carries a receipt timestamp. `decide` refuses a take when that age is greater than `FRESH_BOOK_SECONDS` (1.0 second).

The paper book is that displayed depth. That limit applies to fills: there is no fill at the mid, and there is no fill beyond displayed size. Posting a new level is allowed. A resting bid or ask from `decide` may sit at a price where displayed size is zero, and then `size_ahead` is 0. The mid is recorded for the Brier comparison and for markout. It is never a fill price.

Maker orders, when a later phase sends them, are post-only GTD. A paper bid or ask that would cross the opposite touch is not rested. Crossing is a take, and a take has to pass the take rule below. Cancel a resting quote when it is no longer the required residual through the model. The venue has a taker delay during which a live maker can cancel. Do not hardcode 150 ms or 250 ms, or any other delay constant. If the venue publishes the delay, log the published value. Do not branch on it.

### Reference price

The reference is the Chainlink BTC/USD stream on PolyBolt. Phase 1 authenticates with `op: auth`, then subscribes to `price.crypto` and `price.crypto.twap` for symbol `btcusd`, with `window_seconds` 60 on the TWAP filter, as specified above. Use `full_accuracy_value`. PolyBolt has no 30-second TWAP.

RTDS price topics shut down at 30 Nov 2026 00:00 UTC. Do not build on RTDS.

The price to beat, `K`, is the Chainlink 60-second TWAP at the open of the window (the print on the bell). Spot from Binance, Coinbase, or any other exchange is not `K` and is not the settlement input. If the open TWAP print is missing, skip the window. Do not substitute a spot print.

`S` in the fair-value call is the Chainlink spot. `sigma_1m` is the standard deviation of one-minute log returns of that same Chainlink spot over the trailing hour (60 completed returns). Fewer than 60 returns, a non-positive standard deviation, or a non-positive spot means there is no fair value.

### Settlement

Settlement is the Chainlink 60-second TWAP versus the price to beat. The close value is the 60-second TWAP at the end of the window. The local Up/Down call is Up when the close TWAP is greater than or equal to the price to beat, and Down otherwise. A tie resolves Up. That matches the resolution text on the checked market: Up if the Chainlink TWAP of the window is at or above the price at the beginning of the range. The source named on that market is `https://data.chain.link/streams/btc-usd-twap-60s-streams`.

One Up share plus one Down share pays $1 at settlement. Redeem from the official Polymarket outcome. The local call is a check against that outcome. It does not override it.

`twapLookbackSeconds` is read from the market (`cryptoMarketConfig.twapLookbackSeconds`). `parse_event` leaves a missing lookback as `None`. Pass that value into `fair_up` as `lookback_seconds`. Do not omit the argument: the function's default is 60, and a missing field must not become a silent 60. A lookback other than 60 seconds is skipped, not priced with the 60-second formula. `fair_up` returns `None` for any other lookback, and `decide` then returns no quotes and no take (`reason` is `no fair value`). Do not derive a 30-second variance in v1. Record the foreign lookback and move on.

Inside the formula the lookback constant `LOOKBACK_SECONDS` is 60 and the variance offset is 40 seconds (`2 * 60 / 3`). With more than 60 seconds left:

```
horizon = max(seconds_left - 40, 1)
z = ln(S / K) / (sigma_1m * sqrt(horizon / 60))
p_up = Φ(z)
```

`Φ` is `normal_cdf` in `fair.py` (the error function, not a fitted approximation). With 60 seconds or fewer left, do not compare spot with `K`. Pass `final_minute_prices`: one Chainlink price per completed second of the averaging window, oldest first, a prefix with no holes and no forward-filled gaps. A hole means pass `None` and skip. The function then locks the printed sum. The future average of the remaining `r = 60 - len(printed)` seconds must be at least `(K * 60 - printed_sum) / r` for Up to win. The unfinished average has expectation `S` and standard deviation `(S * sigma_1m / sqrt(60)) * sqrt(r / 3)`. When `r` is 0, the probability is 1 if the average is at or above `K` and 0 otherwise, so a tie on the finished average is Up.

Probability is capped at 95% when more than 120 seconds remain. The same clamp floors the other tail at 5%. Inside 120 seconds the cap is off and the probability may sit anywhere in `[0, 1]`. The recorder stores the capped value. A stored probability above 95% with more than 120 seconds left is a bug.

### Fee fields

Read `feeSchedule` on every window. The fields used are `rate`, `exponent`, `takerOnly`, and `rebateRate`. Also read `orderMinSize`, `orderPriceMinTickSize`, `version`, and `negRisk`.

`CryptoWindow` stores rate, rebate rate, taker-only, minimum size, tick, lookback, version, and `fee_exponent`. `parse_event` sets `fee_exponent` from `feeSchedule.exponent`. A missing exponent stays `None`. `skip_reasons` refuses any value other than 1, so a missing exponent is a skip, the same as an exponent of 2. The window record logs the raw schedule as `raw_fee_schedule`. That copy is only a log. The skip uses the stored field. The coded curve in `fees.py` has no exponent argument. It is the exponent-1 polynomial. `skip_reasons` also skips when `takerOnly` is not true, because the maker rule below assumes makers pay zero, when the tick is not `0.01`, because `strategy.TICK` is `0.01` and `decide` does not take a tick argument, and when the configured clip is smaller than `orderMinSize`.

The 9 Nov 2026 check, from the open market `btc-updown-5m-1794209700`, is a fixture to re-read, not a constant to substitute when a field is missing:

| Field | Observed value |
| --- | --- |
| `feeSchedule.rate` | 0.07 |
| `feeSchedule.exponent` | 1 |
| `feeSchedule.takerOnly` | true |
| `feeSchedule.rebateRate` | 0.2 |
| `orderMinSize` | 5 |
| `orderPriceMinTickSize` | 0.01 |
| `cryptoMarketConfig` id | `btc-5m-twap-60` |
| `twapLookbackSeconds` | 60 |
| `version` | `v1` |
| `negRisk` | false |

`version` chooses the later inventory path. The observed market was `v1`, so a split, merge, or redeem goes through the CTF adapter, not the V2 router. A market with `version == "v2"` would use the router. Read `version` per market. Do not assume `v1` forever, and do not send a split until Phase 3 has done the $1 check on the version that market publishes.

At the checked rate of 0.07, `fee_per_share(0.50, 0.07)` is 0.0175 USDC and `taker_breakeven(0.50, 0.07)` is 0.5175. A 62.1% model at a 55¢ ask clears a 3¢ residual. The same model at a 60¢ ask does not. Those three facts are tests. A different published rate uses the same functions with the rate that was read.

## Fill and queue model

Paper fills replicate the book, not a mid-price simulator. There is no fill at the mid, no fill through size that is not displayed, and no fill when the book snapshot is older than one second.

Taker cost is `shares × feeRate × price × (1 − price)`. That is `fee_usdc` in `fivemin.fees`. Charge it on each lot at that lot's price. Do not charge the fee of the average price. Makers pay zero.

A take walks displayed size. Sort asks from best to worst. Consume size at prices at or better than the decision's limit until the clip is filled. If displayed size through that limit is smaller than the clip, the take does not happen. There is no partial clip and no invented remainder. The fill price used in the log is the size-weighted price of the lots walked. The fee is the sum of the per-lot fees. The decision layer already requires the best ask's displayed size to be at least the clip and the book age to be at most 1.0 second. The walker still refuses to go past displayed size if the book moves between the decision and the fill.

A resting order waits for trades to clear size ahead of it.

- On rest, `size_ahead` is the size already displayed at our price on our side. Posting a new level is allowed. If that level was empty, `size_ahead` is 0. Otherwise we join behind the size that was already there. Our own size is not part of `size_ahead`.
- A later trade print at our price reduces `size_ahead` first. Only the part of the print that remains after `size_ahead` reaches zero fills us, at our price, up to our remaining size.
- A trade that prints strictly through our price (below a bid, above an ask) is treated the same way: clear `size_ahead`, then fill us. The fill price is still our limit.
- A cancel of displayed size does not reduce `size_ahead`. The public book does not say whether the cancelled order was ahead of us or behind us. Only trades clear the queue.
- We never fill more than the trade size, and we never fill a resting order from a quote update that has no trade.

One-second markout and settlement markout are logged on every paper fill.

- One-second markout. One second after the fill, if both best bid and best ask exist and that book is at most one second old, the mid is their average. For a buy, markout is `mid − fill price`. For a maker sale, markout is `fill price − mid`. A missing mid is stored as null, counted in a missing column, and left out of the average.
- Settlement markout. The official payout is 1 when our outcome won and 0 otherwise. A tie resolves Up, so an Up share pays 1 on a tie. Settlement markout for a buy is `payout − fill price − taker fee per share`. The maker fee per share is zero, so a maker buy is `payout − fill price`.

The judged result is after fees and before rebates. Sum the settlement results. That sum is the only P&L the gates look at. Rebates are a separate line: for each maker fill, log `rebateRate × fee_per_share(fill price, feeRate) × shares` as an estimate of the maker rebate, using the `rebateRate` read from that market (0.2 on the 9 Nov check, which is 20% of the crypto taker fee). Do not add that line to the judged result. Do not add a leaderboard taker-rebate tier. A new account does not inherit one, and this plan does not raise size to chase one.

A paper book cannot fully reproduce queue position or the taker-delay race against other live orders. The queue model is an approximation. Someone can join ahead of a paper order, cancel ahead of it, or win the cancel race during the taker delay, and the public tape will not show that in a way this model can replay. Paper P&L is not a live fill. A cancel that would have lost the race on the live book is an adverse fill we will not see until Phase 3, which is why Phase 3 compares live markouts with paper markouts on the same windows and why takes stay off until a later paper stretch.

Report, from the log: skip rate, takes, makes, mean one-second markout, mean settlement markout, fee drag, judged P&L, and the rebate line. A day with zero trades is a working record.

## Decision rules

On every book update that passes the risk gate, call `min_edge(now)` and `decide`. Most windows are skips.

Trade only when model probability clears the ask plus the taker fee by at least 3¢ (5¢ on weekends and 09:00–12:00 UTC). For Up that is `buy_edge(p_up, ask_up, fee_rate) >= min_edge`. For Down it is the same with `1 − p_up` and the Down ask. `min_edge` returns `BASE_EDGE` (0.03) except on Saturday, Sunday (`weekday >= 5`), and hours 9, 10, and 11 UTC, where it returns `QUIET_EDGE` (0.05). The take also requires displayed size at the ask of at least the clip, and a book age of at most 1.0 second. The order is a buy that walks that ask, then holds to redeem. One take, the larger of the two edges. `decide` implements the choice. It does not remember the window. The engine enforces one taker clip per window.

Passive bids and asks sit at least that residual through the model. `decide` sets the bid to the tick at or below `p − edge` and the ask to the tick at or above `p + edge`, and drops either price outside `[0.01, 0.99]`. At `p = 0.50` and a 3¢ edge the quotes are 47¢ and 53¢ on both outcomes, which the tests lock. Rest a quote only while it is still that far through the model. Recompute on every Chainlink tick. If the quote has fallen inside the residual, cancel it. A bid that would cross the ask is not a passive bid.

A complete-set split is allowed only when the two passive bids sum to 99¢ or less. That is `decision.split_ok` (`SPLIT_MAX_SUM` is 0.99). Both bids have to exist. A double fill then pays at most 99¢ for one Up and one Down, which redeem for $1, so the locked cent is there before any rebate and without a taker fee. A single fill is a share the model was willing to own at that bid. If the two bids sum to more than 99¢, do not split. `pair_lock` is not a split and not a trade. It is true when the all-in taker cost of both asks is under $1. The engine logs it (`reason` can be `pair under a dollar, log only`) and places nothing because of it.

Inventory is merged or redeemed, not sold. A take is a buy and is held to the official outcome. At 20 seconds before the window ends, cancel every resting quote. Merge any complete-set pair still held. Leave unpaired shares for redeem. Do not send a sell to flatten, and do not sell a winner early as a taker. Cancel earlier than 20 seconds when the last-minute formula is in use and the resting quote is no longer a full residual through the updated model. The passive ask prices that `decide` returns are the residual offer. They may rest as post-only quotes. They are not a liquidation. If one is hit, that fill is a maker sale at a price the model already required, and it is marked out on its own. There is no second action that dumps inventory into the bid.

A post-only bid under 10¢ is paper-only. In the public fill census, maker buys under 10¢ settled about 4.1% above the price paid, and taker buys in that band settled about 17.6% below it. The sleeve is the passive bid `decide` already computed, in the case where that bid is strictly under 10¢. It is already at least the residual through the model. Tag its fills `sleeve=under_10c`, keep them post-only, and keep them in their own markout row. `decide` has no branch for this sleeve. Do not add a bid under 10¢ that `decide` did not emit, and do not buy the 20–40¢ side because it looks cheap. The engine drops a take whose ask is strictly under 10¢ even when `decide` returns that take. Taker buys under 10¢ were the worst band in the census. The live flag, when it exists in a later goal, does not send this sleeve.

The pure function also does not know about feed silence, the daily-loss halt, or the one-clip limit. Those sit in the risk gate in front of `decide`.

## Risk limits

The risk gate runs before any paper fill and, later, before any live order. Hitting a halt cancels every resting quote.

- Feed silence. If the PolyBolt socket or either book has no message for 1.0 second, cancel all quotes and trade nothing. The same 1.0 second is `FRESH_BOOK_SECONDS`. A stale Chainlink spot or a stale open TWAP means `fair_up` is not called. There is no last-known-price trade.
- Daily loss. The operator sets a dollar cap in config before Phase 2. The process refuses to start the paper engine without that cap. This plan does not choose the number and does not treat it as a profit target. When the UTC day's judged P&L, after fees and before rebates, reaches minus that cap, cancel every order and refuse new decisions until a person restarts the process.
- One clip per window. At most one taker clip, and resting size of at most one clip per side. The clip is config and must be at least the market's `orderMinSize` (5 on the 9 Nov check). No add, no martingale, no second clip after a fill or a miss.
- Open exposure. The operator sets a cap on the open paper cost (price times shares, plus taker fees already charged). An order that would exceed it is skipped.
- Pre-rebate P&L. The figure that trips the daily loss, and the figure Phase 2 has to show, is the judged result: after fees and before rebates.

A dedicated trading wallet appears only in Phase 3. This plan does not ask for a private key. Phase 1 does require the three CLOB API credentials named above (`apiKey`, `secret`, `passphrase`). Those credentials are not a private key, and they are not committed. When Phase 3 exists, the trading key stays in a separate gitignored env file, on a wallet funded only with the test budget.

## Build phases

The phased build starts from the existing pure fee, fair-value, and decision code. Record live windows until the local Up/Down call matches Polymarket. Then run the paper engine. Live capital stays out until that paper record exists.

### Phase 1 — read-only recorder

`fivemin.recorder` already resolves the current and next `btc-updown-5m-{unix_open}` slug from Gamma, polls both CLOB books, and appends one JSON object per second plus a `window` record and a `resolution` record. Each `window` record includes the slug, both token ids, `raw_fee_schedule`, minimum size, tick, lookback, version, `negRisk`, and `skip_reasons`. Each tick includes `S`, `K`, `sigma_1m`, seconds left, `p_up`, both best bids and asks with sizes, book age, and the session edge. At resolution it records the close TWAP, the local Up/Down call, and the official outcome. With `NullFeed` those reference fields are null. Phase 1 still has to authenticate to PolyBolt with `op: auth`, then subscribe to `price.crypto` and `price.crypto.twap` for symbol `btcusd` with `window_seconds` 60, and stamp `K` from the TWAP tick on the bell. That adapter is not built. The paper loop is not built either.

Run at least 50 resolved windows. Two gates, both required:

1. The local Up/Down call from `K` versus the close TWAP matches Polymarket on every window. One mismatch stops the build. Fix the settlement read before any paper fill. A TWAP rebuilt from stored spot ticks may be logged beside the stream. It does not replace the stream in this gate.
2. Score `p_up` with `brier` against the 0/1 outcome, next to the book mid at the same predeclared second (60 seconds after the open, once `K` exists and before the last-minute formula). Matching the mid is success. Beating the mid on 50 windows is not required and will not be claimed. Stop if `Brier(p_up) − Brier(mid)` is greater than 0.02. Also stop if any stored `p_up` with more than 120 seconds left is outside `[0.05, 0.95]`. If we lose to the mid by that margin, the formula or the vol estimate is wrong and no fill logic is written yet.

Phase 1 places nothing and simulates nothing.

### Phase 2 — paper engine

Wire `decide` to the fill model above. Charge the fee curve. Walk depth. Wait out the queue. Log the one-second markout and the settlement markout. No orders leave the machine. The paper/live flag stays on paper.

Run several days. The log has to include a weekend (Saturday or Sunday, so the 5¢ residual is actually in force) and a New York morning. In November, New York is on Eastern Standard Time, so that morning is 14:00–17:00 UTC. It also has to include the quiet UTC band on a weekday, 09:00–12:00 UTC, which is a different session from the New York morning.

Then read the log:

- A negative one-second maker markout larger than the 3¢ cushion stops the maker path. The comparison uses the base 3¢ (`average < −0.03`), including on weekend fills, so the wider 5¢ hurdle cannot hide it. Stop means cancel maker quotes and do not resume them on that same sample.
- A take path that only wins because it filled at the mid is thrown out. Recompute every take at the walked price and the per-lot fee. If the walked result is not positive, the take rule does not graduate. A take that is positive only against the mid is not a pass.
- The under-10¢ sleeve is judged on its own row. It does not get to pull the maker average up, and it is not sent anywhere but the paper log.
- Judged P&L is reproducible from the log alone, after fees and before rebates.

### Phase 3 — tiny live, only after that paper record

This phase is specified so the later goal has a gate. It is not part of the work that writes this document. Live capital stays out until Phase 2's record exists and the maker markout has not tripped the stop.

When it is started, from the same machine that will send orders:

- Check `https://polymarket.com/api/geoblock`. If the machine is blocked, stop. Do not route around the block.
- Use a dedicated wallet. Confirm one $1 split, one merge, and one redeem on the path that the market's `version` and `negRisk` select. The 9 Nov market was `v1`, which is the CTF adapter. Do not depend on inventory logic before that $1 round trip succeeds.
- Then post-only quotes at the market minimum size (5 shares on the check), and run a tested daily-loss halt that cancels resting orders. Compare live one-second markouts with the paper markouts on the same windows.
- Takes stay off until paper takes, after fees and after failed cancels, are positive on a later stretch of days than the one used to inspect the rule.
- The under-10¢ bid stays paper-only.

### Phase 4 — other books stay closed

No ETH, SOL, XRP, or 15-minute market. They share the BTC move, so they are one bet, and the 5-minute table does not transfer. Add nothing until a later decision, after BTC 5-minute live matches paper. That decision is outside this plan.

## What the leaderboard does not specify

The 9 Nov 2026 census is a constraint on the gates, not a strategy to copy. Always-on bots are the book: a few percent of wallets post most of the maker size. As a group their trading P&L was a loss, and rebates made the group slightly positive. The wallets that finished up were mostly makers and mostly active on both outcomes. Both-sides is allowed here only as the passive complete set whose bids sum to 99¢ or less. A paired cost above $1 is how a two-sided quote bleeds. Copying a leaderboard wallet one or two minutes later makes you the taker who pays the fee. The late directional load on the SLIP-ME tape is one wallet, not a rule, and the pre-registered test of that idea lost money. Hold to redeem. Do not chase a rebate tier. The largest losses in the census were large.

## Limits of this plan

The queue model is an approximation. A paper book cannot fully reproduce queue position or the taker-delay race against other live orders. Paper P&L is not a live fill.

`decide` does not enforce one clip per window, does not drop a take under 10¢, and does not halt on a silent feed. The engine does. `fair_up` prices only a 60-second lookback. Anything else is a skip. Fee rate, lookback, tick, minimum size, exponent, and version are read per market. The 9 Nov 2026 values are the check.

Success is the Phase 1 match to Polymarket, a `p_up` that is not a wide miss of the book mid, and a Phase 2 log whose judged result is after fees and before rebates and whose maker markout is inside the 3¢ cushion. There is no profit target. This document is the plan only.

## Findings from the raw payloads (9 Nov 2026)

Checked against live Gamma and CLOB responses, saved under `tests/fixtures/` and locked by `tests/test_live_payloads.py`:

- The `/book` payload lists bids ascending and asks descending. The best price is the last element on both sides. `parse_book` sorts; nothing indexes the raw lists.
- Gamma carries `eventStartTime`, which must equal the slug's open. `skip_reasons` checks it.
- A resolved window carries `closed: true`, `umaResolutionStatus: "resolved"` and `outcomePrices` of `["0","1"]` or `["1","0"]`, matched by outcome name. That is `official_outcome`. A closed window is only returned by Gamma with `&closed=true`.
- The Up and Down books mirror (Up ask x is Down bid 1 − x). Two REST snapshots taken about 0.3 s apart summed to 0.99–1.02 in about one tick in five, so a REST pair is not a consistent pair. `pair_lock` and any two-book logic need the websocket, not the poll.
- `decide()` can emit a passive bid at or above the best ask. The engine must pass every passive price through `paper.rest`, which refuses a crossing quote.
- `parse_event` previously defaulted a missing `takerOnly` to true, a missing tick to 0.01 and a missing `orderMinSize` to 0. Those are now `None` and skip the window.

- PolyBolt's reference-price channels require CLOB API credentials (`{"op":"auth"}` with apiKey, secret, passphrase). An unauthenticated subscribe to `price.crypto` and `price.crypto.twap` answered `auth_required` on 9 Nov 2026. "Phase 1 and Phase 2 can read the public market and book feeds without a wallet" holds for Gamma and the CLOB book, not for `K` and `S`. Deriving CLOB API credentials needs a wallet signature, so the operator does that once, outside this code, and sets `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`. Read-only credentials on an unfunded wallet are enough; nothing here places an order.
- Every PolyBolt point carries `source` (`chainlink`, `pyth`, `binance`). The `provider` pin may be ignored by the server, and the docs' own example snapshot is `pyth`. `fivemin.polybolt` rejects any point that is not `chainlink` and counts it.

Without those credentials the recorder runs `NullFeed`. Then every tick records `no fair value`, `local_call` is null, and gate 1 counts the window as resolved without a local call, never as a match.
