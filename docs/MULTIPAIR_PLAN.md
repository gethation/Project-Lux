# 多標的改造計畫 — QFF/TSM + CCF/UMC

目標：把單標的的 Project Lux 改造成能同時執行 N 個配對的系統，第二個配對為
`TAIFEX:CCF`（富邦下單）與 `NYSE:UMC`（IBKR 下單），參數 1m / window 2500 /
entry_z 1.5 / exit_z 0.0。

策略機制本身不變 —— PoC 的 `calculate_ccf_umc_spread.py` 刻意沿用
`qff_*`/`tsm_*` 欄位結構，證明兩個配對是同一套數學。

---

## 0. 已定案的決策

| 項目 | 決定 |
|---|---|
| 執行拓撲 | **單行程、多 pair context**。共用一個 Fubon gateway、一份合併保證金視圖、一個 SQLite store |
| 命名 | **結構泛化為 `tw_leg` / `us_leg`；標的身分（QFF/CCF/TSM/UMC）存成資料值**。CLI、terminal UI、log、報表全程顯示真名 |
| Schema | 全面泛化，破壞性變更 |
| 1m 資料 | 三腿都建**真實** 1m，不做內插近似 |
| FX | **每個 pair 各自設定**。QFF/TSM 續用 BitoPro USDT/TWD；CCF/UMC 用 Twelve Data 真實 USD/TWD（§8.2 已量化驗證此決定） |
| IBKR | `ib_async` + IB Gateway，比照 Fubon 做 subprocess 隔離 |
| 部位大小 | **顯式 `sizing.mode`，預設 `fixed_lots` / `lots = 1`**。`notional` 模式需明確指定。兩個 pair 都預設 1 口，曝險不強求一致 |
| 資金 | 每 pair 固定口數 + 全帳戶合併紅線 |
| 開發節奏 | 分支開發；舊 store 封存不遷移；replay fixture 基準逐字對齊 + dry-run 通過才合併 |
| CCF/UMC 實單 | **本輪不排**。補齊 1m 資料並重跑驗證後再議 |
| 精簡範圍 | 死碼遺留物、合併重複執行/對帳路徑、精簡 CLI、精簡測試 —— 四項全做 |

---

## 1. 現況基線

```
lux_trader/   22,628 行 / 93 個 .py
tests/        14,595 行
qff|tsm 出現  2,345 次，散在 93 個檔案中的 55 個
```

三個硬編碼層次，決定了改造成本：

| 層次 | 位置 | 說明 |
|---|---|---|
| 命名 | `MarketBar.qff_close_filled`、`tsm_twd_fair`、`BrokerName.FUBON_QFF`、`Direction.SHORT_TSM_LONG_QFF`，以及 `bars`/`trades`/`orders`/`fills`/`positions` 全部欄位 | 結構性，非註解 |
| 交易時段 | `core/calendar.py:11` `DAY 8:45–13:45 / NIGHT 17:25–5:00` | 寫死 TAIFEX；warmup index、週末強平全建在上面 |
| 單標的 | `persistence/schema.py:8` `strategy_state ... CHECK (id = 1)` | 一個 store 只能有一組策略狀態 |

券商連線的既有約束：

- Fubon SDK 跑在獨立子行程。**執行 worker 在建構時綁死單一 symbol**
  （`integrations/fubon/execution_process.py:32`）；行情 worker 已是 symbol-agnostic。
- Fubon 一個帳號只能一個 session —— 這是選擇單行程拓撲的根本原因。
- QFF 與 CCF 共用同一個富邦期貨帳戶，保證金是同一個池。

---

## 2. 目標架構

```
configs/live.toml
  [[pairs]] id="qff_tsm"   tw_leg{display="QFF", venue="fubon"}  us_leg{display="TSM", venue="binance"}
  [[pairs]] id="ccf_umc"   tw_leg{display="CCF", venue="fubon"}  us_leg{display="UMC", venue="ibkr"}
                                  │
                    ┌─────────────┴─────────────┐
              PairContext(qff_tsm)        PairContext(ccf_umc)
                    │                           │
         各自: providers / 1m bar builder / indicator / strategy / session calendar
                    │                           │
                    └─────────────┬─────────────┘
                                  │
                    共用: Fubon gateway · 合併保證金監控 · SQLite(pair_id) · dashboard
```

**命名對照**（結構是角色，顯示是身分）

| 結構 | qff_tsm 顯示 | ccf_umc 顯示 |
|---|---|---|
| `tw_leg` | QFF | CCF |
| `us_leg` | TSM | UMC |
| `Direction.LONG_TW_SHORT_US` | Long QFF / Short TSM | Long CCF / Short UMC |

新增第三個配對 = 加一段 TOML，零行程式碼。

---

## 3. Phase 0 — 精簡

**先精簡再泛化。** 泛化要動 2345 處、55 個檔案；先刪掉不需要泛化的東西，
Phase 1 的工作量與測試重寫量都會直接下降。

### 0.1 死碼與遺留物
- `.tmp_pytest` / `.tmp_pytest_live_execute` / `.tmp_pytest_live_execute_core` 三個殘留目錄
- `config.live.smoke.local.toml` 位於 repo root，其餘 config 都在 `configs/` —— 移入或刪除
- `issue/M6_FUBON_SYMBOL_FORMAT_ISSUE.md` —— 若已解決則封存
- `docs/M6_RUNBOOK.md` 目前是**已刪除但未 commit** 的狀態，需確認是刻意的
- **不刪 BitoPro** —— QFF/TSM 續用它作為 FX 來源（見 §8.2，該處 USDT 計價本來就正確）
- **修正 `configs/live.example.toml` 的 `qff_fee_per_contract_twd = 5.0`** ——
  實際使用的 `config.live.exec*.local.toml` 已是 88.0，只有範例檔還停在 5.0。
  照範例複製的人會低估手續費 17.6 倍。
  （`replay.*.toml` 的 5.0 屬凍結回歸基準，**不動**）

安全性檢查結果：`.pfx` 與 `.env` 已正確被 `.gitignore` 排除；三個被追蹤的
`*.local.toml` 經檢查**沒有**內嵌憑證。無需處理。

