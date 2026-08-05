# Moving to another Windows machine (for live-execute)

Nothing in `lux_trader/` contains an absolute path — the source is portable.
Everything below is either a config value, a file git does not carry, or a
setting on the machine itself.

Work top to bottom. The verification ladder at the end is not optional: it is
the same order that caught six defects in one night here, and every rung is
cheap.

---

## 1. Config values to edit

For live-execute, only **`configs/config.live.ccf_umc.execute.local.toml`**
matters. Every path in it is repo-relative except one:

```toml
taifex_ccf_1m_csv = 'D:\Users\Documents\Proof of Concept\data\processed\ccf1_1m_cumulative.csv'
```

That is a **fallback only** — the warmup builder prefers Fubon's intraday candle
API and only falls back to this CSV if Fubon cannot fill the window. Either
copy the file and repoint this, or delete the line and accept that a Fubon
outage during warmup means no warmup.

Check the other two while you are there:

```toml
fubon_env_path = '.env'      # must match what the file is actually called
ibkr_port      = 4001        # 4001 = Gateway, 7496 = TWS
```

`ibkr_port` is worth a second look. The quote provider and the order adapter
both read it now, but they did not always: a wrong port used to run healthy all
session and fail at the first order, after the CCF leg had filled.

---

## 2. Files git does not carry — copy by hand

| what | where | note |
|---|---|---|
| `.env` | repo root | Fubon credentials + `TWELVEDATA_API_KEY` |
| `*.pfx` | next to `.env` | Fubon certificate |
| `ccf1_1m_cumulative.csv` | wherever §1 points | warmup fallback, optional |

`.env` must contain at least: `FUBON_PERSONAL_ID`, `FUBON_PASSWORD` (or
`FUBON_API_KEY`), `FUBON_CERT_PATH`, `FUBON_CERT_PASSWORD`, `TWELVEDATA_API_KEY`.

`FUBON_CERT_PATH` may be relative — it resolves next to `.env`. If it is
absolute, it needs editing.

`data/` is gitignored and will be recreated. Do **not** copy a store across:
the live-execute gate reads the last reconciliation report out of it, and a
stale report from another machine would let real orders start on evidence about
positions that are not there.

---

## 3. Python environment

Conda env named `Quant` (the launcher hardcodes that name):

```powershell
conda create -n Quant python=3.12
conda run -n Quant pip install -r requirements.txt
```

Then **`fubon_neo` separately** — it is not on PyPI. Install the wheel Fubon
distributes.

`ib_async` is pinned at **2.1.0** on purpose. That version reports an order
`Cancelled` on IBKR warning 10349 and then fills it; the confirmation path
handles that, but a bump can change event semantics again. Do not float it.

`scripts/lux.ps1` finds conda via PATH, then `%USERPROFILE%\anaconda3`, then
`%USERPROFILE%\miniconda3`, then a hardcoded `D:\Users\miniconda3\...`. The
first three will almost certainly hit; if not, add conda to PATH rather than
editing the script.

---

## 4. Machine settings

**Set the system timezone to Taipei.** The live loop's clock is
`datetime.now().astimezone()` (`runtime/live/engine.py:142`,
`execution/real_coordinator.py:50`), so it inherits the host zone. Comparisons
stay correct either way — everything is timezone-aware — but every recorded
timestamp, log line and measurement in this project assumes Taipei, and a
different host zone makes records that read as a different time of day.

**Windows time sync.** The startup clock-skew gate is fail-closed and checks
against NTP. `w32tm /resync` needs elevation; without it you get a
`windows_time_sync resync_failed` warning at every startup, which is harmless
as long as the skew check itself passes. Confirm the machine's clock is
actually synced.

---

## 5. IB Gateway

Install, log in, and then check each of these. Several were learned the hard
way:

- **API enabled**, socket port matching `ibkr_port` (4001).
- **"Read-Only API" UNCHECKED.** With it on, orders are rejected outright.
- **Trusted IP `127.0.0.1`** if the loop runs on the same box.
- **Auto-restart / auto-logoff time moved OUT of 21:30–04:00 Taipei.** The
  default is 11:45 PM local, which sits in the middle of this pair's session.
  On the old machine it killed a soak mid-run.
- **"Send API messages in English" CHECKED.** Otherwise errors arrive as
  `\u59d4\u8a17\u55ae...` escapes and are unreadable during an incident.
- **Market data**: the account needs NYSE real-time reaching the **API**, which
  IBKR entitles separately from the TWS screen. Verify with the doctor in §7,
  not by looking at the Gateway window.

---

## 6. Fubon: one SDK session per account

**Make sure nothing on the old machine is still running.** Fubon allows one SDK
session per account; two processes will fight, and the symptom is not obvious.
Confirm the old box has no live loop, no soak, and no `admin` command open.

---

## 7. Verification ladder — do not skip rungs

Each step is cheap and each one has caught something real.

```powershell
# 1. the suite. 586 passed / 7 skipped
conda run -n Quant python -m pytest -q

# 2. the golden must be byte-identical:
#    18 trades, net 235726.65723246636, fee 20134.405122268032
conda run -n Quant python -m lux_trader replay  --config configs/replay.fixture.ccf_umc.toml --reset-store
conda run -n Quant python -m lux_trader summary --config configs/replay.fixture.ccf_umc.toml

# 3. IBKR feed: expect tier 1, real bid/ask, shortable shares, historical bars
$env:LUX_LIVE_MARKETDATA='1'
conda run -n Quant python -m lux_trader status doctor --config configs/config.live.ccf_umc.execute.local.toml --mode ibkr

# 4. all three venues at once (Fubon book, UMC book, FX mid)
conda run -n Quant python -m lux_trader status doctor --config configs/config.live.ccf_umc.execute.local.toml --mode live

# 5. warmup must build 2500 bars
conda run -n Quant python -m lux_trader warmup --config configs/config.live.ccf_umc.dryrun.local.toml --reset-store

# 6. a full dry-run session. Orders are impossible from this config.
conda run -n Quant python -m lux_trader live --config configs/config.live.ccf_umc.dryrun.local.toml --mode dry-run --reset-store

# 7. the order gate, with the env vars UNSET. Expect the three config checks to
#    PASS and the env + reconciliation checks to FAIL. That is correct.
conda run -n Quant python -m lux_trader status doctor --config configs/config.live.ccf_umc.execute.local.toml --mode order
```

Only after 1–7 are clean:

```powershell
# 8. one share, both directions, real orders. Needs three env gates.
#    See docs/HANDOFF.md for the exact command.
conda run -n Quant python -m lux_trader admin exec-smoke --config configs/config.ibkr.smoke.local.toml --venue ibkr --confirm-symbol UMC
```

**Verify step 8 against the account, not against the program's own output.** On
the old machine the program reported "failed, safe, nothing to flatten" while
holding a share. Check positions and executions on a separate read-only
connection or in the Gateway itself.

---

## 8. Things that do not travel

- **CCF history is accumulated manually.** TAIFEX serves 30 days. Unattended
  running needs a scheduler for this and there isn't one.
- **The soak measurements are machine- and session-specific.** Quote cadence,
  capture rate and skew all depend on network path and load. Re-measure on the
  new box before trusting the budgets in the config; the numbers recorded in
  `docs/CCF_UMC_PLAN.md` are from this machine.
- **Do not run heavy compute on the new machine either.** A replay sweep here
  starved the IBKR worker and dropped bars; it reads as a degraded feed.
