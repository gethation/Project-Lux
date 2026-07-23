# Codex 委派工作流程

Project Lux 的實作外包給 Codex，我（Claude）負責需求確認與獨立驗收。
本文件是每次委派的執行手冊，定案於 2026-07-23。

---

## 角色分工

| 階段 | 誰 | 產出 |
|---|---|---|
| 1. 需求與細節確認 | Claude ↔ 使用者 | 所有決定點先問，不自行假設 |
| 2. 撰寫規格與提示 | Claude | 可執行的規格書 + 委派提示檔 |
| 3. 實作 | Codex | 程式碼 + 逐子任務 commit |
| 4. **獨立驗收** | Claude | 親自重跑，不採信報告聲稱 |
| 5. 回報與確認 | Claude → 使用者 | 摘要 + 差異清單 |

**驗收必須獨立。** Checkpoint 1 的經驗：親自跑測試、實測 golden tripwire 是否真的
會響（8/8 攔截）、逐行看實單路徑 diff，才抓得出報告與現實的落差。

---

## 什麼該委派，什麼自己做

判準不是任務難易，而是三個比值：

| 問題 | 委派 | 自己做 |
|---|---|---|
| 寫規格成本 vs 實作成本 | 規格**遠小於**實作 | 兩者相當（那不如直接做）|
| 驗收成本 vs 實作成本 | 有**機械式**驗收條件 | 驗收要靠判斷 |
| 需要幾輪經驗迴圈 | 一次講清楚就能做完 | 試 → 看結果 → 調整 → 再試 |

**委派**：面廣、機械式、有不可爭辯的驗收條件。
例：Phase 1 的 96 檔案／2870 行改名，驗收就是 golden 逐值不變。

**自己做**：需要緊密經驗迴圈、要跟活系統互動、或判斷本身就是工作內容。
例：IBKR 行情切片（連上去才知道拿到什麼 → 調整 → 再試）、flaky 測試調查
（10 次對照實驗與假設檢定）。這類工作委派出去，每一輪都要付
「寫規格 → 等 → 驗收」的來回成本。

**特別保留給自己**：任何會在**異常狀態下主動下單**的邏輯（例如 recall 偵測與
CCF 按比例減碼）。那是系統唯一違反「異常一律 PAUSE」原則的地方，不隔一層。

> 附帶效益：即使最後決定自己做，**先寫一段規格仍然值得** —— 它逼人把隱性決定
> 講明白。Phase 0/1 的規格逼出了表格範圍、sizing 預設、golden 容忍度這些
> 若邊寫邊決定就會前後不一致的東西。只是這種情況不必寫到交付等級。

## Session 策略：resume 還是開新的

**判準：同一任務的續作 → `resume --last`；新的階段 → 新 session。**

預設開新 session，因為**repo 本身就是共享狀態** —— 規格書、程式碼、git log、
前一份 checkpoint 報告都在，對話記憶不是必要的傳遞管道。好處是不夾帶過期假設
（Phase 0 時「CLI 有 14 個指令」是真的，Phase 1 時已經是 7 個）、可重現、
邊界清楚。

用 resume 的時機：任務被中斷但已累積昂貴的分析成果，且**沒有跨越階段邊界**。

**resume 的陷阱**：它會繼承原 session 的 model / effort / **sandbox** 設定。
曾經因為原 session 被 `--full-auto` 降級成壞掉的沙箱，resume 時必須顯式帶上
`--sandbox danger-full-access` 才修好。resume 不是無腦繼承就好。

## 啟動 Codex

### 固定參數

| 項目 | 值 | 理由 |
|---|---|---|
| 執行檔 | `C:\Users\Huang\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe` | **不在 PATH 上**，必須用完整路徑 |
| 模型 | `gpt-5.6-sol` | 使用者指定 |
| 推理強度 | `xhigh` | 使用者指定 |
| 沙箱 | `--sandbox danger-full-access` | 使用者指定最大權限 |
| **禁用** | `--full-auto` | 見下方 |
| 視窗 | `-WindowStyle Hidden` | 見下方 |
| 提示傳遞 | 寫成檔案，`Get-Content -Raw \|` 導入 | 避開所有引號轉義問題 |

### 絕對不要加 `--full-auto`

已棄用，而且會**覆蓋** `--sandbox` 設定。實測 log 顯示即使指定
`danger-full-access`，實際仍以 `workspace-write` 執行 —— 而該模式需要
`codex-windows-sandbox-setup.exe`，這台機器**根本沒有那個檔案**
（`bin/` 只有 `codex.exe` 與 `codex-code-mode-host.exe`）。結果是
`orchestrator_helper_launch_failed: program not found`，Codex 完全無法執行 shell：
不能跑 pytest、不能 git commit，只能用 node_repl 做唯讀檔案 I/O。

### 隱藏視窗，不要可見視窗

2026-07-23 有一輪因為使用者不小心關掉 PowerShell 視窗而中斷
（exit code `-1073741510` = `STATUS_CONTROL_C_EXIT`）。隱藏視窗沒有可以誤關的目標。
要停止時用 `Stop-Process -Id <pid>`；Claude 每次啟動後會回報 PID。

### 啟動指令樣板

