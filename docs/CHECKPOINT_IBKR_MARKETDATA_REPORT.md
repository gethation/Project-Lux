# Checkpoint — IBKR Market Data Slice

Branch `feature/ibkr-marketdata`. Tasks 4.1–4.6 of
`docs/IMPLEMENTATION_SPEC_IBKR_MARKETDATA.md` are complete.

4.1 and 4.2 were implemented by the delegated agent; it then stopped correctly under
the stop-and-ask rule when a pre-existing flaky test blocked its commit gate (see §9).
4.3–4.6 were implemented by the reviewing agent after that block was resolved.

| Commit | Task |
|---|---|
| `78f8c24` | 4.1 connectivity diagnostic |
| `ad7ad77` | 4.2 subprocess-isolated IBKR client |
| `c0a4acd` | (unblock) Fubon worker-rebuild test spawn headroom |
| `6d5573c` | 4.3 UMC quote provider with delayed-data banner |
| `0b39bd8` | 4.4 chunked UMC 1m historical fetch |
| `cc6ab94` | 4.5 read-only IBKR account and positions |
| `396a71f` | 4.6 UMC 1m accumulation script |

---

## 1. Probe output

Run against the live account on 2026-07-23 before any code was written, so the design
rested on measured behaviour rather than assumption.

```text
1. CONNECT  127.0.0.1:4001 clientId=77
  connected      : True
  server version : 178
  accounts       : 1 found
  account type   : live (U-prefix)

2. RESOLVE UMC CONTRACT
  matches: 1
    conId=46613372 UMC STK SMART primary=NYSE cur=USD
    longName='UNITED MICROELECTRON-SP ADR'
    timeZoneId=US/Eastern
    tradingHours='20260723:0400-20260723:2000;...'

3. MARKET DATA TIER
  requested 1 (LIVE)    -> served 1, no data
    ERROR [10089] requires additional subscription; delayed data available
  requested 3 (DELAYED) -> served 3 (DELAYED)
    bid=nan ask=nan last=21.39 close=21.29

4. HISTORICAL 1m
  whatToShow=TRADES   useRTH=True    390 bars  2026-07-22 13:30 -> 19:59 UTC
  whatToShow=MIDPOINT useRTH=True      0 bars  ERROR [162] No market data
                                               permissions for NYSE STK
  whatToShow=TRADES   useRTH=False     67 bars (extended hours, partial day)

5. READ-ONLY ACCOUNT
  open positions: 0
  AccountType: INDIVIDUAL / NetLiquidation: 150.00 USD
```

A follow-up probe measured depth and per-request volume:

| `durationStr` | bars |
|---|---:|
| `1 D` | 390 |
| `5 D` | 1,950 |
| `10 D` | 3,900 |
| `1 M` | 8,190 |
| `2 M` | 15,600 |

Depth reaches **at least 2 years**: one RTH day requested at 7d, 30d, 90d, 180d, 365d
and 730d offsets each returned a full 390-bar session. The documented "six months"
limit applies to bars of 30 seconds or less, not to 1-minute bars.

**Conclusions that shaped the implementation**

- `TRADES` works without a NYSE subscription; `MIDPOINT` does not. Use `TRADES` only.
- Quotes are delayed (tier 3). Warn loudly, continue.
- Historical backfill is unaffected by the missing subscription — a bar from last week
  is complete regardless of the real-time entitlement.

---

## 2. Test result

```text
453 passed, 8 skipped
```

Reconciliation against the 425/8 baseline on `master`:

| Change | Effect | Running |
|---|---:|---:|
| Baseline (`master`) | — | 425 / 8 |
| 4.1 diagnostic tests | +4 | 429 / 8 |
| 4.2 client-process tests | +6 | 435 / 8 |
| 4.3 quote-provider tests | (included above) | 435 / 8 |
| 4.4 historical tests | +11 | 446 / 8 |
| 4.5 read-only broker tests | +7 | 453 / 8 |
| **Final** | **+28** | **453 / 8** |

The eight live-gated smoke tests remain skipped and unmodified.

---

## 3. Replay golden

Unchanged. `tests/integration/test_replay_golden.py` is green inside the 453-test run
above; it re-runs the fixture replay and compares every field of the committed golden
summary (integers and strings exactly, floats at `1e-9` relative).

