# Live 機器升級手冊 — 舊版 → Phase 2

QFF/TSM 的 live-execute 跑在**另一台機器**上，目前是 Phase 0/1 合併**之前**的程式。
本文件是把那台機器升級到 `master`（含 Phase 2 多配對）的完整步驟。

**現在不需要急著做。** 那台機器跑的是實戰驗證過的版本；新程式雖然 596 測試綠、
replay golden 逐值吻合，但**從未在 live 環境跑過**。等你有一段能安心停機的時間再做。

**升級的價值不在 CCF/UMC。** 那條配對還卡在 NYSE 即時行情訂閱（見
`configs/config.multipair.dryrun.local.toml` 裡 `us_leg.stale_seconds` 旁的量測記錄），
沒訂閱就產不出訊號。升級的實際理由是讓 live 機器跟開發線收斂，不要再讓兩邊的程式
愈差愈遠。

**升級的實際風險在哪裡：** golden replay 逐值不變只證明**核心策略數學**沒被動到。
它走的是中價 z + 次根 bar 成交，而 live 走的是 bid/ask 定向 z + 同根 bar 成交 ——
**golden 結構上覆蓋不到 live 路徑**。Phase 2 改動了 10 個 QFF/TSM live 會經過的檔案，
其中兩個特別要注意：

| 檔案 | 改動量 | 為什麼要緊 |
|---|---|---|
| `integrations/fubon/execution.py` | +97 | 實單送單路徑（「一個 adapter 對多個合約」的改造） |
| `core/tradable_spread.py` | +84 | live 決策唯一的 z 來源，golden 零覆蓋 |

其餘：`core/calendar.py` +78、`integrations/binance/execution.py` +53、
`integrations/fubon/execution_process.py` +50、`execution/outcome.py` +31、
`execution/real_coordinator.py` +24、`execution/price_policy.py` +19、
`execution/intent.py` +15、`core/strategy.py` +14。

所以步驟 6 的完整時段 dry-run **不是形式**，它是唯一會實際執行到這些改動的關卡。
另外，舊的 M6 兩腿實單驗收是在 `fubon/execution.py` 改造**之前**做的，**結論不能沿用**。

---

## 為什麼不能直接 `git pull`

| 變更 | 後果 |
|---|---|
| **Schema v1 → v2** | 新程式**拒絕開啟**舊 store，明確報錯不會靜默毀損 |
| **CLI 14 → 7 個指令** | 舊指令名全部移除，`live-execute` 等會回 `invalid choice` |
| **config 改為 `[[pairs]]` 格式** | 舊格式的 config 無法載入 |
| **`live --mode execute` 需要 `--pair`** | 舊的啟動指令／排程會被**拒絕**，不會誤啟動 |
| **`configs/*.local.toml` 有被 git 追蹤** | pull 會覆蓋那台機器上的本地設定 |

最後一項最容易被忽略：那些檔案的註解寫著「gitignored」，**但實際上是被追蹤的**。
如果你在 live 機器上改過 store 路徑、口數或 symbol，pull 會蓋掉。

> Phase 2 **沒有再破一次 schema** —— `SCHEMA_VERSION` 仍是 2。如果那台機器已經在
> Phase 0+1 上，這次升級不需要換 store。從 Phase 0/1 **之前**升上來才需要。

---

## 前置條件（缺一不可）

- [ ] **部位已平倉。** 換 store 等於系統失去部位記憶；帶倉升級會讓系統狀態與券商
      實際部位脫節，而系統會以為自己是空手的
- [ ] **live 行程已停止**，不是暫停
- [ ] 有一段不交易的時間窗口（至少能容納 dry-run 觀察）
- [ ] 這台開發機**沒有**在跑任何連富邦的東西（一帳號一 session）

平倉的確認方式是查券商而非查 store：

```powershell
$env:LUX_READONLY_BROKER='1'
python -m lux_trader status reconcile --config <你的config> --readonly
```

> 注意：這是**升級後**的指令寫法。升級前那台機器上要用舊的
> `python -m lux_trader reconcile-brokers --config <cfg> --readonly`。

---

## 步驟

### 1. 備份（在 live 機器上）

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item data\project_lux_live_execute.sqlite3 "data\archive\live_execute.schema-v1.$stamp.sqlite3"
Copy-Item configs\config.live.exec.local.toml "configs\archive\config.live.exec.local.$stamp.toml"
git rev-parse HEAD | Out-File "data\archive\pre-upgrade-commit.$stamp.txt"
```

最後那行記下升級前的 commit —— 回退時要用。

### 2. 保存本地設定差異

```powershell
git diff configs/ | Out-File "configs\archive\local-config-diff.$stamp.patch"
git status --short configs/
```

如果有輸出，**先看過那份 patch**，確認哪些是你在那台機器上刻意改的
（store 路徑、口數、symbol、ntfy topic 等），升級後要重新套用。

### 3. 更新程式

```powershell
git fetch origin
git stash push -u -m "live machine local config before upgrade"
git checkout master
git pull
```

用 `stash` 而非直接 checkout，是因為 config 被追蹤，直接切分支會被拒絕或覆蓋。

### 4. 還原你的本地設定

打開新版 `configs/config.live.exec.local.toml`，對照步驟 2 的 patch，把你原本的
值填回**新的 `[[pairs]]` 結構**裡。新格式的關鍵欄位：

```toml
[paths]
store_path = 'data\project_lux_live_execute.sqlite3'

