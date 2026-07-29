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

## 2. Phase A — 剝除與更名 ✅ 完成 2026-07-29

純刪除與機械更名，**不加任何功能**。每一步都有可驗證的不變量。

| 步驟 | commit | 驗收 |
|---|---|---|
| A1 更名 | `acacf94` | 372 passed / 8 skipped，golden **逐字不變** |
| A2 刪除 Binance/BitoPro | `253f465` | 336 passed / 7 skipped（−36 = 被刪的 Binance 測試） |
| A3 週末規則開關 | `db9e0ae` | 340 passed / 7 skipped |
| A4 部位與費用 | `9c5c81f` | 343 passed / 7 skipped |
| A5 CLI 14→7 + A6 schema | `9191162` | 344 passed / 7 skipped |

`lux_trader` 20,470 → 19,246 行（−1,224）。QFF/TSM replay golden
（29,909 bars / 66 trades / net 261,507.82918）**全程未動**。

Phase A 期間新增的三道硬失敗（都是原本會靜默出錯的地方）：
- `fees.ccf_contract_multiplier` 無預設值 —— 少寫會導致 20 倍部位錯誤且完全無跡可循
- `integrations/venues.py` 的五個未接通 venue 一律 raise，不回傳降級替代品
- 開啟 QFF/TSM 舊 store 直接拒絕，而非用 `ensure_column` 補欄位補成半殘狀態

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

## 3. Phase B — 補上 IBKR 行情與 FX ✅ 大部分完成 2026-07-29

| 步驟 | commit | 結果 |
|---|---|---|
| B1 逐檔移植 | `1b3f04f` | 413 passed（+67 移植測試）。審核擋下 4 件事，見下 |
| B2 交集時段 + B3 warmup + B5 換月時點 | `1f0c885` | 419 passed / 7 skipped |
| B4 clock skew 來源 | `3a386f5` | ✅ NTP，429 passed / 7 skipped |

**B1 逐檔審核擋下的四件事**（都不是照單全收）：
1. `reqMarketDataType(3)` 寫死 → 可設定、預設 1(live)。要求 3 會**覆蓋**已持有的授權。
2. **`close` 是價格 fallback 的最後一環** —— 那是前一交易日收盤，而沒有 tick 時間時
   quote 會被蓋上 `observed_at`。合起來是「昨天的價格戴著今天的時間戳」，staleness
   閘門會放行。且**沒授權的 session 正是產生 close-only payload 的情況**。改成 raise。
3. **FX 的 bid/ask 是阻斷級問題**：Twelve Data 無盤口 → `missing_book` → 就算 IBKR
   授權到位也**永遠產不出訊號**。改成 FX 一律取 mid —— 匯率是讓兩腿可比的**參考**，
   不是我們會穿越的盤口；兩腿在各自幣別結算，每筆交易並不換匯。
4. margin panel 從 Binance 欄位改讀 IBKR tag（`UnrealizedPnL` / `MaintMarginReq`）。

**B2/B5 發現：兩條強制平倉規則其實是死的**，原因相同 —— grace window 都錨在
pair 永遠到不了的 TAIFEX 時鐘上：
- **換月**：`force_exit_time = '13:35'` 在日盤。改成 pair session 收盤前
  `force_exit_grace_minutes`（預設 5）。新測試斷言「deadline 全年都不會落在
  08:45–13:45」。
- **週末**：grace window 錨在 TAIFEX 05:00 夜盤收盤，但 pair 04:00 就停了 ——
  整個窗口落在不會被處理的分鐘上，規則接好了卻永遠不可能觸發。同樣改錨到
  pair session 收盤。

RTH 時鐘從 `integrations/ibkr/calendar.py` 移到 `core/us_calendar.py`（純 zoneinfo、
與 venue 無關，放 core 才能不靠 Gateway 測時段）。

**B4 定案：用 NTP，不用券商時間。** 三個理由指向同一邊：
1. 閘門量的是**絕對時間**。loop 用本機時鐘標 bar，staleness 是
   `local_now − quote.timestamp`，對三個各自用真實時間戳的來源同時比較 ——
   本機時鐘一漂，**所有比較同時壞掉**。拿某一家券商的時鐘去驗，驗的是那家的時鐘。
2. **券商探測在券商掛掉時跑不了**，而 IB Gateway 有每日登入畫面 —— 閘門會恰好在
   啟動時失效，且因為 fail closed，變成「開不起來」。
