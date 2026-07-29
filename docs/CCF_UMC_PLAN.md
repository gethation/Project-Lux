# CCF/UMC 單一配對改造計畫

**分支**：`ccf-umc`，基底 `bf06371`（= `origin/stable-version`，QFF/TSM 實盤穩定版）
**日期**：2026-07-29

---

## 0. 已定案的決策

| # | 決策 | 理由 |
|---|---|---|
| 1 | **本專案只跑 CCF/UMC 一個配對**。解耦在行程／分支層級，不在程式碼泛型層級 | master 的 `list[PairContext]` 泛化讓兩個 pair 互相牽動；一分支一配對從結構上消除這件事 |
| 2 | **CCF/UMC 上線後退役 QFF/TSM**，過渡期兩者輪流跑（一次只開一台） | 富邦一帳號限一 SDK session，兩套同時跑必然互踢。退役是唯一不需要額外程式碼的解法 |
| 3 | **具名 `ccf_*` / `umc_*`**，不用 `tw_leg` / `us_leg` | 單一用途專案，config 與 log 要一眼看懂在講哪一腿 |
| 4 | **移植 master 的 pair-agnostic 模組，但逐檔審過再收** | 那些檔案 grep `PairContext\|pair_id\|pair_key` 命中 0 次，可省下約 3,000 行已測過的程式碼；審核是為了不盲收 master 的既有假設 |
| 5 | **live 保留 directional bid/ask z-score + 同根 bar 成交** | 執行現實性優先。代價見 §7 |
| 6 | **CLI 14 → 約 6**，並合併重複的執行／對帳路徑 | 「精簡以適應接下來的建構」的主體 |
| 7 | **接受 CCF 目前 7 週樣本**，不接 TAIFEX 歷史檔庫 | 樣本會隨時間自然成長；實單前重新評估 |

---

## 1. 目標系統

一個只跑 CCF/UMC 的行程。三個資料源、兩個下單通道：

| 角色 | 標的 | 通道 | 說明 |
|---|---|---|---|
| 台股腿 | `TAIFEX:CCF` 聯電股票期貨 | **富邦 API** | 2,000 股/口 |
| 美股腿 | `NYSE:UMC` 聯電 ADR | **IBKR API** | 現股，整股交易，ADR 5:1 |
| 匯率 | `USD/TWD` | Twelve Data | 5m 快取輪詢，非共時腿 |

**策略參數（凍結）**：1m / `zscore_window = 2500` / `entry_z = 1.5` / `exit_z = 0.0` /
週末規則 `none` / 固定 **1 口**

**Spread**：

```text
umc_twd_fair = umc_close * usdtwd_close / 5
spread = (umc_twd_fair - ccf_close) / (umc_twd_fair + ccf_close) * 200
z = (spread - rolling_mean(spread, 2500)) / rolling_std(spread, 2500, ddof=0)
```

**交易時段** = TAIFEX 夜盤 ∩ NYSE RTH
= 台北 `21:30–04:00`（美國夏令）／`22:30–05:00`（冬令）。
TAIFEX 日盤 `08:45–13:45` 完全落在 NYSE 之外，**本系統在日盤不交易**。

---

## 2. Phase A — 剝除與更名

純刪除與機械更名，**不加任何功能**。每一步都有可驗證的不變量。

### A1 — 更名（tripwire：既有 QFF/TSM replay golden 必須逐字不變）

`qff_*` → `ccf_*`、`tsm_*` → `umc_*`，涵蓋 config key、store 欄位、CLI 旗標、
事件字串、測試。`BrokerName`：`FUBON_QFF` → `FUBON_CCF`、`BINANCE_TSM` → `IBKR_UMC`。

> **驗收**：`replay` 對既有 QFF/TSM fixture 產出的 summary **逐字不變**。
> 這證明更名是純機械的。這一步不動任何數值路徑。

### A2 — 刪除美股腿的舊實作

