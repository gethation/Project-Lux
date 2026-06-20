# Project Lux Phase Plan and Test Report

更新日期：2026-06-19

## 1. 專案總覽

Project Lux 是 QFF/TSM 配對交易系統的最小可運行架構。核心程式位於 `lux_trader/`，測試位於 `tests/`。

專案背景是：主要交易邏輯已先在 `D:\Users\Documents\Proof of Concept` 做過想法驗證。PoC 是第一版策略行為的基準來源，包含交易標的、spread/z-score 計算、進出場門檻、部位 sizing、費用、QFF 交易時段假設，以及 replay/backtest 的 reference summary。Project Lux 的任務不是重新發明策略，而是把 PoC 驗證過的行為整理成可部署、可測試、可恢復、可逐步接 live market data 的系統架構。

因此 Phase 1 的核心驗收標準是「Project Lux replay 結果要和 PoC reference 對齊」，包含交易次數、方向、部位、PnL 與費用。之後任何策略規則變更，都應該明確記錄，並重新和 PoC 或新的 reference dataset 驗證。

目前系統已從 Phase 1 的 PoC CSV replay 擴展到 Phase 2 的 live market data + PaperBroker。Phase 2 仍然不允許任何真實下單，只驗證即時/日內行情、warmup、策略資料流與 SQLite recovery 基礎。

## 2. Phase 1 到 Phase 5 目標

| Phase | 目標 | 主要內容 | 下單狀態 |
| --- | --- | --- | --- |
| Phase 1 | PoC CSV replay MVP | 讀取 PoC CSV、重算 rolling z-score、跑 PairStrategy、PaperBroker、SQLite store、resume、summary | 不接 API，不下單 |
| Phase 2 | Live market data + PaperBroker | 接 Fubon marketdata、TAIFEX downloader、Binance/BitoPro ccxt，建立 live warmup、expiry buffer QFF 選約與 1m bar polling | 只做 paper order |
| Phase 3 | Read-only broker reconciliation | Fubon/Binance read-only broker，登入、查部位、查委託、查保證金，啟動時做 broker/store 對帳 | 不送單 |
| Phase 4 | Dry-run execution | 策略產生真實 order intent，但只記錄不送出；驗證雙腿 order intent、風控、失敗處理 | 不送單，只記錄 intent |
| Phase 5 | Minimal live execution | 將 Phase 4 validated execution intent 接到真實 Fubon/Binance execution adapter，加入多重 safety gate、post-trade reconciliation 與失敗即 `PAUSED` | 多重 gate 通過後才允許最小實單 |

Phase 5 是第一個允許真實送單的階段，但預設仍必須關閉。只有 config 與環境變數 safety gate 全部通過、broker/store 對帳成功、且 execution plan 未執行過時，才允許送出最小實單。

## 3. Phase 2 live market data 內容

Phase 2 的目標是確認 live market data pipeline 可以支撐 paper trading：

- Fubon marketdata 可登入並取得 QFF candidates。
- QFF active contract 使用 expiry buffer policy：選出最早到期且距最後交易日至少 5 個營業日的 QFF。
- 如果舊 QFF 持倉期間 eligible active contract 已切換，策略繼續使用舊契約等待 exit signal；若到最後交易日前一個營業日 13:35 仍未出場，觸發 force exit。
- TAIFEX 官方前 30 個交易日期貨每筆成交 CSV ZIP 可下載並聚合成 QFF 1m close。
- Fubon QFF intraday candles 與 TAIFEX fallback 可合併成 QFF warmup source。
- Binance `TSM/USDT:USDT` 可透過 `ccxt binanceusdm` 抓取 ticker/OHLCV。
- BitoPro `USDT/TWD` 可透過 `ccxt bitopro` 抓取 ticker/OHLCV。
- `live-paper` 是 Phase 2 正常系統入口；啟動時會檢查 SQLite seed bars，不足時自動執行 warmup。
- `warmup-live` 可產生 1440 根 seed bars，保留作為 debug / 驗收 / 手動重建工具。
- `live-paper` 每秒 polling quote，但只在 1 分鐘完成後執行策略判斷。
- 全流程仍使用 `PaperBroker`，不呼叫任何 Fubon/Binance 下單 API。