[[pairs]]
id = 'qff_tsm'
label = 'QFF/TSM'

[pairs.sizing]
mode = 'fixed_lots'      # 預設；固定口數
lots = 1

[pairs.strategy]
entry_z = 2.0
exit_z = 0.5
zscore_window = 500
```

**特別檢查 `[pairs.sizing]`** —— 新版預設是固定口數 1 口。如果你原本跑的是
notional 模式，必須明寫 `mode = 'notional'` 與 `leg_notional_twd`。

**還要檢查兩件 Phase 2 帶進來的事：**

1. **`fubon_env_path` 必須是 `'.env'`，不是 `'env'`。** 四個 local config 原本都少一個點，
   指向不存在的檔案；`load_dotenv` 找不到就靜默跳過，所以富邦登入只在 shell 已經
   export 過憑證時才會成功。如果那台機器是靠 shell 環境變數活著的，這個修正之後
   它會改從 `.env` 讀 —— 確認那台機器的 `.env` 內容正確。

   同時確認 `.env` 裡**沒有** `FUBON_ALLOW_LIVE_ORDER`。`lux.ps1` 在
   `live --mode execute` 時會自己設三個閘門變數；把它留在 `.env` 等於讓每個讀 config
   的行程都帶著一個實單閘。

2. **確認只有一個 `[[pairs]]`，或第二個沒有 `enabled`。** exec config 應該維持單一
   `qff_tsm`。dry-run 不帶 `--pair` 會跑**所有** enabled 的 pair。

### 5. 驗證程式本身（不碰券商）

```powershell
python -m pytest -q
```

預期 **596 passed, 8 skipped**。

```powershell
python -m lux_trader replay --config configs/replay.fixture.toml --reset-store
python -m lux_trader summary --config configs/replay.fixture.toml
```

必須逐值吻合：

| 欄位 | 值 |
|---|---|
| `rows` | `29909` |
| `trade_count` | `66` |
| `net_pnl_twd` | `261507.82918245535` |
| `total_fee_twd` | `68317.49687897251` |

**有任何一項不符就停下來回報，不要繼續。**

這四個值在 Phase 2 分支上實測仍然逐值吻合，且 `tests/integration/` 與
`tests/fixtures/replay/` 在整個 Phase 2 期間**沒有被修改過** —— 所以這不是重新校準
出來的吻合。但別把它讀成「live 行為不變」：它證明的是策略數學不變，見開頭那張表。

### 6. 開新 store 跑 dry-run

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs/config.live.exec.dryrun.local.toml --reset-store --ui dashboard
```

`--reset-store` 是必須的 —— 舊 store 是 v1，新程式會拒絕。

**觀察至少一個完整交易時段**，重點看五件事（後兩項是 Phase 2 新增的）：

1. **富邦 SDK 連線與重連** —— Phase 0 把 subprocess transport 重寫過，Phase 2 又改了
   `execution_process.py`（+50），這仍是最可能出現細微差異的地方
2. **合約換月與週末強平** —— `SessionCalendar` 抽成協定後判斷路徑改變了，Phase 2 的
   per-pair weekend policy 又動了 `core/calendar.py`（+78）
3. **Dashboard 顯示的標的名** —— 應顯示 `QFF`/`TSM`，不該出現 `tw_leg`/`us_leg`
4. **定向 z 的數值合理性** —— `core/tradable_spread.py` +84 行且 golden 零覆蓋。
   比對 `short_zscore`／`long_zscore` 與中價 `spread_zscore` 的差距是否仍在
   合理範圍（QFF/TSM 正常盤口下約 0.05–0.10 z）。**差距異常放大代表盤口讀錯了。**
5. **有沒有真的走到下單決策** —— 一個沒有進出場訊號的時段，等於沒有測到
   `fubon/execution.py` 那 97 行。若整段觀察都沒觸發，這關**不算過**，
   要用 `admin exec-smoke` 補（見下）。

### 7. 補做兩腿實單驗收

**不要從 dry-run 直接跳到 execute。** 舊的 M6 驗收是在「一個 adapter 對多個合約」
改造之前做的，那 97 行就在送單路徑上，結論不能沿用。用最小尺寸重新確認一次
兩腿都送得出、對得上、平得掉。

`tests/smoke/test_live_execute_smoke.py` 是這件事的既有工具（預設 skip，
由環境變數開啟）。

### 8. 轉實單

前面都沒有異常後：