### 0.2 合併重複的執行/對帳路徑
現況 `integrations/fubon/` 有 execution、execution_process、readonly、readonly_process
四層，職責重疊；`execution/` 下 intent、gate、real_coordinator、simulation、
recorder、outcome、position 界線模糊。

定義單一 `VenueAdapter` 協定：

```
quote(symbol)  ·  historical_1m(symbol, start, end)  ·  place(order)
cancel(id)     ·  positions()                        ·  account()
```

加一層通用的 subprocess 傳輸（現有 Fubon 那套抽出來即可）。之後 Fubon、
Binance、**IBKR 各實作一次**，Phase 3 的成本因此大幅下降。

### 0.3 CLI：14 → 約 6
```
replay · live (--mode dry-run|execute) · status (--funds|--orders|--reconcile)
recover (--clear-pause|--manual-flat) · warmup · summary
```
`exec-smoke` / `manual-close` 收進 gated 的 `admin` 子命令。全部新增 `--pair`。

### 0.4 測試
測試量幾乎等同產品碼，泛化後大批需重寫。趁此合併重複 fixture 與情境。

---

## 4. Phase 1 — 泛化 pair 抽象

1. **資料模型**：`MarketBar`、`PositionSizing`、`Direction`、`BrokerName`、
   `StrategyRuntimeState` → `tw_leg`/`us_leg`
2. **Schema**：新增 `pairs` 對照表；所有表加 `pair_id`；
   `strategy_state` 去掉 `CHECK (id = 1)`，主鍵改 `pair_id`
3. **Config**：`[[pairs]]` 陣列，每個 pair 帶自己的 strategy / sizing / fees / FX / venue 設定。
   **部位大小改為顯式模式，預設固定口數**：

   ```toml
   [pairs.sizing]
   mode = "fixed_lots"        # 預設；另一選項 "notional"
   lots = 1                   # 預設
   # mode = "notional" 時才需要：
   # leg_notional_twd = 1000000.0
   ```

   - `configs/replay.fixture.toml` **必須明寫 `mode = "notional"`** 才能保住凍結基準
   - 保證金分母已能從固定口數推導
     （`margin/service.py:77` + `runtime/live/engine.py:909`），無需改動
4. **Session/Calendar**：抽成 `SessionCalendar` 協定
   - `TaifexSessionCalendar`（現有）—— **CCF 與 QFF 時段完全相同，可直接沿用**（§8.1）
   - `IntersectionCalendar`（TAIFEX ∩ UMC RTH，**必須處理美國 DST 位移**）
5. **Fees / 契約規格**：per-pair。
   - **`tw_leg` 乘數必須 per-pair** —— QFF=100、CCF=**2000**（§8.1）。
     **這是本階段風險最高的參數，需專門測試涵蓋**
   - 期交稅邏輯不變，兩者共用
   - `us_leg` 費用模型要能表達 IBKR 的 per-share + 最低收費 + 賣出規費，
     而非只有 bps

**驗收**：`configs/replay.fixture.toml` 的 replay 結果與現有基準
（`rows=29909, trade_count=66, net_pnl_twd≈261507.83, total_fee_twd≈68317.50`）
**逐字對齊**。這是整個泛化不可協商的門檻。

---

## 5. Phase 2 — 多 pair runtime

> **slice 1 已完成（2026-07-24，branch `phase2-multipair-config`）**：
> 第 1、3、5 項落地 —— `LiveRuntime.for_pairs()` 持 `list[PairContext]` 依序推進；
> 保證金監控改為帳戶層級單一實例、any-pair-open 觸發紅線節奏（動作維持純告警，
> 見 §5.4 註記）；單一 SQLite 檔案由 per-pair store 實例分割（busy_timeout +
> 每 pair 推進後 commit）。**per-pair PAUSED 免費獲得**：狀態本就存於 per-pair
> `strategy_state`，per-pair 迴圈天然「一個停、其他續跑」（有整合測試釘住）。
> 驗收：replay fixture 逐字對齊、555 測試通過。
> 另修一個 slice 1 才暴露的真缺陷：execution plan id 原本不含 pair 成分，
> 兩 pair 同分鐘同方向進場會撞帳戶層級的 ID —— 已改為 `EXEC-{pair_id}-…`。
>
> **第 2 項（execution worker symbol-agnostic）刻意未動**：`self.identity`
> 貫穿回報比對與 recall 偵測路徑（15 處），且 slice 1 沒有任何路徑會用到第二個
> symbol。設計分岔（adapter 逐呼叫帶 symbol vs worker 內 per-symbol adapter 共用
> session）待與使用者確認後獨立成片。
>
> **slice 2「CCF/UMC 接線」已完成（2026-07-24）**：
> - **時段模型**：`us_leg.venue == 'ibkr'` 自動採 TAIFEX ∩ UMC RTH 交集
>   （`runtime/live/pair_session.py`，DST-safe，經冬夏兩側測試釘住）；warmup
>   index 同樣取交集（fill index 維持完整 TAIFEX 時段供 CCF forward-fill）
> - **venue 分派**：us_leg（binance/ibkr）與 fx（bitopro/twelvedata）皆依
>   config 分派（`runtime/live/providers.py`）；IBKR provider 兼作報價與
>   warmup 歷史來源，單一 Gateway 連線
> - **延遲行情**：`us_leg.stale_seconds`（per-pair，可選）承認 IBKR 延遲
>   timestamps；設定後該腿同時退出 skew 檢查（延遲 timestamp 對 skew 的失敗
>   與對新鮮度完全同構），且 **warmup 視窗結尾同步往回退**（實測 2026-07-25
>   盤中：IBKR 歷史只到 15.9 分鐘前，視窗若收在 now−1min 就是向 feed 要
>   不可能存在的 bar）。訂閱 NYSE 後移除該設定，三者一併恢復嚴格
>
> ### ⚠️ 訂閱 NYSE 前，CCF/UMC 不會產生任何交易訊號（已定案，2026-07-25）
>
> 實測：延遲 tier（`marketDataType=3`）**不提供 bid/ask** —— 此 IBKR 帳戶
> 無盤口權限（與歷史資料 `MIDPOINT` 觸發 error 162 同源）。
> `estimate_directional_spread` 需要 us_leg 的盤口才能估可執行價差，因此
> CCF/UMC 每根 bar 都會 `missing_book skip_signal`。
>
> **這是正確行為，不是缺陷。** 曾評估「無盤口時兩側改用 last 價」（與 FX 的
> 處理同構），**經使用者定案否決**：UMC 的買賣價差是真實的執行成本，用 last
> 價估會系統性低估它。寧可在訂閱前完全不出訊號，也不要出一個低估成本的訊號。
>
> 訂閱前**可**驗證：warmup、bar 生成、時段交集、快取、多 pair 共存、對帳。
> 訂閱前**不可**驗證：進出場、部位管理、實單對帳。
> （`--qff-lots` 的尺度對照是**回測**，與訂閱無關，已於 2026-07-25 完成。）
> - **per-pair warmup**：`pairs.warmup_minutes`（與 zscore_window 獨立，
>   使用者定案）；未設則沿用帳戶層級值
> - **欄位遷移**：runtime 讀 pair 的 product/symbol/fx symbol；
>   `[live_market_data]` 同名欄位保留相容但 runtime 不再讀取
> - **CLI**：全 dry-run 多 pair 解鎖（共用一個 Fubon 行情行程）；execute
>   混雜的多選擇仍拒絕（allow_live_order 閘重設留待混合模式片）
> - **config**：`configs/config.multipair.dryrun.local.toml`（CCF 手續費
>   88 TWD 為佔位，未決事項 #2）
> - 驗收：572 測試、replay fixture 逐字對齊
>
> 第 4 項（Dashboard 多 panel）仍未動，留給混合模式片。

