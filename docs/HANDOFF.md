# Handoff — CCF/UMC, as of 2026-08-05

Branch `ccf-umc`, pushed, clean. **586 passed / 7 skipped.** The replay golden is
byte-identical and must stay that way: **18 trades, net 235726.65723246636, fee
20134.405122268032** (`configs/replay.fixture.ccf_umc.toml`).

Read `docs/CCF_UMC_PLAN.md` for the full record. This file is the short version
plus the things that will bite you.

---

## What the system is

One process trading one pair: **CCF** (聯電 stock futures, TAIFEX, 2000
shares/contract) via **Fubon**, against **UMC** (NYSE ADR, 5:1) via **IBKR**,
converted with USD/TWD from Twelve Data.

Session = TAIFEX ∩ NYSE RTH = **Taipei 21:30–04:00** (US summer). The pair never
trades the TAIFEX day session.

Live entries score on a **directional bid/ask z-score**, not the mid z-score the
backtest uses. That difference is measured — see "the haircut" below.

---

## Verified against reality (not just tests)

- **Single-leg real orders, both directions**, 1 share each, 2026-08-04.
  `position=-1` on the short means Reg T permission, an actual **borrow
  delivery**, and buy-to-cover all hold — the path roughly half the backtest's
  trades need, which nothing had ever executed.
- **`strategy → sizing → price_policy → validator → fill`**, 63/63 checks, both
  directions, in a dry-run soak.
- **`umc_fee_model = 'ibkr'`** on both buy and sell sides, and against one real
  IBKR bill.
- **Venue self-heal**: IB Gateway died mid-session; the loop retried every second
  and resumed on its own when it came back.

## NOT verified — do not assume from a green suite

- **Both legs at once.** Everything so far is one leg at one venue, so the state
  the pair actually fears — CCF filled, UMC unknown — has never occurred.
- **`unknown` → PAUSE.** Only ever run against fakes.
- **D5 position drift, margin panel, broker reconciliation.** `LUX_READONLY_BROKER`
  was off for every soak. This is the biggest "assumed working" block, and it is
  the cheapest to close: turn the env var on for the next soak. It is read-only.
- **Forced exits** (rollover, weekend). Rewired in Phase B, never fired live.
- **Recall** (D6). Cannot be manufactured; needs the broker to actually recall.

---

## Traps that have already caused real bugs

**1. An order status is a report; an execution is a fact.**
ib_async 2.1.0 reports an order `Cancelled` when IBKR emits warning 10349, and
IBKR fills it a second later. On 2026-08-04 this made the system say "failed,
safe, nothing to flatten" while holding a share, and then "CRITICAL, still
holding" while flat. `failed` now has to be *earned*: no executions for that
order id, no position movement, still true after a settle wait. **Never decide a
non-fill from a single position snapshot** — the position view lags the fill.
`ib_async` is pinned at 2.1.0 for this reason; re-test before bumping.

**2. The FX rate is a scalar conversion, not a book you cross.**
Twelve Data serves no bid/ask, so `usd_twd.bid`/`.ask` are always `None`. This
same confusion has been fixed in **three** places (`core/tradable_spread.py`,
`market_data/minute_bar.py`, `execution/price_policy.py`). When you touch
anything FX-shaped, ask: *converting, or crossing?* Directionality comes from
UMC's own book.

**3. Test doubles that are narrower than reality pass forever.**
The price-policy fixture gave USD/TWD a bid/ask that has never existed, so the
tests stayed green while live rejected every order. The IBKR CLI double had only
`preflight()`. **Make doubles match the real interface**, including the fields
that are always `None` in production.

**4. Comments that stopped being true have caused bugs twice.**
"this account has no bid/ask permission" and "the IBKR execution adapter lands in
Phase D" both misled real changes. If you make a claim false, fix it in the same
commit.

**5. Do not run heavy compute on the machine running the live loop.**
A four-way replay sweep starved the IBKR worker's event loop:
`ticker_advanced` fell 95% → 0%, 42.7% of quotes aged past their budget, bars
were dropped. It reads as a degraded feed and the cause is local.