## 4. Phase 2 測試計畫

### 4.1 離線 deterministic tests

目的：驗證我們自己的邏輯，不依賴外部 API。

命令：

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_market_data.py -q
```

覆蓋內容：

- QFF symbol parser、front-month fallback selector、expiry buffer active selector。
- Expiry buffer contract policy：5 個營業日門檻、eligible active symbol 切換、T-1 13:35 force-exit deadline。
- Fubon symbol `QFFG6` 對應 TAIFEX contract month `202607`。
- TAIFEX HTML CSV ZIP link parser。
- TAIFEX tick CSV 聚合成 1m QFF close。
- Fubon/TAIFEX 同分鐘資料合併時，Fubon 覆蓋 TAIFEX。
- QFF 缺分鐘 forward-fill。
- TSM 或 USDT/TWD 缺分鐘 fail fast。
- `WarmupRunner` safety gate：`allow_live_order=true` 時不得碰任何 provider。
- `QffWarmupCheckRunner` 可單獨測 QFF leg。

### 4.2 QFF-only 實際連線測試

目的：單獨驗證 Fubon + TAIFEX 的 QFF warmup leg，不碰 Binance/BitoPro，不跑策略。

命令：

```powershell
$env:LUX_LIVE_MARKETDATA='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader qff-warmup-check --config config.live.smoke.local.toml --output-csv=
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

通過條件：

- Fubon marketdata login 成功。
- QFF candidates 成功解析，並選出符合 expiry buffer 的 active symbol。
- Fubon QFF 1m candles 非空。
- TAIFEX 官方 CSV ZIP 下載成功。
- TAIFEX QFF ticks 可聚合成 1m close。
- 合併後 rows = 1440。
- `qff_close_filled_nulls = 0`。
- 輸出 `source_rows`、`source_used_counts`、overlap mismatch summary。

### 4.3 完整 live market-data smoke

目的：驗證 Phase 2 live warmup 使用真實 Fubon、TAIFEX、Binance、BitoPro 資料源。

命令：

```powershell
$env:LUX_LIVE_MARKETDATA='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_smoke.py -q -m live_marketdata
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

通過條件：

- `live-doctor` 可取得 QFF active symbol、Binance quote、BitoPro quote。
- `tests/test_live_smoke.py` 實際通過。
- `warmup_bars = 1440`。
- `bars = 0`、`orders = 0`、`fills = 0`、`trades = 0`。
- 完整 `live-paper` startup smoke 會使用 `data/live_paper_startup_smoke.sqlite3`，從空 store 啟動、驗證 `warmup_auto`、真實 quote polling、BAR 或 skipped-minute event，以及 resume 不重建 warmup。

### 4.4 全專案 regression tests

目的：確認 Phase 2 不破壞 Phase 1 replay 與策略狀態機。

命令：

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest
```

通過條件：

- Phase 1 replay integration tests 通過。
- Strategy/store/calendar/sizing/indicator tests 通過。
- 未設定 `LUX_LIVE_MARKETDATA=1` 時，live smoke tests 應 skip。

## 5. 目前已完成目標

### Phase 1 已完成

- 建立 `lux_trader/` package 與 CLI。
- 建立核心 models、strategy state、PaperBroker、SQLiteStore。
- 支援 PoC CSV replay。
- 支援 rolling 1440 z-score，`ddof=0`。
- 支援 QFF trading calendar。
- 支援 position sizing、fees、trade summary。
- 支援 SQLite resume。
- 建立 Phase 1 unit/integration tests。

### Phase 2 已完成