1. `LiveRuntime` 改持有 `list[PairContext]`，主迴圈依序推進各 pair
2. **Fubon execution worker 改為 symbol-agnostic** —— symbol 從建構參數改為每次呼叫傳入
3. **保證金**：兩 pair 曝險合併後對全帳戶權益計算比例
   （紅線觸發時的動作見 §5.4）
4. **Dashboard**：多 panel，每個 pair 一格，顯示各自的真實標的名
5. 單一 store，以 `pair_id` 分割；resume / 對帳 / 事件全部 pair-aware

### 5.1 啟用哪些 pair —— config + CLI（已定案）

單行程拓撲下兩個 pair 無法分行程跑（富邦一帳號一 session），所以「這次要跑哪些」
必須是明確開關，否則無法分階段上線（先 QFF/TSM → 加 CCF/UMC dry-run → 兩者實單）。

```toml
[[pairs]]
id = "qff_tsm"
enabled = true      # 此 pair 允許被啟動

[[pairs]]
id = "ccf_umc"
enabled = false     # 設定保留在版控裡，但不載入
```

| 情境 | 行為 |
|---|---|
| `enabled = false` | 不載入。`--pair` 明確指名也不能跑 —— 停用就是停用 |
| `live --mode dry-run`（不帶 `--pair`） | 跑所有 enabled 的 pair |
| **`live --mode execute`（不帶 `--pair`）** | **拒絕啟動並報錯** |
| `live --mode execute --pair qff_tsm` | 只跑指名的；要兩個就重複指定 |

**實單模式強制明寫 `--pair` 的理由**與 `--mode` 必填相同：*在 config 新增一個 pair
不該讓下次啟動默默多交易一個標的。* 擴大實單曝險必須是指令列上看得見的動作。

`--pair` 已於 Phase 0 加入 9 個涉及策略狀態的指令，機制已存在，Phase 2 只需賦予
多值語意。

### 5.2 故障隔離（已定案）

**單一 pair 的問題只停該 pair，另一個繼續。** 對帳不符、合約換月失敗、單腿異常
都是 pair 局部事件，沒有理由連坐健康的另一邊。

例外是**共用資源層級的故障**，本質上就會同時影響兩者，不需要特別的連坐規則：

- 全帳戶保證金紅線 → 依 §5.4 同時平全部 pair
- Fubon SDK session 斷線 → 兩個 pair 的 tw_leg 都不通
- 時鐘偏移、Windows 時間同步失敗 → 全域前置檢查

實作上：`StrategyState.PAUSED` 成為 per-pair 狀態，主迴圈跳過 PAUSED 的 pair 但
繼續推進其餘；`recover` 指令用 `--pair` 指定要解除哪一個。

### 5.3 CCF/UMC 不套用週末規則（已定案，2026-07-24）

**CCF/UMC 這個 pair 移除週末的兩條限制**：週五 session 的進場禁令、以及該
session 最後一根 bar 的強制平倉。**QFF/TSM 維持原樣不動。**

這不是放寬風控換取報酬，而是**移除一條套錯標的的規則**。它的原始理由是：

> Binance TSM 永續 **24/7 交易**，而 QFF 週末凍結 → 週末等於裸露一條腿。

**CCF/UMC 沒有這個風險** —— TAIFEX 與 NYSE 週末都關，兩腿一起凍結，避險比例
完整保持。

#### 實測（1m / w2500 / entry 1.5 / exit 0，29 個 session）

| 政策 | 交易 | 淨利 | 報酬 | 虧損筆數 | 最大回撤 | 曝險 |
|---|---:|---:|---:|---:|---:|---:|
| `flat`（現行，兩條都保留） | 12 | 181,054 | 9.05% | 1 | −25,586 | 20.2% |
| `no-entry`（只移除強制平倉） | 11 | 169,365 | 8.47% | 0 | −25,586 | 28.8% |
| **`none`（兩條都移除）** | **16** | **216,716** | **10.84%** | **0** | **−25,586** | 47.8% |

**最大回撤三者完全相同** —— 多承擔 3 個週末持倉沒有讓最壞情況變差。

#### 貢獻分解：+19.7% 幾乎全部來自解除**進場禁令**

