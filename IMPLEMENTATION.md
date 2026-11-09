# BTC 5-minute paper platform — Limitless

This is the implementation plan for a paper-trading platform that follows the live Limitless BTC 5-minute Up/Down market. It is a plan for a later engineer. It does not place orders, ask for a private key, or claim a profit target. There is no profit target. The read-only recorder and the dashboard are in the package and run on the VPS. The paper loop is what is not built. This document does not start a trading process.

The venue is Limitless Exchange on Base (`api.limitless.exchange`, documented at `docs.limitless.exchange`). The plan moved here from Polymarket on 9 Nov 2026, because Polymarket's domains are under an Indian governmental blocking order (see the last section) and every machine this project runs on is in India. Nothing in this package contacts Polymarket. The Limitless terms make the user responsible for whether their use is lawful where they are; that question is open for any real-money phase and is not answered by the fact that the domain resolves. This plan does no wash trading, spoofing, self-matching or layering, does not route around any block, and does not treat a leaderboard wallet, an X screenshot or a closed-source bot as the specification.

Paper capital is imaginary. Live capital stays out until the paper record in Phase 2 exists, and Phase 3 is a later goal.

## Where the code is

The package is `fivemin` (`src/fivemin/`). It requires Python 3.10+ (the suite passes on the VPS's 3.10.12). Nothing in it signs an order. Tests in `tests/` are the specification of these functions; later work calls them, does not reimplement them, and does not weaken the tests. Raw venue payloads captured on 9 Nov 2026 are in `tests/fixtures/` and locked by `tests/test_live_payloads.py`.

| Module | Role |
| --- | --- |
| `fivemin.market` | `slug_for`, `parse_market`, `skip_reasons`, `official_outcome`, `local_call`. Limitless market JSON to a `CryptoWindow`. |
| `fivemin.book` | `parse_book` (6-decimal sizes, sorted by price), `mirror` (the Down book). |
| `fivemin.fees` | Taker curve `fee_rate * p * (1 − p)` and the exact per-match `limitless_match_fee`. |
| `fivemin.reference` | Chainlink 1-minute candles (σ and an independent TWAP estimate) and the Coinbase/Kraken spot proxy. |
| `fivemin.fair` | `fair_up` and `brier`. `None` when the 60-second formula does not apply. |
| `fivemin.session` | `min_edge(now)`. 3¢, or 5¢ on Saturday, Sunday and 09:00–12:00 UTC. |
| `fivemin.strategy` | `decide(...)`. Pure. One take candidate, passive prices, `split_ok`, log-only `pair_lock`. |
| `fivemin.paper` | Depth walk, queue model, markouts, rebate line. Not wired to an engine yet. |
| `fivemin.recorder` | Phase 1 recorder. Read-only. |
| `fivemin.report`, `fivemin.dashboard` | Gates and the read-only page, reduced from the append-only log. |

Run the suite with the project virtualenv's pytest from the workspace root. Deploy with `deploy/run.sh` (user-level, pid files, refuses a busy port, binds 127.0.0.1).

```
window clock
    -> GET /markets/btc-up-or-down-5-min-{open}        (market, K, fee venue, lookback)
    -> GET /markets/{slug}/orderbook                    (Up book; Down = mirror)
    -> Chainlink candles (σ) + spot proxy (S)
    -> fair_up, min_edge, decide
    -> risk gate
    -> paper fills (no live execution in the first two phases)
    -> redeem from the official outcome
    -> append-only log
```

Config, not code, for: clip size, the daily-loss dollar cap, the open-exposure cap, and the paper/live flag (default paper). The 3¢ and 5¢ residuals, the 95% cap and the 120-second switch stay in `session.py` and `fair.py` so the tests keep owning them.

## What is copied from the live venue

Every market number is read from that market on that window. The 9 Nov 2026 values are a check that the parser still matches the venue, not defaults.

### Discovery

A window opens every 300 seconds. The slug is `btc-up-or-down-5-min-{unix_open}` (`slug_for`). `GET /markets/{slug}` returns it; `startAt` is the open and `expirationTimestamp` (ms) the close. `skip_reasons` refuses a slug that is not this series, a slug whose number is not `startAt`, a window that is not 300 seconds, an oracle stream that is not `twap`, a TWAP window that is not 60 seconds, and a venue whose fee schedule is unknown. The market's metadata names Polymarket as an external parity source (`externalSlug`, `isPolyArbitrage`); this plan reads none of it.

Outcomes are YES and NO token ids, not names. YES is Up: the resolved window of 08:50 UTC carried `chainlinkTwapResolution.outcome: "UP"` with `winningOutcomeIndex: 0` and `payoutNumerators [1, 0]`.

### Book

`GET /markets/{slug}/orderbook` returns one book in the YES token. Sizes are raw 6-decimal units (1 share = 1,000,000); prices are decimals in thousandths. Down has no separate book: buying Down at q is selling Up at 1 − q, so `mirror` turns Up asks into Down bids and Up bids into Down asks. One request gives both sides at the same instant.

The venue's own `midpoint` is not used. On 9 Nov the book had no asks at all and the venue still printed `midpoint 0.7755`; `Book.mid()` is `None` for a one-sided book. A price that is not displayed is not a price we can trade. The mid is for the Brier comparison and markouts only.

The book was thin on every window checked: often 100 shares a side at 10–15¢ apart, and once nothing on one side. Volume per window was a few dollars. Expect most windows to be skips and most paper quotes never to fill. `settings.minSize` (50 shares) is the minimum for LP rewards, not an order minimum; no order minimum and no tick are published. `decide` quotes on a 1¢ grid, which is valid on a thousandths book.

Maker orders, when a later phase sends them, are post-only. A paper quote that would cross the opposite touch is not rested (`paper.rest`). The venue publishes a taker delay (`takerDelayMs`, 250 on 9 Nov); log it, do not branch on it.

There is no public trade stream. The websocket (`wss://ws.limitless.exchange`, Socket.IO namespace `/markets`) carries the coalesced book only; trades are on a cached REST endpoint. The queue model needs trade prints, so Phase 2 has to read those events and accept that they lag.

### Reference prices

`K` is `metadata.openPrice`: the Chainlink BTC/USD 60 s TWAP captured at the open (18-decimal precision). If it is missing, the window is not priced.

The settlement close is `metadata.chainlinkTwapResolution.close.raw / 1e18`. Up when close ≥ K. A tie is Up. Redeem from the official result (`status: RESOLVED`, `winningOutcomeIndex`); a 50-50 resolution pays $0.50 a share and is recorded as no outcome.

`sigma_1m` is the standard deviation of the last 60 one-minute log returns of Chainlink BTC/USD spot, from `GET /markets/{slug}/oracle-candles`. Those candles are spot, not TWAP: on 9 Nov the mean of the minute before each bell matched the official open and close TWAP to within $1–4 (under 0.5 bp), while the bar at the bell was $9–43 off. They publish about two minutes late; `sigma_from_candles` uses the newest 61 consecutive bars if they end within five minutes, and returns `None` on any missing minute.

`S` is a proxy: the median of the Coinbase and Kraken BTC/USD mids, stale after 1.0 second. Live Chainlink spot needs credentials this project does not have, and Pyth's public endpoint now answers 401. This is a deviation from "S is the Chainlink spot", so its cost is measured every run: a `basis` record per minute (proxy vs the Chainlink bar close) and, per window, the error of the close TWAP rebuilt from proxy seconds against the official close. The final-minute formula uses one proxy price per completed second; a hole means `None`.

Inside the formula the lookback is 60 seconds and the variance offset 40 seconds. With more than 60 seconds left, `z = ln(S/K) / (sigma_1m * sqrt(max(seconds_left − 40, 1) / 60))` and `p_up = Φ(z)`. With 60 or fewer, the printed seconds are locked and only the unfinished average is random. Probability is capped to [5%, 95%] above 120 seconds left; a stored value outside that is a bug.

### Fees

On new trading venues (`venue.feeModuleType == "COLLATERAL"`, `metadata.fee: true`) the taker pays `rate_bps × N × (S − N) / (10000 × S)` in USDC, N the USDC before fee and S the shares, rounded half-up to 0.00001 per match. With N = price × S this is `shares × rate × p × (1 − p)`, the same polynomial as `fees.fee_usdc`, with rate 0.05 at the standard 500 bps. Makers pay nothing. The docs' example — 100 shares at $0.40 cost $40 plus $1.20 — is a test.

At 0.05, `fee_per_share(0.50)` is 1.25¢ and the taker break-even at 50¢ is 51.25%. The maker rebate multiplier was 0.3 (`settings.rebateRate`). The rebate stays a separate line and never enters the judged result.

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

The judged result is after fees and before rebates. Sum the settlement results. That sum is the only P&L the gates look at. Rebates are a separate line: for each maker fill, log `rebateRate × fee_per_share(fill price, feeRate) × shares` as an estimate of the maker rebate, using `settings.rebateRate` read from that market (0.3 on the 9 Nov check). Do not add that line to the judged result. Do not add a leaderboard taker-rebate tier. A new account does not inherit one, and this plan does not raise size to chase one.

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
- One clip per window. At most one taker clip, and resting size of at most one clip per side. The clip is config. Limitless publishes no order minimum; `settings.minSize` (50) is only the rewards threshold. No add, no martingale, no second clip after a fill or a miss.
- Open exposure. The operator sets a cap on the open paper cost (price times shares, plus taker fees already charged). An order that would exceed it is skipped.
- Pre-rebate P&L. The figure that trips the daily loss, and the figure Phase 2 has to show, is the judged result: after fees and before rebates.

A dedicated trading wallet appears only in Phase 3. This plan does not ask for a private key. Phase 1 does require the three CLOB API credentials named above (`apiKey`, `secret`, `passphrase`). Those credentials are not a private key, and they are not committed. When Phase 3 exists, the trading key stays in a separate gitignored env file, on a wallet funded only with the test budget.

## Build phases

The phased build starts from the existing pure fee, fair-value, and decision code. Record live windows until the local Up/Down call matches Limitless's resolution. Then run the paper engine. Live capital stays out until that paper record exists.

### Phase 1 — read-only recorder

`fivemin.recorder` resolves the current and next slug, reads the Up book once a second (Down is the mirror), polls the spot proxy, refreshes Chainlink candles once a minute, and appends one JSON object per second plus `window`, `basis` and `resolution` records. Each tick has `S`, `K`, `sigma_1m`, seconds left, `p_up`, both best bids and asks with sizes, book age, the session edge, and what `decide` said. Each resolution has the venue's K and close, the official result, the local call, the candle estimates with their errors, and the proxy-rebuilt close with its error. It runs on the VPS under `deploy/run.sh`.

Run at least 50 resolved windows. Gates:

1. The local Up/Down call from the venue's Chainlink `K` and close matches Limitless's official result on every window. One mismatch stops the build.
2. The independent check: the TWAP estimated from Chainlink spot candles at each bell gives the same call wherever the margin is wider than 1 bp. One disagreement outside that band stops the build. Inside it the window is counted as ambiguous, not judged.
3. Score `p_up` with `brier` against the outcome, next to the book mid at 60 seconds after the open. Stop if `Brier(p_up) − Brier(mid)` is greater than 0.02, or if any stored `p_up` with more than 120 seconds left is outside `[0.05, 0.95]`. On a book this thin the mid is often missing; windows without one are not scored.

Also read, not gated: the proxy basis and the proxy-rebuilt close error. If they are large next to the edges `decide` acts on, the proxy is the problem and no fill logic is written yet.

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

- Get a legal answer on real-money prediction markets for where the operator is. Without it, stop here. Do not route around any block.
- Use a dedicated wallet on Base. Confirm one $1 buy, one redeem, and the venue's fee-module approval on the venue the market names (`venue.exchange`, `venue.feeModule`). Do not depend on inventory logic before that $1 round trip succeeds.
- Then post-only quotes at the clip, and run a tested daily-loss halt that cancels resting orders. Compare live one-second markouts with the paper markouts on the same windows.
- Takes stay off until paper takes, after fees and after failed cancels, are positive on a later stretch of days than the one used to inspect the rule.
- The under-10¢ bid stays paper-only.

### Phase 4 — other books stay closed

No ETH, SOL, XRP, or 15-minute market. They share the BTC move, so they are one bet, and the 5-minute table does not transfer. Add nothing until a later decision, after BTC 5-minute live matches paper. That decision is outside this plan.

## What the leaderboard does not specify

The census below was taken on Polymarket's BTC 5-minute tape on 9 Nov 2026, before the move. Limitless's book is far thinner, so treat it as a description of who sits on these books, not as numbers to reuse. It is a constraint on the gates, not a strategy to copy. Always-on bots are the book: a few percent of wallets post most of the maker size. As a group their trading P&L was a loss, and rebates made the group slightly positive. The wallets that finished up were mostly makers and mostly active on both outcomes. Both-sides is allowed here only as the passive complete set whose bids sum to 99¢ or less. A paired cost above $1 is how a two-sided quote bleeds. Copying a leaderboard wallet one or two minutes later makes you the taker who pays the fee. The late directional load on the SLIP-ME tape is one wallet, not a rule, and the pre-registered test of that idea lost money. Hold to redeem. Do not chase a rebate tier. The largest losses in the census were large.

## Limits of this plan

The queue model is an approximation. A paper book cannot fully reproduce queue position or the taker-delay race against other live orders. Paper P&L is not a live fill.

`decide` does not enforce one clip per window, does not drop a take under 10¢, and does not halt on a silent feed. The engine does. `fair_up` prices only a 60-second lookback. Anything else is a skip. Fee venue, lookback and stream type are read per market. The 9 Nov 2026 values are the check. `S` is a measured proxy, not Chainlink.

Success is the Phase 1 match to Limitless's resolution, a `p_up` that is not a wide miss of the book mid, and a Phase 2 log whose judged result is after fees and before rebates and whose maker markout is inside the 3¢ cushion. There is no profit target. This document is the plan only.

## Findings from the raw payloads (9 Nov 2026)

- Limitless resolves this series from the same Chainlink BTC/USD 60 s TWAP stream as the plan's original venue and records both ends in `chainlinkTwapResolution` (open and close raw values, feed id, `offsetSeconds`). The resolved 08:50 window: K 74263.908, close 74422.150, Up.
- `GET /markets/{slug}/oracle-candles` is public and returns Chainlink BTC/USD spot in 1-minute bars, about two minutes late.
- One book, YES token, 6-decimal sizes. The venue's `midpoint` is printed even for a one-sided book.
- The new-venue taker fee is exactly the plan's polynomial with rate 0.05.
- Thin book: 100-share levels 10–15¢ apart, a few dollars of volume per window.
- PolyBolt (Chainlink spot) needs CLOB credentials; Pyth Hermes answers 401. Coinbase and Kraken are public and reachable from the VPS, hence the proxy.

## Why the venue changed (9 Nov 2026)

`polymarket.com` and its API hosts are under a governmental blocking order for India. Google Public DNS answers `REFUSED` with Extended DNS Error 16 (Censored): "subject to a judicial injunction or other governmental blocking order for IN", Lumen notice 41503850, from an Indian VPS (AS152565) and from an Indian home connection. No resolver change, hosts entry, VPN or offshore server was used to get around it, and none should be. Limitless and every source above resolve normally from the same VPS through the same resolver. That is a statement about DNS on that day, not a legal opinion about Limitless; Phase 3 needs that opinion first.
