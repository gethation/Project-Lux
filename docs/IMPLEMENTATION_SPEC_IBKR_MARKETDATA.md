# Implementation Spec — IBKR Market Data Slice

**Audience:** the coding agent implementing this work.
**Reviewer:** the planning agent, who verifies before this is considered done.
**Context:** `docs/MULTIPAIR_PLAN.md` §6 (Phase 3). This slice is the *market-data
only* portion, pulled ahead of the rest of Phase 3 because the execution side is
blocked by the account type and because this unblocks the CCF/UMC dry-run.

Phase 0 and Phase 1 are complete and merged to `master`. Read
`docs/IMPLEMENTATION_SPEC_PHASE_0_1.md` §0 for the working agreement — the
stop-and-ask rule, scope discipline, and commit rules all still apply unchanged.

---

## 0. What this slice is and is not

**In scope**

| Capability | Notes |
|---|---|
| Connection management | Reuse `lux_trader/integrations/subprocess_transport.py` (extracted in Phase 0.2 for exactly this) |
| Real-time quote for UMC | Implements the existing `QuoteProvider` protocol |
| Historical 1m bars | For warmup and for building research history |
| Read-only account / positions | Implements the existing `ReadOnlyBroker` protocol |
| A UMC 1m history accumulation script | Mirrors `accumulate_taifex_1m.py` in the PoC repo |

**Explicitly out of scope — do not write any of this**

- Order placement, modification, or cancellation
- Borrow / shortability checks
- Recall detection and the proportional CCF unwind (plan §5c)
- The CCF/UMC pair config itself (a separate follow-up)

---

## 1. ABSOLUTE PROHIBITION — this connects to a LIVE account

The user's IBKR account is a **live account holding real money**. There is no
sandbox on your side.

- **Never call `placeOrder`, `cancelOrder`, `reqGlobalCancel`, or any order-related
  API.** Not to "test the path", not behind a flag, not in a test file.
- The finished code must contain **no order-placement call anywhere**. The reviewer
  greps for this.
- The same prohibitions from the Phase 0/1 spec still apply to Project Lux's own
  commands: never run `live --mode execute`, `admin exec-smoke`, `admin manual-close`.

The user has been told to enable **Read-Only API** in IB Gateway, which makes the
platform itself reject order messages. Treat that as a backstop, not a licence —
your code must be correct without it.

---

## 2. Environment

IB Gateway (stable), logged into the **live** account.

| Setting | Value |
|---|---|
| Socket port | **4001** (Gateway live). Gateway paper is 4002; TWS is 7496/7497 |
| Host | `127.0.0.1` |
| Read-Only API | Enabled |
| Trusted IP | `127.0.0.1` |

**Every one of these must be configurable** — port especially, because the user may
switch between Gateway and TWS. Do not hardcode.

New dependency: **`ib_async`** (the maintained fork of `ib_insync`; the original was
retired in 2024). This is the one approved new third-party dependency — add it to
`requirements.txt`. Do not add anything else without asking.