`none` 比 `flat` 多做的 5 筆交易貢獻 47,351，全部是原本被進場禁令擋掉的訊號。
**其中 3 筆持倉不到 5 小時，根本不會碰到週末** —— 它們只是剛好落在「該週最後
一個 session」就被無差別禁止。這是這條規則最大的代價：它禁掉的不是「會跨週末的
交易」，而是那個 session 裡的**所有**訊號。

`no-entry` 反而比 `flat` 差，原因是單一巧合：`flat` 在週五強制平倉讓系統回到空手，
剛好接住週一開盤的新訊號（賺 29,564）；`no-entry` 因為還持有舊部位而錯過。
**這是運氣，不是機制** —— 不應據此保留強制平倉。

原本唯一的那筆虧損（−781）也是強制平倉造成的：它在 z=−1.02 被趕出場（目標是 0），
毛利 +234 扣掉手續費就變負。移除限制後同一筆變成 **+17,095**。

#### 仍然存在的風險

- **週末跳空未被模型化。** 兩腿都凍結代表沒有裸腿，但週一開盤價差可能已跳到不利
  位置，而你在週末無法反應。29 個 session 只涵蓋 3 個週末，樣本完全不足以宣稱
  「週末跳空不是問題」
- **曝險從 20% 升到 48%**，資金佔用時間翻倍以上。兩個 pair 共用富邦帳戶時，這會
  直接影響保證金規劃與紅線觸發機率
- **16 筆交易、0 虧損**這個紀錄比先前的 11 勝 1 敗更不可信，只說明這 29 個 session
  裡沒出現過不利走勢

#### 實作要求

週末規則必須成為 **per-pair 設定**，而非全域常數。現行
`core/calendar.py` 的 `WEEKEND_FORCE_EXIT_GRACE_MINUTES` 與相關遮罩邏輯要能依
pair 關閉。QFF/TSM 預設維持啟用，CCF/UMC 預設關閉。

完整分析見 PoC 的 `docs/ccf_umc_weekend_policy.md`。

### 5.4 保證金紅線觸發時：同時平全部 pair（已定案）

> **實作狀態（2026-07-24）**：本節描述的「自動平倉」在現行系統**尚不存在** ——
> 目前紅線觸發是**純告警**（ntfy + 轉帳指引），連單 pair 都不自動平倉。
> Phase 2 slice 1 只合併了**量測**（any-pair-open 觸發紅線節奏）；
> 「同時平全部 + PAUSED」是會改變 QFF/TSM 實單行為的新功能，
> **獨立成一片實作、config 門控、預設關閉**（使用者 2026-07-24 定案）。
> 屆時 dry-run pair 一視同仁：模擬平倉 + PAUSED。

全帳戶保證金觸及紅線時，**所有 pair 同時平倉**，不做優先序排列。

理由：紅線的定義就是「轉帳來不及了」（見既有的 margin policy）。在那個狀態下，
任何「先平 A 再看 B」的策略都是在賭剩下那個 pair 的行情，而系統對單邊方向本來就
沒有觀點。同時平把判斷從決策路徑上移除。

實作要求：

- 平倉指令**同時送出**，不要序列化後等前一個回報才送下一個 —— 紅線狀態下每一秒
  都在惡化
- 任何一個 pair 平倉失敗，**不影響其餘 pair 繼續平**，但必須立刻告警並 PAUSE
- 全平之後一律 PAUSE，需人工確認保證金已補足才恢復 —— 這與 §5.2 的
  「單一 pair 故障只停該 pair」不同，因為紅線是帳戶層級事件

被捨棄的選項與原因：「先平未實現虧損大的」需要在最緊急的時刻做估值比較；
「先平 z-score 較遠的」等於預測哪一邊會先收斂；固定優先序則讓某個 pair 系統性地
承擔較高風險。三者都在紅線狀態下引入了不必要的判斷。

---

## 6. Phase 3 — IBKR 整合

環境目前完全空白：`ib_async` / `ib_insync` / `ibapi` 皆未安裝，
7496 / 7497 / 4001 / 4002 全部未監聽。

0. **帳戶型態的排序限制（2026-07-23）**：目前是 IBKR **現金帳戶**，不能放空。
   使用者計畫日後升級為 margin 帳戶。

   現金帳戶可驗證的：行情訂閱、歷史 1m、買單、部位/帳戶查詢、成交確認分層、
   整股取整與最低佣金 —— 約八成的 adapter 程式碼。

   **必須等 margin 升級後才能驗證**：放空下單、借券可用量檢查、recall 偵測與
   §5c 的 CCF 按比例減碼。這些正好是風險最高的部分，不可用「應該差不多」帶過。

   策略層面的影響不是小事：**回測中約半數交易需要放空 UMC**（w500 慢組態
   15 筆中有 8 筆）。配對訊號本質對稱，砍掉一個方向就是砍掉一半。現金帳戶期間
   若要實際交易，等於跑一個未經回測的單方向策略。

   另注意現金帳戶的 T+1 交割與 free-riding 限制，回測完全沒有模型化。

1. **環境建置**：IB Gateway 安裝、paper 帳號、**NYSE 即時行情訂閱**（付費）
2. `ib_async` + subprocess 隔離（比照 Fubon）
3. 實作 `VenueAdapter`：行情 / 歷史 1m / 下單 / 部位 / 帳戶
4. **現股特性**（與 Binance 永續期貨的根本差異）：
   - 整股交易，不能分數股（實測影響僅 ±0.01%，見 §8.1）
   - RTH-only；T+1 交割
   - 費用是 per-share + 規費 + **最低佣金**，不是 bps
   - **放空需要真的借到股票** —— 見下方，這是本階段最大的未知數