- 新增 `config.live.example.toml`。
- 新增 `live-doctor`、`warmup-live`、`live-paper`。
- `live-paper` 預設自動處理 startup warmup；`--skip-warmup` 可要求必須已有 seed bars，否則 fail fast。
- 新增 `qff-warmup-check`，可單獨測 Fubon + TAIFEX QFF warmup。
- 新增 Fubon QFF marketdata adapter。
- 新增 ccxt ticker/OHLCV provider。
- 新增 TAIFEX official CSV ZIP downloader。
- 新增 QFF warmup source report。
- 新增 ExpiryBufferContractPolicy：
  - eligible active QFF = 最早到期且距最後交易日至少 5 個營業日的契約。
  - FLAT / ENTRY_PENDING 狀態遇到新 active symbol 時，切換後重建 1440 分鐘 warmup。
  - OPEN / EXIT_PENDING 狀態遇到新 active symbol 時，維持舊契約直到 exit signal。
  - 最後安全閥為舊契約最後交易日前一個營業日 13:35 force exit。
- SQLite `warmup_bars`、`bars`、`orders`、`fills`、`trades` 已補記 `qff_symbol`、`qff_expiry`、`contract_policy_state`。
- `strategy_state` 已補記 `trading_qff_symbol`、`eligible_active_qff_symbol`、`pending_symbol_switch`、`last_warmup_symbol`。
- 新增 Fubon + TAIFEX + Binance + BitoPro live smoke tests。
- 新增完整 `live-paper` startup smoke，覆蓋真實 API auto warmup、market ticks、minute finalize / skip event 與 resume。
- Project root 已可放 `.env` 與 `B121371533.pfx`，並由 `.gitignore` 保護。
- `data/taifex_cache/`、`data/qff_warmup_check_*.csv`、Fubon runtime `log/` 已忽略。

## 6. 目前已完成測試紀錄

目前紀錄的驗證結果如下：

```text
pytest tests/test_contract_policy.py tests/test_live_market_data.py -q
27 passed
```

```text
LUX_LIVE_MARKETDATA=1 pytest tests/test_live_smoke.py -q -m live_marketdata
3 passed
```

```text
pytest
37 passed, 3 skipped
```

QFF-only 實際連線測試紀錄：

```text
qff-warmup-check passed
qff_symbol=QFFG6
qff_expiry=2026-07-15
contract_policy_state=active
rows=1440
source_rows={"fubon": 294, "taifex": 3826}
source_used_counts={"forward_fill": 869, "fubon": 294, "taifex": 277}
qff_close_filled_nulls=0
```

完整 `warmup-live` CLI 測試紀錄：

```text
Warmup complete: bars_written=1440, qff_symbol=QFFG6
```

SQLite 驗證：

```text
counts={'warmup_bars': 1440, 'bars': 0, 'orders': 0, 'fills': 0, 'trades': 0}
metadata_or_value_nulls=0
symbols=[('QFFG6', '2026-07-15', 'active', 1440)]
```

完整 `live-paper` startup CLI 測試紀錄：

```text
live-doctor passed
qff_candidate_session_counts={"AFTERHOURS": 5, "REGULAR": 0}
qff_active_symbol=QFFG6
qff_active_expiry=2026-07-15

live-paper --reset-store --max-iterations 130
EVENT warmup_auto start
EVENT warmup_auto done_1440
Live-paper stopped: iterations=130, bars_processed=3, skipped_minutes=0, qff_symbol=QFFG6

live-paper --resume --max-iterations 70
WARN stale_tsm skipped_minute
Live-paper stopped: iterations=70, bars_processed=0, skipped_minutes=1, qff_symbol=QFFG6
```

SQLite 驗證：

```text
counts={'warmup_bars': 1440, 'bars': 3, 'orders': 0, 'fills': 0, 'trades': 0, 'market_ticks': 600, 'live_runs': 2}
sources=[('binanceusdm', 200), ('bitopro', 200), ('fubon_qff', 200)]
symbols=[('QFFG6', '2026-07-15', 'active', 1440)]
metadata_or_value_nulls=0
duplicate_bars=0
```

## 7. 尚未完成與後續工作

### Phase 2 後續補強