```powershell
.\scripts\lux.ps1 live --config configs/config.live.exec.local.toml --pair qff_tsm:execute --reset-store
```

**`--pair` 是 Phase 2 新增的必填項。** `--mode execute` 不帶 `--pair` 會被無條件拒絕
（`cli/pair_selection.py`），而且這個檢查在查 enabled pair **之前**，所以就算 config
只有一個 pair 也一樣要寫。

實單的安全閘全部未變：`safety.allow_live_order`、`[live_execution] enabled`、
三個 `*_ALLOW_LIVE_ORDER=1` 環境變數、以及啟動時的唯讀對帳。

> **對照 Phase 0+1 版本的說法**：那時記錄「三個 handler 檔案 blob hash 完全相同」。
> 這在 Phase 2 **已不成立** —— `execution/gate.py` 本身確實仍然逐位元組相同
> （blob `7bede0af`，master 與 Phase 2 一致），但呼叫它的
> `runtime/live/modes.py`(+55)、`cli/commands_live.py`(+71)、`config.py`(+132)
> 都改過。閘門的**判斷邏輯**沒變，**周圍的路徑**變了。

---

## 回退

新程式在 live 出現任何無法立即理解的行為時，不要現場除錯，直接退回：

```powershell
# 1. 停掉 live 行程
# 2. 退回程式
git checkout <步驟 1 記下的 commit>      # 合併前是 8171132

# 3. 還原舊 store
Copy-Item "data\archive\live_execute.schema-v1.<stamp>.sqlite3" data\project_lux_live_execute.sqlite3

# 4. 還原舊 config
Copy-Item "configs\archive\config.live.exec.local.<stamp>.toml" configs\config.live.exec.local.toml
```

舊程式配舊 store 可以直接繼續跑。**這是保留備份的全部意義** —— 沒有備份就沒有回退。

回退後把觀察到的現象告訴我，不要自己改新程式。

---

## 新舊指令對照

| 舊 | 新 |
|---|---|
| `live-dry-run` | `live --mode dry-run` |
| `live-execute` | `live --mode execute` |
| `live-status` | `status live` |
| `broker-status` | `status broker` |
| `reconcile-brokers` | `status reconcile` |
| `margin-check` | `status margin` |
| `doctor` | `status doctor` |
| `clear-pause` | `recover clear-pause` |
| `recover-manual-flat` | `recover manual-flat` |
| `warmup-live` | `warmup` |
| `exec-smoke` | `admin exec-smoke` |
| `manual-close` | `admin manual-close` |
| `replay` / `summary` | 不變 |

**`live --mode` 是必填、沒有預設。** 這是刻意的：漏打參數不可能誤觸實單。

完整的逐旗標對照在 `docs/CHECKPOINT_1_REPORT.md` §3。

### Phase 2 的 pair 選擇

**實單啟動指令多一個必填參數。** `live --mode execute` 不帶 `--pair` 會被拒絕：

```powershell
# 舊
.\scripts\lux.ps1 live --mode execute --config configs/config.live.exec.local.toml --resume
# 新
.\scripts\lux.ps1 live --config configs/config.live.exec.local.toml --pair qff_tsm:execute --resume
```

理由是計畫書 §5.1：config 多一個 pair 不該讓下次啟動默默多交易一個標的，所以擴大
實單曝險必須在指令列上看得見。**dry-run 不受影響**，不帶 `--pair` 會跑所有 `enabled`
的 pair。

升級前先確認你的啟動腳本／排程有沒有寫死舊指令 —— 這是唯一會讓升級後起不來的改動。

---

## 升級後才有的能力

| 能力 | 說明 |
|---|---|
| `--pair` | **每個吃 `--config` 的指令**都接受（結構性測試強制，見 `tests/unit/test_cli_parser.py`） |
| 多 pair 同時執行 | 一個行程一個迴圈跑 `list[PairContext]`，因為富邦一帳號限一 SDK session |
| Venue dispatch | us_leg 可選 `binance`／`ibkr`，FX 可選 `bitopro`／`twelvedata`，都由 pair config 指定 |
| Per-pair 週末政策 | QFF/TSM 保留週末強平，CCF/UMC 用 `weekend_policy = 'none'` |
| RTH 交集時段 | us_leg 在 IBKR 的 pair 只在 TAIFEX ∩ NYSE RTH 交易 |
| 固定口數為預設 | `mode = 'fixed_lots'`, `lots = 1`，符合你「先用 1 口測試」的習慣 |

**還沒有的**：IBKR 下單（Phase 3）、CCF/UMC 實際交易（卡在 NYSE 即時行情訂閱）、
成本模型裡的資金費率。

**不要把「升級不改變 QFF/TSM 交易行為」當成已證明的結論。** replay golden 逐值不變
只涵蓋策略數學；live 專屬路徑（定向 z、同根 bar 成交、富邦送單）沒有等價的凍結基準，
那正是步驟 6 與 7 存在的理由。