刪 `integrations/binance/`（4 檔）、`integrations/bitopro/`（2 檔）、
`integrations/ccxt_market_data.py` 與對應測試；清掉 config 裡的
`binance_execution`、`bitopro_symbol`、margin 的 Binance 比例。

> **驗收**：`pytest -q` 全綠；replay golden 仍逐字不變（replay 走 CSV，不碰交易所）。

### A3 — 週末規則變成開關（**先不刪**）

引入 `weekend_policy = flat | no-entry | none`，本專案預設 `none`。
`flat` 分支暫時保留，讓 QFF/TSM 的 golden tripwire 活到 Phase C 結束。

> **為什麼不直接刪**：週末規則的移除會改變回測數值。在 CCF/UMC 的 golden
> 立起來之前就刪掉唯一的參照基準，等於整個 Phase A/B 沒有防線。

### A4 — 部位與費用

- Sizing 預設 `fixed_lots`, `lots = 1`；`notional` 模式保留但需明確指定
- `ccf_contract_multiplier = 2000.0` — **這是全案風險最高的常數**（QFF 是 100，
  差 20 倍）。必須有專門測試釘死，且 config 缺值時要硬失敗而非取預設
- `ccf_fee_per_contract_twd`（**目前用 QFF 的 88 佔位，待確認**）、
  `ccf_tax_rate = 2e-5`、`umc_fee_bps = 2.5`（Phase D 換成真實費用模型）

### A5 — CLI 14 → 6

現行：`replay` `summary` `doctor` `live-dry-run` `live-status` `reconcile-brokers`
`clear-pause` `recover-manual-flat` `warmup-live` `margin-check` `live-execute`
`exec-smoke` `manual-close` `broker-status`

整併為：

| 命令 | 涵蓋 |
|---|---|
| `replay` | replay + summary |
| `live --mode dry-run\|execute` | live-dry-run + live-execute |
| `status <doctor\|live\|broker\|reconcile\|margin\|warmup>` | 6 個唯讀查詢 |
| `admin <clear-pause\|manual-close\|manual-flat\|exec-smoke>` | 4 個危險操作，集中一處好上鎖 |

同時合併重複的執行與對帳路徑（`commands_execution.py` / `commands_recovery.py`
目前有兩條平行實作）。

### A6 — Store schema

欄位更名 + 週末欄位最終會移除 → **schema 破壞性變更，舊 store 直接封存不遷移**。
CCF/UMC 是全新部署，沒有需要保留的歷史。

---

## 3. Phase B — 補上 IBKR 行情與 FX

### B1 — 逐檔移植（審核後才收）

| 檔案 | 行數 | 審核重點 |
|---|---:|---|
| `integrations/subprocess_transport.py` | 164 | 無 |
| `integrations/ibkr/client_process.py` | 623 | **`reqMarketDataType(3)` 寫死** — 要改成可設定、預設 1(live)，讓沒授權時大聲失敗而非靜默降級 |
| `integrations/ibkr/market_data.py` | 214 | 延遲行情下 bid/ask 為 NaN 的處理路徑 |
| `integrations/ibkr/historical.py` | 183 | warmup 回溯深度是否夠 2500 根 |
| `integrations/ibkr/readonly.py` | 130 | 現金帳戶 vs margin 帳戶的欄位差異 |
| `integrations/ibkr/diagnostic.py` | 218 | 無 |
| `integrations/ibkr/calendar.py` | 43 | 無 |
| `integrations/twelvedata/market_data.py` | 214 | 免費層 800/日額度、快取必要性 |
| `market_data/cached_quote.py` | 108 | 無 |
| `LiveQuote.market_data_tier` / `.is_delayed` | — | 無 |
| 對應測試 | ~1,500 | 移植時一併收 |