- 讓 warmup window 改成 session-aware，而不是單純連續 1440 分鐘。
- 設定 QFF forward-fill 比例 warning/fail 門檻。
- 加入更完整的 warmup quality summary。
- 接官方交易日/假日行事曆，取代第一版 weekday + configured holiday list。
- 補更多 expiry buffer resume 情境測試，例如持倉跨切約日後重啟。
- 明確整理 indicator state 的保存/重建政策。

### Phase 3 預計工作

- Commit 1：已完成 read-only broker domain skeleton，包含 snapshot/reconciliation 型別、fake broker 與 mismatch 判斷單元測試。
- Commit 2：已完成 SQLite reconciliation tables 與 `broker-doctor` / `reconcile-brokers` CLI skeleton，fake/stub 資料流可跑通。
- Commit 3：已完成 Fubon read-only adapter，查 `margin_equity`、`single_position`、today orders；真實 smoke 需 `LUX_READONLY_BROKER=1`。
- Commit 4：已完成 Binance read-only adapter，從 `.env` 讀 `BINANCE_API_KEY` / `BINANCE_SECRET`，查 balance、positions、open orders。
- Commit 5：完成 Fubon + Binance + Store reconciliation acceptance；第一版 mismatch 只 warning + record，不阻擋 `live-paper`。

