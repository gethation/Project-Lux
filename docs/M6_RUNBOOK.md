# M6 — 最小化雙腿真實下單驗收 Runbook

這是唯一會送出**真實資金訂單**的步驟。必須**有人看管**執行，使用最小 smoke 部位
（Fubon TMF `TMFG6` 1 口 + Binance `TSM/USDT:USDT` 0.1 unit），並只在 TMF 正常可進場
時段執行（日盤 08:45-13:45，或一般夜盤 15:00-05:00；避開週五夜盤、close-only、接近休市
與流動性不穩時段）。

流程由 `tests/smoke/test_live_execute_smoke.py` 驅動：直接測 execution channel
（`RealExecutionCoordinator`，`qff_first=true` 先 Fubon 再 Binance），不跑 QFF/TMF
warmup 或策略訊號。完成且僅完成一次往返：真實雙腿**進場** → 真實雙腿**出場** →
驗證帳戶回到空倉。所有 gate 未設定時該測試會**跳過**。

## 1. 前置條件

- 完整 deterministic 測試綠燈：`conda run -n Quant pytest -q`。
- `.env`（Fubon + `BINANCE_API_KEY`/`BINANCE_SECRET`）與 Fubon `.pfx` 位於專案根目錄。
- **兩邊券商帳戶目前空倉**（無 TMF/TSM 部位、無未成交委託）。測試會檢查，但請先自行確認：
  ```powershell
  $env:LUX_READONLY_BROKER='1'
  .\scripts\lux.ps1 broker-status --config configs/config.live.exec.smoke.local.toml
  ```
- 設定檔 `configs/config.live.exec.smoke.local.toml` 存在（gitignored），包含：
  - `[safety] allow_live_order = true`
  - `[live_execution] enabled = true`、`qff_first = true`
  - `[live_execution_smoke] enabled = true`、`fubon_symbol = 'TMFG6'`、`fubon_lots = 1`、
    `binance_symbol = 'TSM/USDT:USDT'`、`tsm_units = 0.1`、`qff_expiry = '<當月>'`
  - **執行前確認 TMFG6 是富邦軟體裡要測的 2026 年 7 月微型台指期合約。**

## 2. 執行前檢查（不送真實訂單）

```powershell
# a) 正式 QFF/TSM dry-run 可運作（simulated adapter，不送單）。
.\scripts\lux.ps1 live-dry-run --config configs/config.live.exec.dryrun.local.toml --reset-store --max-iterations 130

# b) 真實券商唯讀 reconciliation：必須 status=matched。
$env:LUX_READONLY_BROKER='1'
.\scripts\lux.ps1 reconcile-brokers --config configs/config.live.exec.dryrun.local.toml --readonly

# c) 檢視 live-order gate 狀態（此時 env gate 未全開是預期 FAIL）。
.\scripts\lux.ps1 doctor --config configs/config.live.exec.smoke.local.toml --mode order
```

`reconcile-brokers` 非 matched 時：停止，修正或手動平倉後再繼續。

## 3. 執行驗收（會送出真實訂單，需有人看管）

```powershell
$env:LUX_READONLY_BROKER='1'
$env:PROJECT_LUX_ALLOW_LIVE_ORDER='1'
$env:FUBON_ALLOW_LIVE_ORDER='1'
$env:BINANCE_ALLOW_LIVE_ORDER='1'

& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant `
  pytest tests/smoke/test_live_execute_smoke.py -q -s `
  -m "readonly_broker and live_execute_smoke"

Remove-Item Env:\LUX_READONLY_BROKER, `
  Env:\PROJECT_LUX_ALLOW_LIVE_ORDER, Env:\FUBON_ALLOW_LIVE_ORDER, Env:\BINANCE_ALLOW_LIVE_ORDER
```

寫入 `data\live_execute_smoke.sqlite3`（依 smoke config `store_path`；每次執行重設）。

## 4. 通過標準（測試自動檢查）

- 進場 outcome `filled`，post-entry read-only reconciliation `matched`；
  orders/fills 存在且**無 `DRYRUN-*` id**。
- 出場 outcome `filled`，post-exit reconciliation `matched`，帳戶回 flat。
- 進出場皆輸出 `leg_timing_gap`，且 `execution_outcomes.payload_json` 保存
  `primary_leg_timing_gap`（Fubon/Binance submit start/handoff 秒差）。
- 兩腿數量正確：TMFG6 1 口、TSM 0.1 unit。
- **最終兩券商 0 部位、0 未成交委託。**

## 5. 失敗復原（可能留下真實部位）

**不要**直接重跑。先唯讀確認兩邊帳戶，再手動平掉遺留 leg：

```powershell
# 最後 outcome 與 reconciliation（M6 direct smoke 不一定有策略 state）。
.\scripts\lux.ps1 live-status --config configs/config.live.exec.smoke.local.toml

# 唯讀查 Fubon 部位/委託/order records：
$env:LUX_READONLY_BROKER='1'
.\scripts\lux.ps1 broker-status --config configs/config.live.exec.smoke.local.toml --orders TMFG6

# 手動平倉（需 manual-close env gates：LUX_FUBON_MANUAL_CLOSE=1 / LUX_BINANCE_MANUAL_CLOSE=1
# 加上對應 *_ALLOW_LIVE_ORDER）：
#   Fubon（TMF 口數）：
.\scripts\lux.ps1 manual-close --config <cfg> --venue fubon --symbol TMFG6 --side sell --lot 1 --confirm-symbol TMFG6
#   Binance（TSM units）：
.\scripts\lux.ps1 manual-close --config <cfg> --venue binance --symbol TSM/USDT:USDT --side buy --quantity 0.1 --confirm-symbol TSM/USDT:USDT

# 若正式 live store 進入 PAUSED：確認兩腿皆平後才清除。
.\scripts\lux.ps1 clear-pause --config <cfg> --readonly
```

## 6. 成功後

把驗收紀錄（訂單 id、成交價、PnL、最終空倉、timing gap）寫入專案報告。M6 完成後
才可進入 M7（無人看管 soak、轉倉/週末 force-exit 情境、go-live runbook）；在那之前
**不得無人值守實單**。