This slice touches no strategy code, so the expected result was no movement at all —
and there was none.

---

## 4. Implementations and how they map onto existing protocols

### `IbkrUmcQuoteProvider` (`integrations/ibkr/market_data.py`)

Implements the existing `QuoteProvider` shape (`fetch_quote`, `session_health`,
`close`) used by the Fubon, Binance and BitoPro adapters.

`LiveQuote` gained two optional fields, `market_data_tier` and `is_delayed`. Both
default so every existing construction site is unaffected. The provider **refuses to
return a quote whose served tier is unknown** — a delayed feed must never be able to
pass as live through a missing field.

### `IbkrReadOnlyBroker` (`integrations/ibkr/readonly.py`)

Implements the `ReadOnlyBroker` protocol (`broker`, `fetch_snapshot`, `close`) and
maps IBKR's account payload onto `BrokerPositionSnapshot` / `BrokerOrderSnapshot` /
`BrokerMarginSnapshot`. `BrokerName` gained `IBKR`.

Two deliberate choices:

- The `LUX_READONLY_BROKER=1` gate lives in `integrations/env.py`, not
  `cli/helpers.py`. Same variable and message, but integrations must not import from
  cli. A test asserts construction is refused when the variable is unset.
- An unrecognised order action maps to `side=None` rather than guessing a direction.

### `fetch_umc_1m_history` (`integrations/ibkr/historical.py`)

Walks backwards in chunks, merges, de-duplicates, and reports. Kept separate from
`client_process.py` so the transport stays a thin request/response layer.

---

## 5. Chosen defaults, with reasoning

| Setting | Value | Why |
|---|---|---|
| Chunk duration | `2 M` | Measured 15,600 bars per request; 3 months is 2 requests, 2 years about 12 |
| Request spacing | `11.0s` | The binding IBKR limit is 60 requests per 10 minutes. 11s is comfortably inside it, and at this volume the conservatism is free |
| `clientId` | diagnostic 17_001, quotes 17_002, read-only 17_003, accumulator 17_004 | Distinct per purpose so two components never collide on one id; all configurable |
| Connect timeout | 8.0s | A Gateway that is up answers immediately; a Gateway at the login screen never will, and should be reported rather than waited on |
| Request timeout | 60.0s | Above the longest observed historical request |
| Quote wait | 10.0s | A delayed feed can take several seconds to deliver a first tick |

Pacing rules honoured (all three bind simultaneously): identical requests >15s apart,
fewer than 6 per 2s per contract, no more than 60 per 10 minutes. Requests here are
never identical because `endDateTime` advances every chunk.

---

## 6. DST handling

The UMC RTH session is 09:30–16:00 **US/Eastern**, which is 21:30–04:00 Taipei in US
summer and 22:30–05:00 in winter. A hardcoded Taipei offset would break silently twice
a year.

The code path is `integrations/ibkr/calendar.py::umc_rth_session`, which builds the
session on the Eastern clock with `zoneinfo` and converts:

```python
eastern = ZoneInfo(market_time_zone_id)          # "US/Eastern"
opens_eastern = datetime.combine(market_date, time(9, 30), tzinfo=eastern)
return ... opens_eastern.astimezone(TAIPEI_TZ)
```

Verified live:

```text
2026-01-15  ET 09:30-16:00  ->  Taipei 01-15 22:30 - 01-16 05:00
2026-07-15  ET 09:30-16:00  ->  Taipei 07-15 21:30 - 07-16 04:00
```

The same shift is directly visible in IBKR's own data: a January historical request
returns `14:30–20:59 UTC` where a July one returns `13:30–19:59 UTC`.

Two supporting decisions:

- `formatDate=2` is mandatory. Any other value makes IBKR return timestamps in the
  timezone selected on the Gateway login screen — operator-configurable state that
  would silently redefine every stored bar.
- `endDateTime` is always formatted in explicit UTC for the same reason.

---

## 7. Historical fetch result

Live verification of the fetcher (5 chunks of `5 D`):

```text
chunks requested : 5
bars             : 9,536 unique (0 duplicates dropped)
sessions         : 25
range            : 2026-06-17 21:30 +08:00 -> 2026-07-24 00:25 +08:00
complete sessions: 24/25 at 390 bars
Taipei hours     : [0, 1, 2, 3, 21, 22, 23]
```

