# Live 啟動指令

以下指令皆在 PowerShell 中執行。

```powershell
Set-Location 'C:\Users\huang\workplace\Project-Lux'
```

> **升級到 Phase 2 config 之後，`live --mode execute` 必須加上 `--pair`。**
> 不帶 `--pair` 的實單啟動會被拒絕（計畫書 §5.1：擴大實單曝險必須在指令列上
> 看得見）。本文件的 execute 指令已改成新形式；**若你的機器還在跑升級前的版本，
> 舊指令仍然有效**，升級步驟見 `LIVE_UPGRADE_RUNBOOK.md`。
>
> dry-run 不受影響 —— 不帶 `--pair` 會跑所有 `enabled` 的 pair。

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
.\scripts\lux.ps1 live --mode dry-run `
  --config configs/config.live.smoke.local.toml `
  --reset-store `
  --max-iterations 3 `
  --quiet-ui
```

## Live execute

> 警告：`live --mode execute` 會送出真實資金訂單，只能在有人看管、確認商品與部位後執行。

`live --mode execute` 啟動時會先重設 store（若指定 `--reset-store`），接著自動用唯讀 API
核對 Fubon、Binance 與本機策略部位。只有最新 reconciliation 為 `matched` 且其他
下單 gate 全部通過，才會建立真實下單 runner。

若想在啟動前只做唯讀核對，可選擇先執行：

```powershell
$env:LUX_READONLY_BROKER = '1'
try {
  .\scripts\lux.ps1 status reconcile `
    --config configs\config.live.exec.local.toml `
    --readonly
}
finally {
  Remove-Item Env:\LUX_READONLY_BROKER -ErrorAction SilentlyContinue
}
```

啟動全新的真實交易：

```powershell
.\scripts\lux.ps1 live --config configs\config.live.exec.local.toml --pair qff_tsm:execute --reset-store
```

從既有狀態繼續執行：

```powershell
.\scripts\lux.ps1 live --config configs\config.live.exec.local.toml --pair qff_tsm:execute --resume
```

`--pair qff_tsm:execute` 等同於 `--mode execute --pair qff_tsm`；當一個行程要同時
跑不同模式的 pair 時（例如 QFF/TSM 實單搭配 CCF/UMC dry-run），只有前者表達得出來。

`scripts\lux.ps1` 會在 `live --mode execute` 執行期間設定所需的 live-order 環境 gate，並在程序結束後還原原本的環境變數。
每次啟動及 resume 都會重新執行唯讀 reconciliation；核對失敗時不會進入真實下單 runner。
