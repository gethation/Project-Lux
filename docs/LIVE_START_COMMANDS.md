# Live 啟動指令

以下指令皆在 PowerShell 中執行，從 repo 根目錄。

> **本分支目前跑不起 live。** UMC 的行情與下單通道在 Phase A2 隨 Binance 一起移除，
> IBKR 與 Twelve Data 的替代品分別在 Phase B 與 Phase D 才進來
> （見 `docs/CCF_UMC_PLAN.md`）。在那之前，任何需要美股腿的指令都會以
> `UsLegVenueNotWired` 明確失敗 —— 這是刻意的，不是故障。
> 這份文件先照新的 CLI 形狀寫好，Phase B/D 完成後即可照用。

## CLI 對照表（14 → 7）

| 舊指令 | 新指令 |
|---|---|
| `live-dry-run` | `live --mode dry-run` |
| `live-execute` | `live --mode execute` |
| `doctor` | `status doctor` |
| `live-status` | `status live` |
| `broker-status` | `status broker` |
| `reconcile-brokers` | `status reconcile` |
| `margin-check` | `status margin` |
| `clear-pause` | `recover clear-pause` |
| `recover-manual-flat` | `recover manual-flat` |
| `warmup-live` | `warmup` |
| `exec-smoke` | `admin exec-smoke` |
| `manual-close` | `admin manual-close` |

`replay` 與 `summary` 名稱不變。

## Live dry run

使用真實即時行情及模擬成交，不會送出真實訂單：

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs\config.live.exec.dryrun.local.toml --reset-store
```

從既有狀態繼續執行：

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs\config.live.exec.dryrun.local.toml --resume
```

若只需有限次數的啟動驗證：

```powershell
.\scripts\lux.ps1 live --mode dry-run --config config.live.smoke.local.toml --reset-store --max-iterations 3 --quiet-ui
```

## Live execute

> 警告：`live --mode execute` 會送出真實資金訂單，只能在有人看管、確認商品與部位後執行。

啟動時會先重設 store（若指定 `--reset-store`），接著自動用唯讀 API 核對 Fubon、
IBKR 與本機策略部位。只有最新 reconciliation 為 `matched` 且其他下單 gate 全部
通過，才會建立真實下單 runner。

若想在啟動前只做唯讀核對：

```powershell
$env:LUX_READONLY_BROKER = '1'; try { .\scripts\lux.ps1 status reconcile --config configs\config.live.exec.local.toml --readonly } finally { Remove-Item Env:\LUX_READONLY_BROKER -ErrorAction SilentlyContinue }
```

啟動全新的真實交易：

```powershell
.\scripts\lux.ps1 live --mode execute --config configs\config.live.exec.local.toml --reset-store
```

從既有狀態繼續執行：

```powershell
.\scripts\lux.ps1 live --mode execute --config configs\config.live.exec.local.toml --resume
```

`scripts\lux.ps1` 會在 `live --mode execute` 期間設定所需的 live-order 環境 gate，
並在程序結束後還原原本的環境變數。每次啟動及 resume 都會重新執行唯讀
reconciliation；核對失敗時不會進入真實下單 runner。

## 富邦 session 衝突

CCF 與 QFF 共用同一個富邦帳號，SDK 只允許一個 session。**啟動本系統前必須先停掉
另一台機器的 QFF/TSM live**，否則會把對方的 session 踢掉。過渡期兩套系統輪流跑，
CCF/UMC 上線後 QFF/TSM 退役。