The single incomplete session is the one in progress at fetch time. No interior
session was flagged.

First real accumulation run:

```text
D:\Users\Documents\Proof of Concept\data\processed\umc_1m_cumulative.csv
32,550 bars / 84 sessions / 2026-03-24 21:30 +08:00 -> 2026-07-24 00:29 +08:00
two requests, no duplicates, no incomplete interior session
```

Re-running added only the two minutes that had genuinely elapsed since, confirming the
merge is idempotent. The schema matches the PoC's OHLCV convention
(`timestamp,open,high,low,close,volume`, Taipei `+08:00`) so downstream research
scripts consume it unchanged.

A completeness note that matters for later analysis: a full RTH day is exactly **390**
one-minute bars. The fetcher names any interior session that is not 390 rather than
smoothing it over, because a short day means data is missing and forward-filling it
would launder that into a plausible-looking series.

---

## 8. No order path — mechanical check

```bash
grep -rniE "placeOrder|cancelOrder|reqGlobalCancel|placeOrderAsync|LimitOrder|MarketOrder" \
  --include=*.py lux_trader/integrations/ibkr/ scripts/accumulate_umc_1m.py
# (no matches)
```

Every `ib_async` call used anywhere in the slice:

```text
ib.accountSummary   ib.cancelMktData   ib.connect          ib.disconnect
ib.isConnected      ib.managedAccounts ib.openTrades       ib.positions
ib.reqContractDetails  ib.reqHistoricalData  ib.reqMarketDataType
ib.reqMktData       ib.sleep
```

All read-only. Connections additionally pass `readonly=True`
(`client_process.py:157`, `diagnostic.py:114`), and the Gateway itself has Read-Only
API enabled as a further backstop. A unit test asserts `IbkrReadOnlyBroker` exposes no
`place_order` / `placeOrder` / `execute` / `cancel_order` attribute.

---

## 9. The blocking issue that interrupted delegation, and its resolution

The delegated agent completed 4.3 but could not commit it: the commit gate requires a
green `pytest -q`, and two **pre-existing, unrelated** Fubon tests were failing —
`test_initial_realtime_timeout_terminates_and_rebuilds_worker` and
`test_reconnect_timeout_terminates_and_rebuilds_worker`. It stopped and asked instead
of weakening someone else's test to unblock itself, which was the right call.

Investigation:

| Condition | Failure rate |
|---|---|
| With the IBKR changes present | 2 / 5 |
| **With them stashed** | **4 / 5** |

Removing the IBKR work made it *worse*, so it was never the cause. Direct measurement
of a cold worker spawn found the real reason:

```text
min 1.730s   median 1.774s   p90 1.853s   max 1.924s   (10 samples)
```

Both tests require the *replacement* worker to answer inside
`init_timeout_seconds=2.0`. A cold Windows worker needs ~1.8s, leaving roughly 4%
headroom — the tests were always operating at 92–96% of their budget. Starting IB
Gateway added enough competition to push them over regularly.

Fixed in `c0a4acd` by raising those two tests to 5.0s (~2.6x headroom, verified 5/5
stable, and 10.0s was tried and found unnecessary). Assertions are untouched. The
third test in the file keeps its 1.0s because both of its workers hang by design and
never need to answer. **Production is unaffected** — the live provider uses
`DEFAULT_INIT_TIMEOUT_SECONDS = 30.0`; the 2.0s existed only inside those two tests.

---

## 10. Open questions

None blocking. Two facts worth carrying forward:

1. **Delayed data limits what a CCF/UMC dry-run can tell you.** CCF arrives from Fubon
   in real time while UMC would be ~15 minutes stale — not a lagged spread but a
   spread built from two different moments. Measured on the existing 5m dataset, that
   disagrees with the true entry signal on **17.7% of bars** (118 false entries, 180
   missed), against 0.95% for the FX-granularity question judged acceptable earlier.
   The dry-run will validate connection, parsing, bar building and scheduling; it will
   not tell you how the strategy behaves until a NYSE subscription is in place.

2. **The account holds 150 USD.** One CCF contract is ~312,000 TWD ≈ 9,800 USD, so the
   matching UMC leg is roughly 460 shares. Funding is required before CCF/UMC can
   trade, independently of the market-data work.

Out of scope and untouched, as specified: the CCF/UMC pair config, order placement,
borrow checks, and recall detection.
