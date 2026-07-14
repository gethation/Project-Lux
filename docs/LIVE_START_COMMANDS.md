# Live 啟動指令

以下指令皆在 PowerShell 中執行。

```powershell
Set-Location 'C:\Users\huang\workplace\Project-Lux'
```

## Live dry run

使用真實即時行情及模擬成交，不會送出真實訂單：

```powershell
.\scripts\lux.ps1 live-dry-run --config configs\config.live.exec.dryrun.local.toml --reset-store
```

從既有狀態繼續執行：

```powershell
.\scripts\lux.ps1 live-dry-run --config configs\config.live.exec.dryrun.local.toml --resume
```

若只需有限次數的啟動驗證：

```powershell
.\scripts\lux.ps1 live-dry-run `
  --config config.live.smoke.local.toml `
  --reset-store `
  --max-iterations 3 `
  --quiet-ui
```

## Live execute

> 警告：`live-execute` 會送出真實資金訂單，只能在有人看管、確認商品與部位後執行。

`live-execute` 啟動時會先重設 store（若指定 `--reset-store`），接著自動用唯讀 API
核對 Fubon、Binance 與本機策略部位。只有最新 reconciliation 為 `matched` 且其他
下單 gate 全部通過，才會建立真實下單 runner。

若想在啟動前只做唯讀核對，可選擇先執行：

```powershell
$env:LUX_READONLY_BROKER = '1'
try {
  .\scripts\lux.ps1 reconcile-brokers `
    --config configs\config.live.exec.local.toml `
    --readonly
}
finally {
  Remove-Item Env:\LUX_READONLY_BROKER -ErrorAction SilentlyContinue
}
```

啟動全新的真實交易：

```powershell
.\scripts\lux.ps1 live-execute --config configs\config.live.exec.local.toml --reset-store
```

從既有狀態繼續執行：

```powershell
.\scripts\lux.ps1 live-execute --config configs\config.live.exec.local.toml --resume
```

`scripts\lux.ps1` 會在 `live-execute` 執行期間設定所需的 live-order 環境 gate，並在程序結束後還原原本的環境變數。
每次啟動及 resume 都會重新執行唯讀 reconciliation；核對失敗時不會進入真實下單 runner。