3. **Windows 本來就從 NTP 對時**（同一段 preflight 裡的 `w32tm /resync`）。用 NTP
   驗證才自洽：從 NTP 對時，然後確認那次對時生效。

用 stdlib 實作（SNTP 就是一次 48-byte UDP 交換），不加相依。多台依序試、**第一個
回應就採用** —— 刻意不做多數決：錯誤答案在這裡不會造成錯誤交易（呼叫端 fail
closed，錯的時間只會拒絕啟動），多數決買到的是可用性不是安全性。

Stratum 0（kiss-of-death）與 zero transmit timestamp 一律拒絕而非解析 —— 兩者
硬解都會得到一個**看起來合理**的時刻，而這正是這個閘門絕不能自己發明的東西。

預設伺服器：台灣國家標準時間 → 全球 pool。2026-07-29 實測全部可達、RTT 29–59ms、
本機偏差 +0.05s。**端到端實測過**（不只 mock）：實際閘門回報 0.053s 通過，模擬
+5 分鐘漂移時以 299.9s 被拒。

---

## 3b. Phase B 原始計畫（保留供對照）

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

## 4. Phase C — replay 基準線 ✅ 完成 2026-07-29

CCF/UMC golden 已立（`configs/replay.fixture.ccf_umc.toml` +
`tests/integration/test_replay_golden.py`），**與 PoC `wk_none_0728` 逐位吻合**：

```
rows 12,460   trades 18   winners 18   losers 0
total_pnl_twd  235,726.65723246615
net_pnl_twd    235,726.65723246636
gross_pnl_twd  255,861.06235473434
total_fee_twd   20,134.405122268032
max_drawdown   -25,585.958856977057
exposure_ratio       0.4862967385518422
```

**凍 fixture 時挖出一個一直存在的結構性偏差：replay 的出場成交價用 close，
PoC 用的是下一根 bar 的 open。** 進場端本來就對（用 open），所以這個偏差被掩蓋 ——
數字夠接近，看起來像對齊。在這組資料上它值 9,449 TWD（占 235,726 的 4%），
並且讓一筆獲利變成虧損。

PoC 的規則是：**訊號出場用下一根 open**（你不可能成交在還沒看到的 close），
**強制平倉用 close**（因為觸發它的正是 session 結束）。已照此實作。

舊的 QFF/TSM golden 測試裡寫著 *"Replay sizing/fill logic is byte-for-byte
identical to the PoC backtest"* —— **那句話是錯的**，它把 265,481 → 261,507 的
落差全部歸因於 TAIFEX 資料窗縮短，其中一部分其實是這個。修正後那個數字會變成
270,264；該 fixture 依計畫在此退役，所以記錄而不重新釘住。

**偏離計畫一處**：C3 原本要求連 `weekend_policy = 'flat'` 分支一起刪。**保留。**
Phase B 已把 `is_weekend_force_exit_bar` 重新錨到 pair session，所以 `flat` 現在
是**正確的程式碼**而非接好卻打不到；它有測試，而且它是重跑那個「+19.7%」週末
比較的唯一途徑 —— PoC 的週末分析本身就明說樣本長大後要重驗。

---

## 4b. Phase C 原始計畫（保留供對照）

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

## 5. Phase D — 進度 2026-07-29

| # | 狀態 |
|---|---|
| **D4 下單前查券商實際部位** | ✅ 完成 `e9cd61c`，444 passed |
| **D1/D2 IBKR execution adapter + 成交確認分層** | ✅ 骨架完成，478 passed |
| **D7 整股取整** | ✅ 隨 D1 落在 order 邊界 |
| D3 費用模型 | **被 golden 擋住**，見下 |
| **D5 持倉期間部位對帳** | ✅ 完成，只查 IBKR 側，456 passed |
| D6 Recall 應對 | 可以開始 —— D5 已提供偵測 |

### D4 已完成

`execution/position_guard.py`：下單前讀兩邊券商實際部位。出場必須找到**反向、
等量**的部位；進場必須兩邊都是 flat。三個刻意的決定：**拒絕而非調整**（依券商
回報重新定量 = 用一個剛被證明是錯的世界模型去交易）、**讀不到不等於 flat**、
**失敗時一張單都不送**（包含本來會先成交的富邦腿，否則留下裸腿）。

### D1/D2 已完成（骨架）