Commit 1-2 skeleton 指令：

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader broker-doctor --config config.live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.example.toml --fake
```

Commit 3-4 read-only smoke 紀錄：

```text
broker-doctor: FUBON_QFF positions=0 open_orders=0 margins=5
broker-doctor: BINANCE_TSM positions=0 open_orders=0 margins=1
reconcile-brokers --fubon-readonly --fake-binance: status=matched, issues=0
reconcile-brokers --readonly: status=matched, issues=0
pytest tests/test_readonly_brokers_smoke.py -q -m readonly_broker: 2 passed
```

### Phase 4 預計工作

Phase 4 目標是建立 dry-run execution：策略產生接近真實下單的雙腿 order intent，但只驗證與記錄，不送出任何 Fubon/Binance 真實委託。`live-paper` 繼續維持 PaperBroker fill 模式；Phase 4 會新增獨立 dry-run execution 流程，避免 paper fill 與真實 order intent 混在同一條路徑。

- Commit 1：已完成 execution intent domain，包含 `PairExecutionPlan`、`ExecutionLeg`、`ExecutionCheck`、`ExecutionPlanStatus`、entry/exit 雙腿 side mapping、`OrderRequest -> ExecutionLeg/Plan` 轉換，以及 dry-run validator。
- Commit 2：已完成 SQLite recorder 與 CLI skeleton，新增 execution intent tables，並用 fake mode 跑通 intent 產生、驗證、落庫與 summary。
- Commit 3：已完成 strategy order builder refactor，把目前 `PairStrategy` 直接呼叫 `broker.place_order()` 的路徑拆出純 order request builder；PaperBroker 行為維持不變。
- Commit 4：已完成 `live-dry-run` 真實 market data 流程，重用 Phase 2 auto warmup、quote polling、bid/ask tradable spread、calendar 與 contract policy；產生 intent 後只記錄並預設進 `PAUSED`。
- Commit 5：已完成 dry-run failure simulation，覆蓋任一腿失敗、延遲、取消、partial fill；任何不完整雙腿結果都只記錄 recommended `PAUSED`，不自動補單。
- Commit 6：已完成真實 read-only + dry-run smoke，先跑 Phase 3 broker reconciliation，再跑 dry-run intent；驗收時 `orders=0`、`fills=0`、`trades=0`。

Commit 1 execution intent domain 紀錄：

```text
commit: f30d72d feat: add execution intent domain
pytest tests/test_execution_intent.py -q: 11 passed
pytest -q: 93 passed, 6 skipped
```

Commit 1 validator 覆蓋：

- 有效雙腿 intent 通過 validation。
- entry/exit、`SHORT_TSM_LONG_QFF` / `LONG_TSM_SHORT_QFF` side mapping 正確。
- missing leg、wrong side、zero quantity、QFF 非整數口數、wrong QFF symbol 會 rejected。
- `allow_live_order=true` 會 rejected，Phase 4 仍不得啟用真實送單。

Commit 2 SQLite recorder + CLI skeleton 紀錄：

```text
新增 tables: execution_plans, execution_legs, execution_checks
新增 CLI: dry-run-doctor, live-dry-run --fake, execution-summary
pytest tests/test_execution_intent.py tests/test_execution_recorder_cli.py -q: 18 passed
pytest -q: 100 passed, 6 skipped
```

Commit 2 驗收：

- fake `live-dry-run` 可產生 valid execution intent，validation 通過後以 `recorded` 狀態寫入 SQLite。
- rejected fake case 會寫入 execution checks 並以 nonzero exit code 結束。
- dry-run recorder 不寫入 `orders`、`fills`、`trades`。
- `allow_live_order=true` 會被 `live-dry-run` 拒絕。

Commit 3 strategy order builder refactor 紀錄：

```text
commit: f2c9512 feat: refactor strategy order builders
pytest tests/test_strategy_store.py tests/test_replay_integration.py -q: 7 passed
pytest -q: 102 passed, 6 skipped
```

Commit 3 驗收：

- `PairStrategy` 可單獨 build entry/exit 雙腿 `OrderRequest`，不必立即呼叫 broker。
- TSM symbol 從 hardcode 拆成 strategy 建構參數，預設仍是 `TSM/USDT:USDT`。
- replay / PaperBroker path 仍使用同一組 builder 後 submit，既有回測結果不變。

Commit 4 live-dry-run real market data 紀錄：

```text
新增 LiveDryRunRunner
新增 CLI real mode: live-dry-run --config ... --reset-store --max-iterations ...
pytest tests/test_live_market_data.py -q: 41 passed
pytest -q: 103 passed, 6 skipped
```

Commit 4 驗收：

- `live-dry-run` 不加 `--fake` 時會走真實 market data runner，沿用 startup auto warmup、quote polling、minute finalize 與 bid/ask tradable spread decision。
- `ENTRY_PENDING` / `EXIT_PENDING` 不再呼叫 PaperBroker fill，而是產生 `PairExecutionPlan` 並寫入 execution tables。
- dry-run intent 產生後策略狀態預設進 `PAUSED`，避免重複產生 intent。
- `orders`、`fills`、`trades` 保持 0；完整資料寫入 `execution_plans`、`execution_legs`、`execution_checks` 與 `events`。

Commit 5 failure simulation 紀錄：

```text
新增 ExecutionSimulationScenario: leg_failure, delay, cancel, partial_fill
新增 table: execution_simulations
新增 CLI: simulate-execution --scenario ... [--fake-plan]
pytest tests/test_execution_recorder_cli.py tests/test_execution_intent.py -q: 22 passed
pytest -q: 107 passed, 6 skipped
```

Commit 5 驗收：

- simulator 可針對 recorded `PairExecutionPlan` 模擬任一腿失敗、延遲、取消與 partial fill。
- simulation 只寫入 `execution_simulations` / `events`，不寫入 `orders`、`fills`、`trades`。
- `simulate-execution --fake-plan` 可建立 deterministic plan 後直接模擬。
- 不使用 `--fake-plan` 時，CLI 會讀取 store 最新 execution plan 進行模擬。
- 所有 failure simulation payload 都帶 `recommended_state=paused`，後續 execution gate 可據此阻擋自動補單。

Commit 6 real read-only + dry-run smoke 紀錄：

```text
新增 test: tests/test_dry_run_smoke.py
pytest tests/test_dry_run_smoke.py -q: 1 skipped without env gates
pytest -q: 107 passed, 7 skipped
LUX_LIVE_MARKETDATA=1 + LUX_READONLY_BROKER=1 pytest tests/test_dry_run_smoke.py -q -m "live_marketdata and readonly_broker and dry_run_smoke": 1 passed
```

Commit 6 驗收：

- smoke 需要同時設定 `LUX_LIVE_MARKETDATA=1` 與 `LUX_READONLY_BROKER=1`，預設測試環境不會碰真實 API。
- 測試先用 Fubon / Binance read-only broker 做 reconciliation，必須 `matched` 才繼續。
- 測試寫入 `ENTRY_PENDING` seed state，讓真實 market data 跨過第一根 finalized minute 後產生 dry-run entry intent，不依賴市場剛好出現 entry signal。
- `LiveDryRunRunner` 實際完成 auto warmup、market ticks、minute finalize、execution intent record。
- SQLite 驗證 `broker_reconciliation_runs=1`、`execution_plans>=1`、`execution_legs>=2`，且 `orders=0`、`fills=0`、`trades=0`。

live-dry-run 全面測試補強：

```text
新增 deterministic tests:
- live-dry-run resume 後不重複 warmup / bar / execution plan
- EXIT_PENDING seed state 產生 exit execution intent
- expiry buffer force-exit 產生 rollover exit execution intent

