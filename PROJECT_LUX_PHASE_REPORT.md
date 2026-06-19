# Project Lux Phase Plan and Test Report

更新日期：2026-06-18

## 1. 專案總覽

Project Lux 是 QFF/TSM 配對交易系統的最小可運行架構。核心程式位於 `lux_trader/`，測試位於 `tests/`。

專案背景是：主要交易邏輯已先在 `D:\Users\Documents\Proof of Concept` 做過想法驗證。PoC 是第一版策略行為的基準來源，包含交易標的、spread/z-score 計算、進出場門檻、部位 sizing、費用、QFF 交易時段假設，以及 replay/backtest 的 reference summary。Project Lux 的任務不是重新發明策略，而是把 PoC 驗證過的行為整理成可部署、可測試、可恢復、可逐步接 live market data 的系統架構。

因此 Phase 1 的核心驗收標準是「Project Lux replay 結果要和 PoC reference 對齊」，包含交易次數、方向、部位、PnL 與費用。之後任何策略規則變更，都應該明確記錄，並重新和 PoC 或新的 reference dataset 驗證。

目前系統已從 Phase 1 的 PoC CSV replay 擴展到 Phase 2 的 live market data + PaperBroker。Phase 2 仍然不允許任何真實下單，只驗證即時/日內行情、warmup、策略資料流與 SQLite recovery 基礎。

## 2. Phase 1 到 Phase 4 目標

| Phase | 目標 | 主要內容 | 下單狀態 |
| --- | --- | --- | --- |
| Phase 1 | PoC CSV replay MVP | 讀取 PoC CSV、重算 rolling z-score、跑 PairStrategy、PaperBroker、SQLite store、resume、summary | 不接 API，不下單 |
| Phase 2 | Live market data + PaperBroker | 接 Fubon marketdata、TAIFEX downloader、Binance/BitoPro ccxt，建立 live warmup、expiry buffer QFF 選約與 1m bar polling | 只做 paper order |
| Phase 3 | Read-only broker reconciliation | Fubon/Binance read-only broker，登入、查部位、查委託、查保證金，啟動時做 broker/store 對帳 | 不送單 |
| Phase 4 | Dry-run execution | 策略產生真實 order intent，但只記錄不送出；驗證雙腿 order intent、風控、失敗處理 | 不送單，只記錄 intent |

Phase 5 之後才考慮最小實單，且必須有環境變數、config safety gate、broker/store 對帳與任一腿失敗進 `PAUSED` 的安全機制。

## 3. 目前階段：Phase 2

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

- Commit 1：建立 read-only broker domain skeleton，包含 snapshot/reconciliation 型別、fake broker 與 mismatch 判斷單元測試。
- Commit 2：新增 SQLite reconciliation tables 與 `broker-doctor` / `reconcile-brokers` CLI skeleton，先用 fake/stub 跑通資料流。
- Commit 3：接 Fubon read-only adapter，查 `margin_equity`、`single_position`、today orders；真實 smoke 需 `LUX_READONLY_BROKER=1`。
- Commit 4：接 Binance read-only adapter，從 `.env` 讀 `BINANCE_API_KEY` / `BINANCE_SECRET`，查 balance、positions、open orders。
- Commit 5：完成 Fubon + Binance + Store reconciliation acceptance；第一版 mismatch 只 warning + record，不阻擋 `live-paper`。

Commit 1-2 skeleton 指令：

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader broker-doctor --config config.live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.example.toml --fake
```

### Phase 4 預計工作

- 讓策略產生真實 order intent。
- 建立 dry-run execution recorder。
- 驗證雙腿 intent 一致性、數量、方向、價格。
- 模擬任一腿失敗、延遲、取消、partial fill 的狀態處理。

## 8. Safety 原則

- Phase 2 到 Phase 4 都不得送真實委託。
- 以實際全流程跑通為驗收基準
- 任何 live test 必須明確設定 `LUX_LIVE_MARKETDATA=1`。
- `allow_live_order=true` 在目前階段必須被拒絕。
- `.env`、`.pfx`、local smoke config、SQLite、TAIFEX cache、runtime logs 都不得進 git。