`stale_seconds = 1200`（延遲行情容忍）**先不要移植成預設值** —— 它是為了讓
延遲資料勉強過暖機而設的權宜，即時行情到位後必須拿掉。改成明確的
`umc.stale_seconds` 且預設走全域 10 秒，需要時才在 config 明寫。

### B2 — 交集時段

移植 master `runtime/live/pair_session.py` 的邏輯，拿掉 `us_leg.venue` 分支
（單一配對恆為 RTH-only）。要補的：

- **美國假日**：master 自承未模型化（假日時 UMC 無資料 → staleness 擋掉 → 不建 bar，
  「大聲但安全」）。沿用此策略，但**加明確 log**，不要讓它看起來像資料異常
- **DST 切換日**必須有測試 —— 時段在台北時間整體位移一小時
- 冬令時 RTH 尾端（台北 05:00）**正好撞上 TAIFEX 夜盤收盤**，邊界要有測試

### B3 — Spread 與 warmup

- ADR 5:1 進到 spread 與部位換算兩處
- `warmup_minutes = 2500`（≈6.4 個 UMC session ≈ 9 個日曆天）
- CCF 暖機：TAIFEX 網路（30 天窗）+ `ccf1_1m_cumulative.csv` 後援
- UMC 暖機：IBKR `reqHistoricalData`
- FX 暖機：Twelve Data

### B4 — clock skew 來源

`bootstrap.py:fetch_binance_usdm_market_time` 隨 Binance 一起刪。改用 IBKR
server time 或 NTP。`clock_skew_fail_seconds` 閘門保留。

### B5 — 換月強制平倉時點（**設計缺陷修正**）

現行 `contract_policy.force_exit_time = '13:35'` 落在 TAIFEX 日盤。CCF/UMC 在
日盤不交易 —— 照搬會平掉 CCF、**留下 UMC 裸腿過夜**。

改成：換月前最後一個**交集窗**的尾端強制平倉。這在 QFF/TSM 上不存在
（Binance 24/7，任何時點平 QFF 都能同時平 TSM），是 CCF/UMC 特有的。

> **驗收**：dry-run 連續跑完一個完整 UMC session，warmup 過關，
> 每根 bar 三腿都有報價，時段進出正確。

---

## 4. Phase C — replay 基準線

### C1 — 凍結 fixture