擴充 real smoke:
- 使用 data/live_dry_run_full_smoke.sqlite3
- full smoke 完成後同一 store 再跑 resume 70 iterations
- 驗證 warmup_bars 維持 1440、live_runs=2、bars timestamp 無重複、execution plan 無重複
```

PowerShell 互動式啟動補強：

```text
新增 scripts/lux.ps1，固定使用 Quant 環境並透過 conda run --no-capture-output 啟動 lux_trader，避免 live terminal UI 被 conda run capture。
```

全面測試指令：

```powershell
Set-Location 'D:\Users\Work place\Project Lux'
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q

$env:LUX_LIVE_MARKETDATA='1'
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader dry-run-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.smoke.local.toml --readonly
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_dry_run_smoke.py -q -m "live_marketdata and readonly_broker and dry_run_smoke"
Remove-Item Env:\LUX_LIVE_MARKETDATA
Remove-Item Env:\LUX_READONLY_BROKER
```

短時間 soak 手動指令：

```powershell
$env:LUX_LIVE_MARKETDATA='1'
.\scripts\lux.ps1 live-dry-run --config config.live.smoke.local.toml --reset-store --max-iterations 900 --no-color
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

Trading calendar closed_dates 補強：

```text
新增 config: [trading_calendar] closed_dates = []
本機 smoke config: closed_dates = ['2026-06-19']
新增 live_session_status(timestamp, closed_dates)
live-paper/live-dry-run 在 non-trading session 不 fetch quote、不 finalize BAR、不跑策略
Terminal UI 顯示: LIVE non-trading session next=MM/DD HH:MM in=HH:MM:SS
live-doctor 顯示 live_session、next_trading_start、qff_book_timestamp、qff_book_age_sec、qff_book_stale
```

驗收紀錄：

```text
pytest -q: 119 passed, 7 skipped
live-doctor: live_session=closed, next_trading_start=2026-06-22T08:45:00+08:00
real market data doctor: qff_book_timestamp=2026-06-19T04:59:59.032000+08:00, qff_book_stale=true
live-dry-run --reset-store --max-iterations 30: bars_processed=0, plans_recorded=0
SQLite: warmup_bars=1440, market_ticks=0, bars=0, execution_plans=0, live_runs=1
```

### Phase 5 預計工作

Phase 5 目標是把 Phase 4 已驗證的 `live-dry-run` intent 流程接成真實送單。第一版採自動 `live-execute` loop：策略在 live market data 中產生 entry/exit intent 後，通過 safety gate 才直接送出真實雙腿委託。第一版 order policy 採市價優先，重點是先驗證最小實單閉環，不追求最佳成交價。

