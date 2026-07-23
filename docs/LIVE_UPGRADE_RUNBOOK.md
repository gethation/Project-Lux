# Live 機器升級手冊 — 舊版 → Phase 0+1

QFF/TSM 的 live-execute 跑在**另一台機器**上，目前是 Phase 0/1 合併**之前**的程式。
本文件是把那台機器升級到 `master` 的完整步驟。

**現在不需要急著做。** 那台機器跑的是實戰驗證過的版本；新程式雖然 453 測試綠、
replay golden 逐值吻合，但**從未在 live 環境跑過**。新程式的價值在於支援第二個標的，
而 CCF/UMC 本來就還要等 NYSE 行情訂閱才有意義。等你有一段能安心停機的時間再做。

---

## 為什麼不能直接 `git pull`

| 變更 | 後果 |
|---|---|
| **Schema v1 → v2** | 新程式**拒絕開啟**舊 store，明確報錯不會靜默毀損 |
| **CLI 14 → 7 個指令** | 舊指令名全部移除，`live-execute` 等會回 `invalid choice` |
| **config 改為 `[[pairs]]` 格式** | 舊格式的 config 無法載入 |
| **`configs/*.local.toml` 有被 git 追蹤** | pull 會覆蓋那台機器上的本地設定 |

最後一項最容易被忽略：那些檔案的註解寫著「gitignored」，**但實際上是被追蹤的**。
如果你在 live 機器上改過 store 路徑、口數或 symbol，pull 會蓋掉。

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

### 5. 驗證程式本身（不碰券商）

```powershell
python -m pytest -q
```

預期 **453 passed, 8 skipped**。

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

### 6. 開新 store 跑 dry-run

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs/config.live.exec.dryrun.local.toml --reset-store --ui dashboard
```

`--reset-store` 是必須的 —— 舊 store 是 v1，新程式會拒絕。

**觀察至少一個完整交易時段**，重點看三件事：

1. **富邦 SDK 連線與重連** —— Phase 0 把 subprocess transport 重寫過，這是最可能
   出現細微差異的地方
2. **合約換月與週末強平** —— `SessionCalendar` 抽成協定後判斷路徑改變了
3. **Dashboard 顯示的標的名** —— 應顯示 `QFF`/`TSM`，不該出現 `tw_leg`/`us_leg`

### 7. 轉實單

dry-run 沒有異常後：

```powershell
.\scripts\lux.ps1 live --mode execute --config configs/config.live.exec.local.toml --reset-store
```

實單的安全閘全部未變（`safety.allow_live_order`、`[live_execution] enabled`、
三個 `*_ALLOW_LIVE_ORDER=1` 環境變數、以及啟動時的唯讀對帳）。已驗證：存放這些閘門的
三個 handler 檔案與升級前 **blob hash 完全相同**，CLI 只改了路由。

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

---

## 升級後才有的能力

| 能力 | 說明 |
|---|---|
| `--pair` | 9 個涉及策略狀態的指令都接受，目前只有 `qff_tsm` |
| 多標的地基 | schema、config、calendar 全部 pair-aware。加第二個 pair 不需要再動這些 |
| 固定口數為預設 | `mode = 'fixed_lots'`, `lots = 1`，符合你「先用 1 口測試」的習慣 |

**還沒有的**：多 pair 同時執行（Phase 2）、IBKR 下單（Phase 3）、CCF/UMC 實際交易。
升級本身不會改變 QFF/TSM 的交易行為 —— replay golden 逐值不變就是這件事的證明。