來源 PoC `D:\Users\Documents\Proof of Concept\data\processed\`：
`wk_none_0728_zscore.csv`、`ccf1_1m_cumulative.csv`、
`umc_1m_cumulative_tvext.csv`、`wk_none_0728_spread_fx.csv`

**複製進 repo**，不要指向 PoC 路徑 —— PoC 的 pipeline 會覆寫那些檔案。

### C2 — golden 數字

| 項目 | 值 |
|---|---:|
| 範圍 | 2026-06-09 21:30 → 2026-07-28 03:59 |
| bars | 12,460 |
| 交易 | 18 |
| net_pnl_twd | 235,726.66 |
| max_drawdown_twd | −25,585.96 |

留**兩個** golden：notional 1M（對齊 PoC）與 fixed 1 口（對齊實盤尺度）。

### C3 — 清理

CCF/UMC golden 綠燈後，刪掉 QFF/TSM fixture 與 `weekend_policy = flat` 分支。
至此週末規則整條消失。

### C4 — 限制必須寫進測試檔開頭

> 這個 golden 驗證的是**策略數學**，不是 live 行為。live 用 directional
> bid/ask z-score + 同根 bar 成交，replay 用 mid/close z + 下一根 bar 成交。
> golden 全綠**不代表** live 會做出同樣的交易。

---

## 5. Phase D — IBKR 下單 adapter

**全新程式碼，本案最大一塊。** master 的 `integrations/ibkr/` 底下沒有
`execution.py`，grep `place_order|placeOrder|submit` 命中 0 —— 目前 UMC
只能讀行情與部位，完全不能下單。

| # | 項目 | 說明 |
|---|---|---|
| D1 | `integrations/ibkr/execution.py` | 實作既有的 `ExecutionAdapter` protocol（`execution/outcome.py:47`） |
| D2 | 成交確認分層 | 比照富邦 `fill_listener.py`：預先指派 order id → callback → 逾時查詢 → position-delta 為最後手段 |
| D3 | 費用模型擴充 | per-share + 最低佣金 + SEC/FINRA。現行 `core/fees.py` 只有 bps，也沒有「按持倉時間計費」（借券費）的概念 |
| D4 | **下單前查券商實際部位** | 現行 `real_coordinator.py` 與 `gate.py` 下單前都不查部位 |
| D5 | 持倉期間部位對帳 | 掛在既有的 15 分鐘保證金檢查上 —— `BrokerAccountSnapshot.positions` 資料已在手，只是沒拿去比對。偵測窗口從「數小時」壓到 15 分鐘 |
| D6 | Recall 應對 | 依 IBKR 實際殘餘股數按比例減碼 CCF；平不掉 → ntfy errors + PAUSE；單日 recall 次數上限則停機 |
| D7 | 整股取整 | 1 口 CCF ≈ 406 股 UMC（中位數），取整誤差 ±0.01% |

**D6 是本系統第一次在異常狀態下主動下單**，違反既有的「任何異常一律 PAUSE，
絕不自作主張交易」安全模型。因此必須釘死：平倉單只能平不能開（方向與數量硬性
斷言）、定量一律用券商實際部位（內部 state 已知是錯的）、失敗必須升級為告警而非
靜默重試。

> **前置條件**：IBKR margin 帳戶。現金帳戶只能驗證約八成（買單、部位查詢、
> 成交確認、整股取整、最低佣金），**放空／借券可用量／recall 這三塊風險最高的
> 無法驗證**。

---

## 6. Phase E — 驗收

1. dry-run soak 完整 UMC session（**前置：IBKR 即時行情到 API**）
2. 極小額實單：1 口 CCF（名目約 320,000 TWD）+ 約 406 股 UMC
3. 富邦 session 切換 runbook：停 QFF/TSM → 起 CCF/UMC

---

## 7. 擋路石（都不是程式碼問題）

| # | 項目 | 擋住 | 現況 |
|---|---|---|---|
| 1 | **IBKR margin 帳戶** | D6/D7 驗證；且回測中**約一半交易需要放空 UMC** | 現金帳戶，不能放空 |
| 2 | **NYSE 即時行情到 API** | Phase E 全部 | 2026-07-27 實測：type 1 → 全 NaN + error 10089；type 3 → 有 last、**無 bid/ask** |
| 3 | IBKR 帳戶餘額 500 USD | 項目 2 | 未確認 |
| 4 | CCF 富邦每口手續費 | A4 的費用正確性 | 用 QFF 的 88 佔位 |
| 5 | QFF/TSM 退役時點 | Phase E 的富邦 session | 待定 |

**項目 2 與決策 5 相乘的後果要講清楚**：directional z-score 需要 bid/ask，
延遲層級不供盤口 → 每根 bar `missing_book skip_signal` → **零進場**。
在即時行情到位前，A/B/C/D 都能建、都能測，但系統不會做出任何一筆交易。

---

## 8. 已知限制

- **樣本**：7 週 / 12,460 根 / **18 筆交易、18 勝 0 敗**。全勝紀錄是警訊不是佐證 ——
  它只說明這 7 週沒出現不利走勢。綁死樣本長度的是 CCF（TAIFEX 只給 30 天，
  目前靠**手動**每日累積，沒有排程）
- **跨週末跳空只有 3 個樣本**。移除週末規則後曝險從 20% 升到 48%
- **replay golden ≠ live 行為**（見 C4）
- **美國假日未模型化**（見 B2）
- **借券費率與 recall 未在回測中模型化**，且造成方向不對稱：
  `Long UMC / Short CCF` 零借券成本，反向有