**6. `replay` and `summary` must work with no brokerage packages installed.**
That is why every Fubon/IBKR import is function-local. A module-scope import
broke the whole CLI and had no symptom on a dev box.

---

## Numbers worth knowing

**The haircut** — the gap between the mid z-score the backtest scores on and the
directional z-score live must cross: **p50 +0.19**, stable across |mid z| 0.0–1.24
and across two independent runs. It is a *constant*, so it acts as a threshold
shift.

Re-running the golden at `entry_z = 1.69` gives **13 of 18 trades and 82.4% of
net at an identical max drawdown**. ⚠️ The obvious alternative — deleting trades
whose entry z fell below 1.69 — gives 38.7% and is **wrong**: a trade not entered
at 1.55 is entered later, when the spread widens further. **Re-run the threshold;
do not filter.**

Exits are shifted the same way (observed: mid crossed zero at 23:03, the
executable side at 23:06), and that side is still unmodelled.

**Quote cadence differs by two orders of magnitude** across venues. Over 17,417
polls: UMC gave 16,497 distinct timestamps, CCF **3,596** (longest frozen run 99
polls), Twelve Data **65** (run of 270). An old timestamp on an illiquid
instrument means *nothing traded*, not *the price is wrong* — which is why
staleness budgets are per-source and why `leg_timestamp_skew` fires occasionally
(~2%) and is left alone.

**Sample is thin.** 18 trades, all winners, over seven weeks; 13 after the
haircut. A perfect record is a warning, not a reassurance.

---

## Running it

```bash
# health of the UMC feed (read-only, needs LUX_LIVE_MARKETDATA=1)
python -m lux_trader status doctor --config <cfg> --mode ibkr

# dry-run soak (no orders possible)
python -m lux_trader live --config configs/config.live.ccf_umc.dryrun.local.toml --mode dry-run --reset-store

# tests
conda run -n Quant python -m pytest -q
```

Configs, all `configs/`:

| file | `allow_live_order` | use |
|---|---|---|
| `config.live.ccf_umc.dryrun.local.toml` | false | the real-threshold soak |
| `config.live.ccf_umc.lowz.local.toml` | false | `entry_z=0.3`, **path coverage only — its PnL is meaningless** |
| `config.ibkr.smoke.local.toml` | **true** | 1-share IBKR smoke; `live_execution.enabled` still false |

Real orders additionally need env gates: `PROJECT_LUX_ALLOW_LIVE_ORDER=1`,
`IBKR_ALLOW_LIVE_ORDER=1`, plus `LUX_IBKR_EXECUTION_SMOKE=1` for exec-smoke.
`admin manual-close` deliberately has no extra gate — the emergency exit must not
be harder to reach than the entrance.

---

## Suggested next steps

1. **Next soak with `LUX_READONLY_BROKER=1`.** Closes D5 drift, margin panel and
   reconciliation in one go. Read-only, low risk.
2. **E4** — one lot, both legs, attended. Needs a real signal: executable z must
   reach 1.5, and it peaked at 1.24 over a full session. Not schedulable.
3. **Confirm CCF's actual Fubon per-contract fee.** 88 TWD is QFF's, used as a
   placeholder. Also confirm SEC/FINRA rates (`RATES_AS_OF = 2026-07-25`).
4. **Model margin interest.** `integrations/ibkr/fees.py` has borrow cost but not
   the interest on a long UMC leg held on margin.

## Blocked on the operator, not on code

- IB Gateway must be logged in; its auto-restart time was moved out of the
  session (it used to fire at ~23:45, mid-session).
- CCF history is accumulated **manually**; TAIFEX only serves 30 days. Unattended
  running needs a scheduler for this.
- Fubon allows **one SDK session per account**. QFF/TSM was retired 2026-08-03 to
  free it.

## Current state

Soak stopped, IB Gateway down, no positions held (verified against the account,
not against local state). Nothing is running.