`integrations/ibkr/execution.py`。**唯讀是預設**：`IbkrConnectionConfig.readonly`
預設 `True`，execution adapter 是 repo 裡**唯一**傳 `False` 的地方，且用獨立
client id（實測：quote 17002 / readonly 17003 皆為唯讀，execution 17004 才可交易）。

**成交確認分層放在 worker 裡**（`Trade` 物件與事件在那個行程）：事件 → 訂單簿 →
部位差額。分類是重點：
- `filled` —— 終態成交，或部位差額**完全吻合**。半吻合**不算確認**（代表部位還
  因為別的原因動過）
- `failed` —— 終態、零成交、部位未動。**安全**，沒有產生曝險，coordinator 可以
  乾淨地收尾而不必 PAUSE
- `unknown` —— 其餘一切（逾時、部分成交、請求本身失敗）→ **PAUSE**

「被拒絕」與「不知道」都是沒成交，但只有一個可以安全地據此行動。把後者當成前者
回報，正是部分成交的空單變成無人追蹤部位的途徑。

**D7 隨之落地**：整股取整在 **order 邊界**（intent 變成 order 的地方），不在
sizing —— 放 sizing 會動到 golden。**向零取整**（超額避險會留下 CCF 腿蓋不住的
美股曝險），並回報 residual 而非默默丟掉。

兩個 live-order env gate **每次下單都重查**，不是建構時查一次 —— 長時間執行的
行程不能保有一個已被撤銷的權限。

**仍然無法驗證的**（需要 margin 帳戶）：放空、借券可用量、recall 應對。回測中
**約一半的交易要先賣出 UMC**，所以在那之前這實質上是個只做多的 adapter，而只做多
的版本沒有人回測過。**測試全綠不等於可以上線。**

### D3 被 golden 擋住

golden 用 `umc_fee_bps = 2.5` 對齊 PoC。換成 per-share + 最低佣金 + SEC/FINRA
會讓 replay 不再重現 PoC。正確做法是 replay 保留 bps、live 用真實模型 ——
且實測 bps 是**偏保守**的（中位數 406 股時模型 $2.49/邊 vs 實際 $2.03），所以
回測沒有美化自己。

### D5 的前提是錯的（計畫本身的錯誤，已查證）

計畫原文說「`BrokerAccountSnapshot` 已經帶 `positions`，而保證金監控每 15 分鐘
就在抓它 —— **資料已在手上，只是沒拿去比對**」。**不成立。**

`margin/service.py:fetch_margin_snapshot` **刻意**優先用輕量的 `fetch_margins()`
而非完整 `fetch_snapshot()`，理由寫在該函式的 docstring 裡：富邦的
`query_single_position` 在頻繁輪詢下**受流量控管（業務系統流量控管）**。這是別人
先前刻意繞開的限制，不是疏漏。

所以 D5 需要一個設計決定，不是「加一行比對」：
- 部位查詢用**比保證金更低的頻率**（例如 30–60 分鐘）？
- 只在持倉期間查（red_line 已經是這個條件）？
- 或者只查 IBKR 側（它的 readonly 沒有 `fetch_margins`，本來就走完整快照，
  順帶就有 positions）—— **而 recall 風險本來就只在 UMC 這一腿**？

**已定案（使用者 2026-07-29）：只查 IBKR 側。** 零額外富邦流量，而且正好覆蓋唯一
會被第三方平掉的腿 —— TAIFEX 期貨沒有借券，唯一的非自願平倉是交易所強平，而那
本來就有保證金監控在看。

**已實作**：`reconciliation/position_drift.py`（純比對）+ margin monitor 掛在既有
排程上。IBKR 的 readonly 沒有 `fetch_margins`，所以保證金檢查**本來就在拉它的完整
快照**，positions 免費附帶 —— 只是從來沒拿去比對。偵測到 drift 時 loop 直接
PAUSE 並立即持久化（drift 可能發生在隨後因 staleness 被跳過的 bar 上，只存在記憶體
裡的 PAUSE 會被下次重啟抹掉）。有測試斷言**富邦的完整快照不會被請求**。

**接線時抓到的 bug**：PAUSE 原本呼叫 `store.record_strategy_state` —— **那個方法
不存在**，執行時會拋 `AttributeError` 直接打死 live loop，而且正好在最要緊的那條
路徑上、當時還沒有任何測試會走到。已改用 `save_state`。

---

## 5b. Phase D 原始計畫（保留供對照）

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