5. **放空 borrow —— 影響一半的交易**

   永續期貨的「放空」只是開空單；現股放空必須先借到實股。回測方向分佈：

   | | 需放空 UMC（要借券） | 免借券 |
   |---|---|---|
   | 5m best（110 筆） | **55 筆（50.0%）** | 55 筆 |
   | 15m best（41 筆） | 18 筆（43.9%） | 23 筆 |

   需借券的累計持倉時間：88.3 小時（5m）／328.8 小時（15m）。

   三個必須處理的點：
   - **可借量** → 下單前檢查，借不到時的降級/放棄路徑
   - **借券費率** → 年化按日計收。**`core/fees.py` 目前沒有「按持倉時間計費」
     的概念**，費用模型要擴充
   - **強制回補（recall）** → 出借方召回會造成**單腿被平、另一腿仍開著**，
     正是系統最想避免的裸露單腿風險。需要偵測與應對路徑

   另注意：這造成**方向不對稱** —— `Long UMC/Short CCF` 零借券成本，
   `Short UMC/Long CCF` 有。回測沒有模型化這個。

   > **成本面已評估為可忽略**（w500 慢組態：8 筆放空、持倉 1–2 天，
   > 借券費 @3%/年 ≈ 608 TWD、SEC 規費 ≈ 206、FINRA TAF ≈ 266，
   > 合計約 1,000 TWD vs 淨利 182k = 0.55%）。這是慢組態的結構性優勢；
   > 快組態（如 w26 的 110 筆）會把這些固定成本放大一個量級。
   > **真正的問題是 recall，不是費率 —— 見 5b。**

5b. **Recall 造成的單腿裸露 —— 現有系統沒有這條路徑**

   出借方召回時，IBKR 會在盤中任意時點強制買回你的 UMC 空單（buy-in），
   不需要你同意。結果：IBKR 的 UMC 部位歸零，但系統 state 仍是 OPEN、
   Fubon 的 CCF 多單不變 —— **實際上裸多 CCF、零避險，而系統認為自己是
   市場中性的**。

   現有的三個對帳時機：

   | 時機 | 有無 | 位置 |
   |---|---|---|
   | 啟動 / resume | ✓ | `modes.py:493`（註解明講就是為了行程停機期間被平掉的幽靈部位）|
   | 成交後 post-trade | ✓ | `modes.py:753` |
   | **持倉期間、行程運作中** | **✗** | **無** |

   保證金紅線檢查雖然每 15 分鐘跑一次（持倉時），但它比對的是權益/維持保證金，
   不是部位數量。

   **最危險的不是察覺得晚，是察覺的方式**：系統會一路撐到 z-score 觸發出場，
   然後送出一張「買回 UMC 空單」的單 —— 但空單已不存在，**那張買單會開出一個
   新的多單**，變成 Long CCF + Long UMC 雙邊同向。已確認
   `execution/real_coordinator.py` 與 `execution/gate.py` **下單前不查詢實際部位**；
   post-trade 對帳會抓到並 PAUSED，但那是事後。

   **修法很便宜**：`BrokerAccountSnapshot` 已經帶 `positions`
   （`reconciliation/models.py:51`），而保證金監控每 15 分鐘就在抓它 ——
   資料已在手上，只是沒拿去比對。在既有的 15 分鐘檢查裡加一次部位比對，
   就能把偵測窗口從「數小時到一天」壓到 15 分鐘。另需在下單前加一次部位驗證。

   **為什麼 QFF/TSM 沒有這個問題**：Binance 永續期貨沒有借券，不會被 recall；
   只有交易所強平會單邊平倉，而那有保證金監控在看。**UMC 現股是系統第一次
   遇到「第三方可以在你不知情的狀況下平掉你一條腿」。**

5c. **Recall 的應對政策（已定案）**

   | 情境 | 動作 |
   |---|---|
   | 偵測到 UMC 部位與內部 state 不符 | **依 IBKR 實際殘餘股數，按比例減碼 CCF**，維持兩腿避險比例；UMC 全被平掉時即全平 |
   | 減碼成功 | 回 **FLAT**，可繼續交易 |
   | CCF 平不掉（TAIFEX 收盤／週末／下單失敗） | **ntfy errors topic 大聲告警 + PAUSE**，記錄待處理部位，**下一個 TAIFEX 可交易瞬間最優先執行平倉** |

   **這是系統第一次在異常狀態下主動下單**，現有安全模型是「任何異常一律
   PAUSE，絕不自作主張交易」。因此實作上必須釘死：

   - 平倉單**只能平不能開** —— 方向與數量都要有硬性斷言
   - 下單前用**券商實際部位**定量，**不可信任內部 state**（它已知是錯的）
   - 平倉失敗必須升級為告警 + PAUSE，不可靜默重試到天亮

   **已知的殘留風險**：選擇「回 FLAT 可繼續交易」意味著系統可能立刻再進一個
   同樣借不到券的空單，形成 recall 迴圈。建議把 recall 事件計數寫進 store，
   並保留一個「單日 recall 次數上限則 PAUSE」的設定旋鈕。
6. **成交確認分層**：比照現有 Fubon / Binance 的 callback-first 設計
   （預先指派 order id、逾時後查詢、position-delta 為最後手段）

---

## 7. Phase 4 — CCF/UMC 1m 資料與驗證

### 現有資料的實際狀況
| 檔案 | 範圍 |
|---|---|
| `ccf1_5m_taipei_tv.csv` | 2026-06-05 → 07-18 |
| `nyse_umc_5m_taipei_tv.csv` | 2026-04-13 → 07-18 |
| `fxidc_usdtwd_5m_taipei_tv.csv` | 2026-06-22 → 07-18 |
| `ccf_umc_spread_umc_session_5m.csv`（交集） | **2,184 根 / 35 個交易日** |

5m grid 最佳解：`window=500, entry 1.5, exit 0.0` → Sharpe 10.06、**15 筆交易**。
（rank 1 是 `window=390`，Sharpe 10.53、16 筆。）

你要的 1m/2500 = 500 × 5m，**時間跨度一致**，換算正確。但 1m 取樣的 z-score
分母與 5m 不相等 —— **這組參數在 1m 上等於尚未驗證**。

### 三腿的 1m 資料來源
1. **CCF 1m** — TAIFEX tick。現有 downloader 只需把
   `parse_taifex_qff_tick_csv` 裡寫死的 `商品代號 == "QFF"`
   （`integrations/taifex/downloader.py:178`）參數化。
   **但**資料源是 `dlFutPrevious30DaysSalesData`，**只涵蓋最近 30 天**
   → 要建長歷史必須接 TAIFEX 歷史檔庫，或從現在開始每日累積。