Python runs through Miniconda `Quant`:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m pytest -q
```

---

## 3. Known constraint — the account has no NYSE real-time subscription

Confirmed with the user. Two consequences:

1. **Quotes will be delayed ~15 minutes.** Use `reqMarketDataType`; IBKR reports
   which tier it actually served (1 = live, 2 = frozen, 3 = delayed, 4 =
   delayed-frozen).

   **Policy (decided): warn loudly and continue.** On startup, print an unmissable
   `DELAYED MARKET DATA` banner, record the served tier in the store, and surface it
   in the terminal UI. Never let a delayed tier be mistaken for live. Do not refuse
   to start — this slice exists to validate connection, parsing, and scheduling, and
   that work is valid on delayed data.

2. ~~**Historical 1m may be refused entirely.**~~ **RESOLVED — the reviewer ran the
   probe on 2026-07-23. Historical 1m TRADES data IS available.**

   | Request | Result |
   |---|---|
   | `whatToShow="TRADES", useRTH=True` | **390 bars** — exactly one RTH session (09:30–16:00 ET) |
   | `whatToShow="TRADES", useRTH=False` | 67 bars — extended hours, partial day |
   | `whatToShow="MIDPOINT"` | **Error 162** — `No market data permissions for NYSE STK` |
   | Delayed quotes (`reqMarketDataType(3)`) | Served tier 3, `last=21.39 close=21.29` |
   | Live quotes (`reqMarketDataType(1)`) | Error 10089, no data — as expected |

   **Use `TRADES`. Never `MIDPOINT`** — it needs bid/ask permissions this account
   does not have. `TRADES` is also what the spec already required, because it matches
   the PoC's TradingView RTH data.

   The 390-bar count is a useful self-check: a complete RTH session is exactly 390
   minutes, so a full trading day that returns anything else means something is wrong.

---

## 4. Tasks

Commit each task as soon as it is green. Run `pytest -q` before every commit.

### 4.1 — Connectivity diagnostic (already probed; make it reproducible)

The reviewer has already run this probe manually — §3 records the results, so the
unknown that gated the rest of this slice is resolved. Your job is to turn it into a
maintained diagnostic the operator can re-run, wired into `status doctor` or an
equivalent, reporting:

- connection, server version, and account
- the market-data tier actually served (never let a delayed tier read as live)
- a 1-day historical 1m probe with the exact error code on failure
- the resolved contract details

**Verified reference values** (2026-07-23, live account):

```text
server version : 178
UMC contract   : conId=46613372, SMART/NYSE, USD
longName       : 'UNITED MICROELECTRON-SP ADR'
timeZoneId     : US/Eastern
tradingHours   : 20260723:0400-20260723:2000   (includes extended hours)
```

Use exactly this contract, and resolve it via `reqContractDetails` asserting exactly
one match rather than assuming — ADRs occasionally collide with other listings:

```python
Stock("UMC", "SMART", "USD", primaryExchange="NYSE")
```

Note `tradingHours` spans 04:00–20:00 ET (extended hours), while the strategy trades
the 09:30–16:00 RTH session. **Do not derive the session window from `tradingHours`**
— use `useRTH=True` and the RTH clock.

### 4.2 — Subprocess-isolated IBKR client

Follow the Fubon pattern: the `ib_async` event loop lives in its own process, and
the parent talks to it through `subprocess_transport`. Rationale: `ib_async` runs
an asyncio loop that must not contend with the live runtime's synchronous loop, and
a hung broker connection must never take the trading process down with it.

- Timeouts are **parameters, not constants** — the Phase 0.2 transport already
  works this way. Pick sensible defaults and state them in the report.
- Handle IBKR's connectivity events (1100 lost / 1101 restored-with-data-loss /
  1102 restored) and expose connection health the way
  `FubonFutureExecutionProcess.session_health()` does.
- `clientId` must be configurable and must not collide with any other connection.

### 4.3 — `QuoteProvider` for UMC

Implement the existing protocol used by the Fubon / Binance / BitoPro market-data
adapters. Read those first and match their shape — quote staleness handling,
reconnect behaviour, and error surfacing should look familiar, not novel.

- Normalize timestamps to **Taipei** (`lux_trader/core/time.py`), like every other
  provider.
- Surface bid / ask / last, and mark whether the data is delayed.
- UMC RTH is 09:30–16:00 ET, i.e. 21:30–04:00 Taipei during US DST and 22:30–05:00
  outside it. **Do not hardcode the Taipei offset** — derive it from the US market
  calendar. A hardcoded offset silently breaks twice a year.

### 4.4 — Historical 1m fetch

`reqHistoricalData` with `barSizeSetting="1 min"`, `whatToShow="TRADES"`,
`useRTH=True` (the strategy trades the RTH session; the PoC's TradingView data is
RTH trades, so this keeps the research and live paths aligned).

- IBKR paces historical requests (roughly: no more than ~60 requests per 10 minutes,
  and identical requests get throttled). Implement chunking with backoff and make
  the pacing parameters configurable.
- Output must match the PoC's OHLCV CSV schema so downstream scripts run unchanged:
  `timestamp,open,high,low,close,volume` with Taipei `+08:00` timestamps.

### 4.5 — `ReadOnlyBroker` for account and positions

Implement the existing protocol (`lux_trader/reconciliation/brokers.py`). Gate it
behind the same `LUX_READONLY_BROKER=1` environment variable the Fubon and Binance
read-only brokers already use — consistency matters more than convenience here.

### 4.6 — UMC 1m accumulation script

Mirror `scripts/accumulate_taifex_1m.py` in the **PoC repo**
(`D:\Users\Documents\Proof of Concept`), which is the reference implementation:
fetch, merge into a cumulative CSV, dedupe by timestamp with fresh data winning,
report conflicts loudly rather than silently overwriting, and detect a coverage gap.

**Target: 3 months** for this first run — enough to validate the pipeline without
fighting pacing limits. The script must support extending the range later.

Unlike TAIFEX's 30-day rolling window, IBKR's history goes back years, so there is
no time pressure here — correctness over coverage.

---

## 5. Invariants

1. **The replay golden baseline must not move.** `rows=29909`, `trade_count=66`,
   `net_pnl_twd=261507.82918245535`, `total_fee_twd=68317.49687897251`. This slice
   should not touch the strategy at all; if the golden moves, you have changed
   something you should not have.
2. **Existing tests stay green.** Baseline: **425 passed, 8 skipped**.
3. **The 8 gated smoke tests stay skipped.**
4. **No order-placement code anywhere.**
5. **`ib_async` is the only new dependency.**
6. Do not modify the QFF/TSM live path. It is the user's production system and it is
   being stabilised in parallel.

---

## 6. Definition of done

`docs/CHECKPOINT_IBKR_MARKETDATA_REPORT.md` containing:

1. **Probe output verbatim** — connection, account, served market-data tier, and the
   historical-data result including any error code.
2. `pytest -q` output, reconciled against 425/8.
3. Replay golden confirmation.
4. The provider and read-only broker implementations, and how they map onto the
   existing protocols.
5. Timeout, pacing, and `clientId` defaults chosen, with reasoning.
6. **How the DST shift is handled**, with the specific code path.
7. Historical fetch result: rows, date range, and the output file path.
8. A mechanical grep showing no order-placement call exists.
9. `docs/HANDOFF_QUESTIONS.md` — anything undefined.

Then stop. Do not start the CCF/UMC pair config or anything on the execution side.