```powershell
$proj  = "D:\Users\Work place\Project Lux"
$codex = "C:\Users\Huang\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log   = Join-Path $proj ".codex-logs\codex-$stamp-<task>.log"

$inner = "Set-Location '$proj'; Get-Content '<promptfile>' -Raw | " +
         "& '$codex' exec --skip-git-repo-check -m gpt-5.6-sol " +
         "--config model_reasoning_effort='xhigh' --sandbox danger-full-access " +
         "-C '$proj' 2>&1 | Tee-Object -FilePath '$log'"

Start-Process powershell -ArgumentList '-WindowStyle','Hidden','-Command',$inner
```

Resume 既有 session 時，旗標放在 `exec` 與 `resume` 之間：

```powershell
... | & $codex exec --skip-git-repo-check --sandbox danger-full-access resume --last
```

---

## 監控（啟動時必須同時掛上）

**啟動與監控是同一個動作，不可分兩步。** 曾經漏掛一次，結果 Codex 死了三分鐘都
沒人發現，直到使用者主動問起。

兩個訊號：

1. **每個 commit 落地時通知** —— 看得出進度是否停滯或走偏
2. **行程結束時通知** —— 不論正常完成或中途死亡，並一併回報新增的 commit、
   未提交殘留、log 尾端的結束原因

因為隱藏視窗的行程不受 harness 追蹤，監控必須自己輪詢 PID 與 `git log`。

---

## 使用者跟看指令

每次啟動後，Claude 必須提供這一行（帶當次實際的 log 路徑）：

```powershell
Get-Content "D:\Users\Work place\Project Lux\.codex-logs\<當次log>" -Wait
```

`.codex-logs/` 已列入 `.gitignore`。

---

## 委派提示的固定結構

1. **禁令清單放最前面** —— 沙箱已關，提示是唯一防線。逐一列出禁止的指令
   （`live --mode execute`、`admin exec-smoke`、`admin manual-close`、
   `status broker/reconcile/margin`，以及任何需要 `*_ALLOW_LIVE_ORDER=1`、
   `LUX_READONLY_BROKER=1`、`LUX_LIVE_MARKETDATA=1` 的操作），並明說
   「沙箱不會攔你，你是唯一防線」。同時列出**允許**的指令白名單。
2. **硬性不變量** —— golden 基準的實際數值、實單路徑不得改變行為、
   測試通過/跳過數、不得新增第三方相依。
3. **逐子任務 commit** —— 每個 task id 一個 commit，**每次 commit 前跑
   `pytest -q`**，完成一個就提交，不要拖到最後。
4. **停下來問的規則** —— 遇到規格未定義一律停，寫進 `docs/HANDOFF_QUESTIONS.md`，
   但**只卡住真正未定義的部分**（自己標為「無歧義」的要先做完）。
5. **明確的階段終點** —— 「做到 X 就停，不要開始 Y」。

### 為什麼堅持逐子任務 commit

2026-07-23 有一輪 Codex 完成了 21 個檔案、約 1000 行的改動卻**一次都沒 commit**，
行程被殺後全部懸在工作區，只因驗收者手動搶救才沒有損失。

---

## 驗收檢查清單

不看報告聲稱，全部自己跑：

- [ ] `pytest -q` 自行重跑，數字與基線對照
- [ ] replay golden 自行重跑，逐值比對
- [ ] **確認 Codex 真的自己跑過測試** —— 比對 log 裡的執行時間與自己的是否不同
      （相同就可疑，可能是抄的）
- [ ] `git diff --name-only` 確認無範圍蔓延
- [ ] 觸及實單路徑的改動**逐行看**；最強證據是 handler 檔案的 blob hash
      與 master 相同
- [ ] 報告的表格抽樣比對實際程式碼（例如 CLI 對照表 vs `parser.py`）
- [ ] log 裡搜尋禁令指令，判讀是實際執行還是僅提及
- [ ] 檢查是否照要求切分 commit

驗收結果以**聊天摘要 + 差異清單**形式給使用者，需要決定的用提問處理。

---

## 富邦 session 互斥 —— 跨機器的約束

**富邦一個帳號只能一個 SDK session。** QFF/TSM 的 live 目前跑在**另一台機器**上，
所以只要它在跑，這台開發機**不得執行任何會連上富邦的指令**，否則可能把正在交易的
session 踢掉：

```
status broker · status reconcile · status margin
live --mode dry-run · live --mode execute · admin exec-smoke · admin manual-close
```

連帶影響：**新程式的 dry-run soak 沒辦法在開發機上做**，必須挑 live 停機的空檔。
CCF/UMC 的 dry-run 之後也受同一條約束（CCF 行情同樣來自富邦）。

IBKR 不受此限 —— 不同券商，且只在開發機上使用。

## 已知環境問題

| 問題 | 狀態 |
|---|---|
| `codex-windows-sandbox-setup.exe` 不存在 | 無法使用任何需要沙箱的模式；用 `danger-full-access` 繞過 |
| Codex 建立 ACL 鎖死的暫存目錄 | `.tmp_pytest*`、`.codex_pytest_tmp` 連系統管理員以外都刪不掉；已列入 `.gitignore`，需以 `takeown` + `icacls` 清除 |
| 行尾符號 | Codex 產生的檔案可能造成純 CRLF/LF 差異；用 `git diff --ignore-all-space` 分辨實質改動 |