2. **UMC 1m** — IBKR `reqHistoricalData`，可回溯數年。需先完成 Phase 3。
3. **USD/TWD** — **已解決，且不需要 1m**（見 §8.2）。研究回填用 FX_IDC；
   live 用 Twelve Data，5m–15m 輪詢即可。

### 驗證

**PoC 回測目前只有 notional 模式**（`--leg-notional-twd`，預設 1,000,000），
沒有固定口數。所以既有回測驗證的部位尺度與實際要下的不同：

| | 回測（notional 1M） | 實際（1 口） |
|---|---|---|
| QFF @1131 | 9 口 = 1,017,900 TWD | 113,100（1/9） |
| CCF @156 | 3 口 = 936,000 TWD | 312,000（1/3） |

訊號不受影響（z-score 與部位大小無關），報酬率大致尺度不變（兩腿費用都隨口數
線性成長）。**但固定最低費用不是** —— IBKR 每筆最低佣金在 1 口的小部位下會變成
不成比例的拖累。

因此：
1. **`backtest_pair_strategy_1m.py` 加上 `--qff-lots`**（sizing 函式加一個分支）
2. 建 1m spread → z(window=2500) → **以 1 口重跑驗證**，含 IBKR 最低佣金的真實拖累

> ### ✅ 已完成（2026-07-25）：`--qff-lots` 實測，尺度不改變經濟性
>
> `scripts/backtest_pair_strategy_1m.py --qff-lots N` 已加入（PoC commit `4136b64`）。
> `0` 維持 notional 模式，與既有基準**逐字對齊**（16 筆 / 216,716.45 / 10.8358% /
> max_dd −25,585.96）。
>
> **1 口 vs notional 1M：一切線性等比例 0.312 倍。**
>
> | | notional 1M | 1 口 |
> |---|---:|---:|
> | 口數（中位數） | 3 | 1 |
> | 單腿名目 | 1,005,000 | 320,500 |
> | 淨利 | 216,716 | 67,641 |
> | **費用佔毛利** | **7.53%** | **7.59%** |
> | **每筆對名目報酬（中位數）** | **0.935%** | **0.935%** |
>
> **最低佣金不成立。** 回測的 us_leg 費用是純 bps、表達不了最低收費，所以另以
> IBKR Fixed 費率手算：1 CCF 口 = 中位數 **406 股 ADR** → `406 × $0.005 = $2.03`，
> 是 $1.00 最低收費的**兩倍**。16 筆進場**零觸及**；要低於約 200 股才會觸及，
> 那是半口以下、CCF 不存在的尺度。
>
> 額外發現：**2.5 bps 模型偏保守而非偏樂觀** —— 每側模型 $2.49 vs 實際 $2.03，
> 差額足以覆蓋模型未計的賣出側 SEC + FINRA TAF（$0.34）。
3. 與 5m notional 結果對照，確認訊號一致、並量化小部位的費用拖累

**看到 1m 的數字之前，不談實單。**

`warmup_minutes` 需從 500 提高到 2500 —— 約 6.4 個 UMC 交易日 / 9 個日曆天，
warmup 資料源必須能回溯這麼遠。

---

## 8. 查證結果（2026-07-22）

### 8.1 CCF 合約規格 — 已確認

| 項目 | QFF（現行） | CCF（新增） |
|---|---|---|
| 商品 | 小型台積電期貨 | 聯電股票期貨 |
| 標的 | 台積電 2330 | 聯華電子 2303 |
| TAIFEX 代碼 | `QF` + `F` | `CC` + `F` |
| **契約單位** | **100 股/口** | **2,000 股/口** |
| 小型契約 | 本身即小型 | **無** —— 小型僅加掛於大立光等級的高價位股，聯電不符 |
| 價位（實測） | ~1,131 TWD | 中位數 **156.0**（區間 118.5–184.5，最後一根 139.0） |
| **一口市值** | ~113,100 TWD | **312,000 TWD**（@156）／278,000（@139）／369,000（@184.5） |
| 日盤 | 08:45–13:45 | 同 |
| 夜盤 | 17:25–次日 05:00 | 同 |
| 最後交易日 | 交割月第三個星期三 | 同 |
| 期交稅 | 契約金額十萬分之二，逐口四捨五入 | 同 |

來源：TAIFEX 股票期貨/選擇權交易標的頁。實證交叉驗證：
`ccf1_5m_taipei_tv.csv` 第一根 bar 落在 **17:25**，與 `core/calendar.py:13`
的 `NIGHT_START = 17*60+25` 完全吻合。

**四項結論**

1. **`tw_leg` 乘數必須是 per-pair 設定** —— QFF=100、CCF=2000，**差 20 倍**。
   這是 Phase 1 泛化時最容易出錯、後果最嚴重的一個參數。
2. **Session calendar 可直接沿用** —— CCF 與 QFF 的 TAIFEX 時段完全相同。
   Phase 1 的新工作只剩「與 UMC RTH 取交集」，`TaifexSessionCalendar` 本身不用改。
3. **稅率邏輯不變** —— `core/fees.py:23` 的
   `round_half_up_nonnegative(price × multiplier × rate)` 與官方「逐口四捨五入」一致，
   對 CCF 直接適用。