- Commit 1：Live execution domain + safety gate。新增 live execution result/status/check models 與 gate validator；檢查 config、env、reconciliation、open orders、unexpected positions、plan freshness 與 duplicated execution；先只用 fake broker 測試，不碰真實 API。
- Commit 2：SQLite recorder + CLI skeleton。新增 live execution audit tables，包含 execution run、per-leg submitted order、per-leg status/fill result、safety gate checks、post-trade reconciliation；新增 `live-order-doctor`、`live-execute --fake`。
- Commit 3：Fubon live execution adapter。接 `FutOptOrder` + `sdk.futopt.place_order(...)` 送 QFF market order；必須同時通過 config `allow_live_order=true`、`[live_execution] enabled=true`、`FUBON_ALLOW_LIVE_ORDER=1` 與全域 live order gate。
- Commit 4：Binance live execution adapter。從 `.env` 讀 `BINANCE_API_KEY` / `BINANCE_SECRET`，用 ccxt USDM private API 送 `TSM/USDT:USDT` market order，並查 open orders / positions 驗證。
- Commit 5：Live execution coordinator。雙腿順序第一版固定為 `QFF first, Binance second`；QFF 失敗時不送 Binance，QFF 成功但 Binance 失敗時記錄裸露風險並進 `PAUSED`；兩腿都確認成交後才更新 strategy state 為 `OPEN` 或 `FLAT`。
- Commit 6：`live-execute` real market data loop。沿用 Phase 4 `live-dry-run` 的 auto warmup、quote polling、minute finalize、bid/ask tradable spread decision 與 contract policy，但將 recorded intent 交給 live execution engine。
- Commit 7：real smoke / manual acceptance。預設 pytest 只跑 fake execution；真實送單 smoke 必須明確設定 `LUX_LIVE_MARKETDATA=1`、`LUX_READONLY_BROKER=1`、`PROJECT_LUX_ALLOW_LIVE_ORDER=1`、`FUBON_ALLOW_LIVE_ORDER=1`、`BINANCE_ALLOW_LIVE_ORDER=1`，且單獨執行。

Phase 5 extension point 紀錄：

- Commit A：已完成 `LiveRuntime` + `LiveModeHandler`，讓 `live-paper`、`live-dry-run`、未來 `live-execute` 共用同一條 live market data loop。
- Commit B：已完成 `live-dry-run` 改用 shared runtime，dry-run 專屬邏輯集中在 `DryRunLiveModeHandler`。
- Commit C：已完成 `ExecutionStore` 與 CLI helpers cleanup，execution tables 操作和 fake/read-only helper 已從大型 `SQLiteStore` / `cli.py` 拆出。
- Commit D：已完成 Phase 5 extension point，新增 `[live_execution]` config、`live-order-doctor`、保留的 `live-execute` CLI 與 `LiveExecuteModeHandler`；目前沒有 live broker adapter，`live-execute` 會 fail fast，不會初始化 provider 或送單。

Phase 5 驗收重點：

- 缺任一 config/env safety gate 時拒絕送單。
- broker reconciliation mismatch 時拒絕送單。
- QFF first 成功、Binance second 成功時，execution audit tables 與 strategy state 更新正確。
- QFF 失敗時 Binance adapter 不得被呼叫。
- Binance 失敗、partial fill、unknown order status 時一律進 `PAUSED`，不自動補單、不自動重試。
- `live-paper`、`live-dry-run` 保留，分別作為 paper trading 與實單前預演工具。

## 8. Safety 原則

- Phase 2 到 Phase 4 都不得送真實委託。
- Phase 5 預設仍不得送真實委託；只有 explicit config + env gate 全部通過時才允許最小實單。
- 以實際全流程跑通為驗收基準
- 任何 live test 必須明確設定 `LUX_LIVE_MARKETDATA=1`。
- `allow_live_order=true` 在 Phase 1 到 Phase 4 必須被拒絕；Phase 5 只能由 `live-execute` safety gate 接受。
- Commit D 階段的 `live-execute` 只是保留入口，必須 fail fast；真實送單 adapter 完成前不得初始化 execution broker。
- 任一腿失敗、partial fill、unknown status、post-trade reconciliation mismatch，都必須進 `PAUSED`，不得自動補單或重試。
- `.env`、`.pfx`、local smoke config、SQLite、TAIFEX cache、runtime logs 都不得進 git。
