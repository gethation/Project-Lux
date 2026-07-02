# M6 — 最小化即時執行驗收（真實雙腿訂單）

這是第一個、也是唯一一個會送出**真實資金訂單**的步驟。請在有人看管的情況下執行，使用**最小部位（約 1 口 QFF）**，並且只在 QFF 正常可進場交易時間內執行（日盤 08:45-13:45，或一般夜盤 17:25-05:00；不要在週五夜盤、close-only、或接近休市的時段執行）。

此驗收會強制完成且僅完成一次往返交易：先送出一筆真實雙腿**進場**（QFF + Binance TSM），再送出一筆真實雙腿**出場**，並驗證帳戶最後回到空倉。流程由 `tests/smoke/test_live_execute_smoke.py` 驅動；除非下方所有 gate 都已設定，否則該測試會被**跳過**。

## 1. 前置條件

- 單腿 smoke 測試已經通過（Binance TSM、Fubon TMF）。✅（已完成）
- 完整 deterministic 測試套件為綠燈：`conda run -n Quant pytest -q`。
- `.env`（Fubon + `BINANCE_API_KEY`/`BINANCE_SECRET`）以及 Fubon `.pfx` 位於專案根目錄。
- **券商帳戶目前為空倉**（沒有 QFF/TSM 部位，也沒有未成交委託）。測試會在交易前檢查這點，但請先自行確認。
- 設定檔 `configs/config.live.exec.smoke.local.toml` 存在（已被 gitignore）。預設包含 `leg_notional_twd = 240000`（約等於 QFF≈2400 時的 1 口 QFF）與 `allow_live_order = true`。**執行前請先依你的帳戶檢查部位大小。**

## 2. 執行前檢查（不送出真實訂單）

先預覽實際部位大小，並確認所有 gate，不送出任何訂單。

```powershell
# a) 查看此設定會產生的部位大小（模擬成交，不送出真實訂單）。
#    將 exec 設定複製成 *.dryrun.local.toml，並設定 allow_live_order=false，
#    然後執行：
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-dry-run --config configs/config.live.exec.dryrun.local.toml --reset-store --max-iterations 130
#    確認 OPEN 這一行顯示 qff_contracts=1（或你預期的口數），且
#    TSM units 數量是你願意實際交易的大小。

# b) 確認所有 live-order gate 都是 OPEN（這不會送出訂單）。
$env:PROJECT_LUX_ALLOW_LIVE_ORDER='1'; $env:FUBON_ALLOW_LIVE_ORDER='1'; $env:BINANCE_ALLOW_LIVE_ORDER='1'
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-order-doctor --config configs/config.live.exec.smoke.local.toml
```

`live-order-doctor` 必須回報所有 gate 檢查皆為 PASS。若有任何 FAIL，請停止並修正。

## 3. 執行驗收（會送出真實訂單）

請在有人看管、QFF 可進場交易時間內（日盤或一般夜盤），並設定全部五個 gate 後執行：

```powershell
$env:LUX_LIVE_MARKETDATA='1'
$env:LUX_READONLY_BROKER='1'
$env:PROJECT_LUX_ALLOW_LIVE_ORDER='1'
$env:FUBON_ALLOW_LIVE_ORDER='1'
$env:BINANCE_ALLOW_LIVE_ORDER='1'

& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant `
  pytest tests/smoke/test_live_execute_smoke.py -q -s `
  -m "live_marketdata and readonly_broker and live_execute_smoke"

Remove-Item Env:\LUX_LIVE_MARKETDATA, Env:\LUX_READONLY_BROKER, `
  Env:\PROJECT_LUX_ALLOW_LIVE_ORDER, Env:\FUBON_ALLOW_LIVE_ORDER, Env:\BINANCE_ALLOW_LIVE_ORDER
```

它會寫入 `data\live_execute_smoke.sqlite3`（每次執行都會重設）。

## 4. 通過標準

測試會檢查以下所有項目；若執行結果為綠燈，代表皆已滿足：

- 進場：`live_execution filled` + `post_trade_reconciliation matched`；策略狀態為 `open`；存在 `orders`/`fills`，且**沒有 `DRYRUN-*` id**（代表是真實訂單）；最新 execution outcome 為 `filled`。
- 出場：`live_execution filled` + `post_trade_reconciliation matched`；策略狀態為 `flat`；`trades` 表格中剛好有 1 筆資料列。
- **最終券商檢查：兩個券商都回報 0 部位與 0 未成交委託。**

## 5. 如果因未平倉部位而停止（復原）

失敗或部分成交的 leg 會暫停策略（`PAUSED`），並可能留下真實部位。請**不要**重新執行驗收。請使用 M2 工具復原：

```powershell
# 檢查狀態、部位、最後 outcome、reconciliation。
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-status --config configs/config.live.exec.smoke.local.toml

# 手動平掉每個遺留的 leg（需要 manual-close env gates）。
#   Fubon（期貨口數）：
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  fubon-manual-close --config ... --symbol <QFFxx> --side sell --lot 1 --confirm-symbol <QFFxx>
#   Binance（TSM units）：
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  binance-manual-close --config ... --symbol TSM/USDT:USDT --side buy --quantity <units> --confirm-symbol TSM/USDT:USDT

# 確認兩個 leg 都已平倉後，清除暫停狀態（會重新執行 reconciliation）。
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  clear-pause --config ... --readonly
```

## 6. 成功後

將此次驗收記錄（訂單 id、成交價格、PnL、最終空倉）寫入 `PROJECT_LUX_PHASE_REPORT.md`。接著 M6 即完成；可繼續進入 **M7**（跨完整交易時段的無人看管 soak，包含合約轉倉，以及 go-live runbook）。