4. **部位粒度變粗約 3 倍 —— 這是「預設固定口數」的主要依據。**

   CCF 一口 ≈ 312,000 TWD，是 QFF（113,100）的 **2.8 倍**。依
   `core/sizing.py` 的 round-half-up 規則實算：

   | leg_notional | QFF | CCF @156 | CCF @139 | CCF @184.5 |
   |---|---|---|---|---|
   | 1,000,000 | 9 口，**+1.8%**，每口 11% | 3 口，**−6.4%**，每口 **31%** | 4 口，**+11.2%** | 3 口，**+10.7%** |
   | 2,000,000 | 18 口，+1.8%，每口 6% | 6 口，−6.4%，每口 16% | 7 口，−2.7% | 5 口，−7.8% |
   | 3,000,000 | 27 口，+1.8%，每口 4% | 10 口，+4.0%，每口 10% | 11 口，+1.9% | 8 口，−1.6% |

   **在 1,000,000 之下，CCF 只有 3 口，每一口就是名目的 31%**，且進位誤差
   隨價格在 −6.4% ~ +11.2% 之間跳動（QFF 穩定在 +1.8%）。

   > **此問題已由「預設固定口數」的決定解除**（§0）：固定口數模式下不存在
   > 進位誤差，部位大小完全可預測。這張表保留下來，是為了說明**為什麼**
   > notional 模式對 CCF 不適用 —— 若日後要切回 notional，
   > `leg_notional_twd` 必須拉到 2–3M。

   **好消息**：兩腿不會因此失衡 —— `sizing.py` 是先進位 tw_leg，再用
   **實際** notional 反算 us_leg。而 IBKR 現股的整股進位實測僅 ±0.01%
   （UMC ADR 相對名目夠便宜），不構成問題。

**仍需確認**：CCF 在富邦的每口手續費。QFF 為 88 TWD/口，CCF 費用結構相同
（每口固定），但金額需向富邦確認。

### 8.2 USD/TWD 來源 — 已確認

先用資料確立需求的真實形狀，再選供應商。

**量化一：需要多快？**
以純小時線 FX 取代 5m 拼接序列（同一份 CCF/UMC 資料，window=500 / entry_z 1.5）：

| 指標 | 結果 |
|---|---|
| FX 相對誤差 | 中位數 0.016%、最大 0.21% |
| \|Δz\| | 中位數 0.018、p99 0.152、最大 0.272 |
| 進場訊號不一致 | 0.95% of bars |
| 出場側符號不一致 | 0.71% |

**量化二：能不能乾脆不即時？**
把 FX 凍結在每個 session 的開盤價 → |Δspread| 最大 **0.361 = 36% of spread std**
→ **不可接受**。

盤中波動分解（median intra-session range）：

| 腿 | 盤中波動 |
|---|---|
| CCF（台股期貨） | 3.38% |
| UMC（美股 ADR） | 3.47% |
| **USDTWD** | **0.171%** |

匯率比股價腿小 20 倍，而且原因是結構性的：**UMC 交易時段（台北 21:30–04:00）
完全落在台北外匯市場營業時間（09:00–16:00）之外**，該段期間只有離岸指示性報價。

> **結論：需要即時更新，但 5m–15m 輪詢已充分；1m 沒有必要。**
> 這解除了原先「必須找到 1m USD/TWD」的阻礙。

**量化三：已整合的 BitoPro USDT/TWD 能不能頂替？**

| 指標 | 結果 |
|---|---|
| 基差（USDT/TWD vs USD/TWD） | 平均 +0.235%、std 0.145%、區間 0.84% |
| 對 spread 的影響 | 最大 **0.767 = 77% of spread std** |
| 500-bar 窗內基差漂移 | rolling std 0.133% |
| 基差 autocorr(1) | 0.975（高度持續、緩慢漂移） |

常數基差會被滾動 z-score 自動吸收 —— **但這個基差會在 z 窗內漂移**，
所以消不掉，會直接污染訊號。**排除**。

**量化四：能不能用 `USD/TWD = (USDT/TWD) ÷ (USDT/USD)` 合成？——
已實測，不行，而且更差。**

| | 直接用 BitoPro | 合成 = BitoPro ÷ Kraken(USDT/USD) |
|---|---|---|
| 基差平均 | +0.1457% | **+0.2598%** |
| 基差 std | 0.1934% | 0.2068% |
| \|Δspread\| 最大 | 58% of std | **66% of std** |
| 500-bar 窗內漂移 | 0.1131% | 0.1210% |

*（1,363 根重疊 bar；Kraken 僅能提供 30 天 1h 資料，窗口比量化三窄，
兩組數字不可直接並排比。）*

公式本身正確，但實際數值方向相反：Kraken 的 USDT/USD 中位數 **0.99902**
（USDT 相對美元折價 0.1%），除以 0.999 會讓結果**變大**，而 BitoPro 本來就
已高於 FX_IDC —— 兩個效應同向，修正把誤差推得更遠。

這反而拆解出更有用的資訊：**BitoPro 的溢價不是 USDT 造成的**。台灣本地
加密市場溢價實際是 **0.26%**，只是被 USDT 的 0.1% 折價遮掉一部分才看起來像
0.146%。那 0.26% 是資本管制與套利摩擦的真實經濟楔子，**不在加密這一側，
所以任何加密幣別的組合都消不掉**。

> 結論：USD/TWD 必須來自真實 FX 供應商。需求很鬆（5–15 分鐘輪詢即可），
> Twelve Data 免費層額度充足。

**供應商評估**

| 來源 | USD/TWD | 判定 |
|---|---|---|
| **Twelve Data** | ✅ 已實測 API 回 `{"symbol":"USD/TWD","currency_group":"Exotic"}` | **推薦**。免費層 800 req/day；5m 輪詢一場約 78 次、1m 約 390 次，皆在額度內 |
| TraderMade | 未驗證（宣稱 8000+ pairs） | 備案 |
| **IBKR** | ❌ | **排除**。TWD 僅供台股交易自動換匯、T+1 定價，**非 IDEALPRO 可報價/可交易商品** |
| BitoPro USDT/TWD | ⚠️ 非 USD | 排除（見量化三）。**僅保留給 QFF/TSM** |
| 台銀牌告／央行 | ⚠️ | 排除。更新頻率過低，且 UMC 時段完全在營業時間外 |
| FX_IDC via tvDatafeed | ⚠️ | 僅供研究回填；非官方 API，不用於 live |

**已實測（2026-07-24 06:33 台北，真實 key，免費層 `basic`）**

| 項目 | 結果 |
|---|---|
| `time_series` interval 支援 | **1min / 5min / 15min 全部可用**，間距正確 |
| 最新 bar 延遲 | **2.5–3.5 分鐘** |
| HTTP latency | 0.3–0.6s |
| 額度 | **8 credits/分鐘**、800/日 |
| 與 FX_IDC 交叉比對 | 32.32121 vs 32.30（相隔 1 小時）→ 差 **0.065%**，確認是真的 USD/TWD |

> **`/quote` 不能用來判斷新鮮度。** 它對 forex 回傳的 `datetime` 只有日期粒度
> （`2026-07-24`），拿去算 age 會得到數百分鐘的假值。**live provider 必須走
> `time_series`**，不是 `/quote`。這是實作時最容易踩的坑。

**尚未直接測到的**：量測時間是台北 06:33（美東週四 18:33），**在 UMC 時段之外**。
UMC 時段（台北 21:30–04:00 = 美東 09:30–16:00）落在流動性**更高**的
倫敦／紐約重疊帶，新鮮度理論上只會更好 —— 但這是推論，不是量測。
上線前補一次時段內的探針即可。

探針腳本：`scripts/probe_twelvedata.py`（不印出 key）。

**與 tvdatafeed 的並排實測（20 個取樣、30 秒間隔、2026-07-24 06:43–06:53）**

| | 平均延遲 | 範圍 |
|---|---|---|
| Twelve Data | 116s | 80–139s |
| tvdatafeed (FX_IDC) | 53s | 11–129s |

tvdatafeed 看似較新鮮是**假象**：它回傳**還在形成中的 bar**（06:46:10 時回傳 06:46
那根，該根才存在 10 秒）。Twelve Data 只回完成的 bar，且穩定在**收盤後 30–40 秒**
發布。兩者不是在量同一件事，而套上 300 秒快取後這個差異完全消失。

**結論：live 用 Twelve Data。** tvdatafeed 是非官方 API（執行時自報
`nologin method, data you access may be limited`）、失敗時丟裸 exception、
無速率契約、精度較粗。

> ### ⚠️ Twelve Data 系統性高於 FX_IDC 0.060%
>
> 20 筆取樣**全部同向**，範圍 0.046–0.076%。
>
> **所有既有回測用的都是 FX_IDC**（`fxidc_usdtwd_*_taipei_tv.csv`），
> 上線改用 Twelve Data 會讓 live 的 FX 比回測基準高 0.06%。
>
> 大機率無害 —— 滾動 z-score 吸收**固定**偏差，而 BitoPro 之所以不能用是因為
> 它的溢價在 z-window 內**漂移**（§8.2 量化三）。但這是 10 分鐘的觀察，
> **不足以宣稱長期穩定**。累積夠久的 Twelve Data 樣本後應重跑一次回測比對。

---

## 9. 未決事項

| # | 事項 | 卡住的 Phase |
|---|---|---|
| ~~1~~ | ~~紅線優先序~~ —— **已定案：同時平全部 pair**，見 §5.4 | ~~2~~ |
| ~~1b~~ | ~~IBKR 最低佣金在 1 口部位下的拖累~~ —— **已解決（2026-07-25）：不成立**。實測 1 CCF 口 = 中位數 **406 股** UMC ADR → 佣金 **$2.03**，是 $1.00 最低收費的兩倍，16 筆進場**沒有一筆**觸及門檻（約 200 股以下才會，那是半口以下、不可交易的尺度）。詳見 §7 驗證段 | ~~3~~ |
| 2 | **CCF 富邦手續費** —— 每口固定金額需向富邦確認（QFF 為 88 TWD/口） | 1 |
| 3 | **TAIFEX 30 天限制** —— 接歷史檔庫，或即刻開始每日累積 CCF 1m。**與其他階段無相依，越早啟動越好** | 4 |
| 4 | **UMC 放空 borrow** —— **回測中 50% 的交易需要放空 UMC**（5m best：110 筆中 55 筆，累計持倉 88.3 小時）。需確認 IBKR 的可借券量與費率，並把「按持倉時間計費」加進 us_leg 費用模型 —— 現有 `fees.py` 沒有這個概念 | 3 |
| ~~5~~ | ~~Twelve Data 免費層實測~~ —— **已解決（2026-07-24）**：1min/5min/15min 皆可用、延遲 2.5–3.5 分鐘、8 credits/分鐘 + 800/日、與 FX_IDC 差 0.065%。詳見 §8.2。**剩一件小事**：量測在 UMC 時段外，上線前補一次時段內探針 | ~~4~~ |

---

## 10. 風險

- **樣本量**：35 個交易日、15 筆交易的回測，統計上幾乎沒有結論力，
  且尚未在目標取樣頻率（1m）上驗證過。這是本計畫把實單排在最後、
  且不排入本輪的原因。
- **CCF 乘數 2000 vs QFF 100**：Phase 1 泛化時若這個參數串錯，部位會差 20 倍。
  必須有專門的測試涵蓋。
- **回測與實盤的尺度落差**：既有回測全部是 notional 1M（QFF 9 口 / CCF 3 口），
  實盤是 1 口。**已用 `--qff-lots 1` 實測（2026-07-25）：一切線性等比例，
  費用佔毛利 7.53% → 7.59%，每筆對名目報酬完全相同。IBKR 最低佣金不觸及**
  （1 口 = 406 股 → $2.03，是 $1.00 門檻的兩倍）。此風險項已消除。
- **切回 notional 模式的陷阱**：若日後改用 notional，CCF 一口 312,000 TWD
  的粒度會讓 1M 名目只剩 3 口、進位誤差 −6.4%~+11.2%（§8.1）。
- **FX 語意差異**：已量化確認 BitoPro USDT/TWD 對 CCF/UMC 會造成 77% of
  spread std 的失真且無法被滾動 z 吸收（§8.2）。CCF/UMC 必須用真實 USD/TWD。
- **美國 DST**：UMC 時段在台北時間會整體位移一小時，session 判斷與
  週末強平邏輯都必須正確處理切換日。
- **共用帳戶**：兩個 pair 共用富邦保證金池，合併視圖算錯的後果是實質的。
- **大改造 vs 運行中的系統**：分支開發 + replay fixture 逐字對齊是主要防線。
