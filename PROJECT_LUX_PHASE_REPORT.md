# Project Lux Phase Plan and Test Report

?湔?交?嚗?026-06-25

## 1. 撠?蝮質汗

Project Lux ??QFF/TSM ??鈭斗?蝟餌絞??撠???嗆??敹?撘???`lux_trader/`嚗葫閰虫???`tests/`??
撠???荔?銝餉?鈭斗??摩撌脣???`D:\Users\Documents\Proof of Concept` ???單?撽??oC ?舐洵銝???亥??箇??箸?靘?嚗??思漱???pread/z-score 閮??脣?湧?瑼颯雿?sizing?祥?具FF 鈭斗??挾?身嚗誑??replay/backtest ??reference summary?roject Lux ?遙???舫??啁???伐????PoC 撽???銵?渡???函蔡?皜祈岫??Ｗ儔??郊??live market data ?頂蝯望瑽?
?迨 Phase 1 ?敹??嗆?皞?roject Lux replay 蝯?閬? PoC reference 撠????鈭斗?甈⊥??雿nL ?祥?具?敺遙雿??亥????湛??賣?閰脫?蝣箄???銝阡??啣? PoC ???reference dataset 撽???
2026-06-24 撌脤??啣?朣??PoC嚗?
- Reference CSV ?寧 `qff_tsm_spread_zscore_1m_taipei_qff_session_w500.csv`??- Indicator ?芯蝙??QFF trading-session bars嚗FF non-trading session 銝?rolling window??- QFF 蝻箄????active session ??forward-fill??- 蝑??箏???`zscore_window=500`?entry_z=2.0`?exit_z=1.0`??- `exit_z=1.0` 隤??箄???moving average 撠銝?`abs(z)>1`嚗hort-spread position ??`z < -1` exit嚗ong-spread position ??`z > 1` exit??- ??PoC reference summary嚗rows=29909`?trade_count=66`?net_pnl_twd=265481.318343568`?total_fee_twd=68321.48561792255`?final_equity_twd=2265481.318343568`??
?桀?蝟餌絞撌脣? Phase 1 ??PoC CSV replay ?游???Phase 4 ??`live-dry-run`?hase 4 隞銝?閮曹遙雿?撖虫??殷?雿歇?賜?祕 market data?ead-only reconciliation?uto warmup ??simulated execution adapter 頝?entry/open/exit/PnL ???湧?瞍?蝔?
## 2. Phase 1 ??Phase 5 ?格?

| Phase | ?格? | 銝餉??批捆 | 銝???|
| --- | --- | --- | --- |
| Phase 1 | PoC CSV replay MVP | 霈??PoC CSV??蝞?rolling z-score?? PairStrategy?aperBroker?QLite store?esume?ummary | 銝 API嚗?銝 |
| Phase 2 | Live market data + PaperBroker | ??Fubon marketdata?AIFEX downloader?inance/BitoPro ccxt嚗遣蝡?live warmup?xpiry buffer QFF ?貊???1m bar polling | ?芸? paper order |
| Phase 3 | Read-only broker reconciliation | Fubon/Binance read-only broker嚗?乓?其??憪??靽??????? broker/store 撠董 | 銝 |
| Phase 4 | Dry-run execution | 蝑?Ｙ? execution plan嚗? simulated adapter ?Ｙ? `DRYRUN-*` orders/fills嚗?啁??亦??rade?nL ??equity | 銝??殷?璅⊥?漱 |
| Phase 5 | Minimal live execution | 撠?Phase 4 validated execution intent ?亙?祕 Fubon/Binance execution adapter嚗??亙???safety gate?ost-trade reconciliation ?仃? `PAUSED` | 憭? gate ??敺??迂?撠祕??|

Phase 5 ?舐洵銝??閮梁?撖阡??畾蛛?雿?閮凋?敹??????config ?憓???safety gate ?券???roker/store 撠董???? execution plan ?芸銵?????閮梢?撠祕?柴?
## 3. Phase 2 live market data ?批捆

Phase 2 ?璅蝣箄? live market data pipeline ?臭誑?舀? paper trading嚗?
- Fubon marketdata ?舐?乩蒂?? QFF candidates??- QFF active contract 雿輻 expiry buffer policy嚗?箸??拙??頝?敺漱??喳? 5 ??璆剜??QFF??- 憒???QFF ????eligible active contract 撌脣???蝑蝜潛?雿輻??蝝?敺?exit signal嚗?唳?敺漱?????璆剜 13:35 隞?箏嚗孛??force exit??- TAIFEX 摰??30 ?漱??疏瘥??漱 CSV ZIP ?臭?頛蒂????QFF 1m close??- Fubon QFF intraday candles ??TAIFEX fallback ?臬?雿菜? QFF warmup source??- Binance `TSM/USDT:USDT` ?舫? `ccxt binanceusdm` ?? ticker/OHLCV??- BitoPro `USDT/TWD` ?舫? `ccxt bitopro` ?? ticker/OHLCV??- `live-paper` ??Phase 2 甇?虜蝟餌絞?亙嚗????炎??SQLite seed bars嚗?頞單??芸??瑁? warmup??- `warmup-live` ?身?Ｙ? 500 ??QFF session seed bars嚗?????debug / 撽 / ???遣撌亙??- `live-paper` 瘥? polling quote嚗??芸 1 ??摰?敺銵??亙?瑯?- ?冽?蝔?雿輻 `PaperBroker`嚗??澆隞颱? Fubon/Binance 銝 API??
## 4. Phase 2 皜祈岫閮

### 4.1 ?Ｙ? deterministic tests

?桃?嚗?霅??撌梁??摩嚗?靘陷憭 API??
?賭誘嚗?
```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_market_data.py -q
```

閬??批捆嚗?
- QFF symbol parser?ront-month fallback selector?xpiry buffer active selector??- Expiry buffer contract policy嚗? ??璆剜?瑼颯ligible active symbol ???-1 13:35 force-exit deadline??- Fubon symbol `QFFG6` 撠? TAIFEX contract month `202607`??- TAIFEX HTML CSV ZIP link parser??- TAIFEX tick CSV ????1m QFF close??- Fubon/TAIFEX ??????雿菜?嚗ubon 閬? TAIFEX??- QFF 蝻箏???forward-fill??- TSM ??USDT/TWD 蝻箏???fail fast??- `WarmupRunner` safety gate嚗allow_live_order=true` ??敺１隞颱? provider??- `QffWarmupCheckRunner` ?臬?冽葫 QFF leg??
### 4.2 QFF-only 撖阡????皜祈岫

?桃?嚗?券?霅?Fubon + TAIFEX ??QFF warmup leg嚗?蝣?Binance/BitoPro嚗?頝??乓?
?賭誘嚗?
```powershell
$env:LUX_LIVE_MARKETDATA='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader qff-warmup-check --config configs/config.live.smoke.local.toml --output-csv=
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

??璇辣嚗?
- Fubon marketdata login ????- QFF candidates ??閫??嚗蒂?詨蝚血? expiry buffer ??active symbol??- Fubon QFF 1m candles ?征??- TAIFEX 摰 CSV ZIP 銝?????- TAIFEX QFF ticks ?航??? 1m close??- ?蔥敺?rows = `config.live.warmup_minutes`嚗??閮剔 500 ??QFF session bars??- `qff_close_filled_nulls = 0`??- 頛詨 `source_rows`?source_used_counts`?verlap mismatch summary??
### 4.3 摰 live market-data smoke

?桃?嚗?霅?Phase 2 live warmup 雿輻?祕 Fubon?AIFEX?inance?itoPro 鞈?皞?
?賭誘嚗?
```powershell
$env:LUX_LIVE_MARKETDATA='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config configs/config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_smoke.py -q -m live_marketdata
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

??璇辣嚗?
- `live-doctor` ?臬?敺?QFF active symbol?inance quote?itoPro quote??- `tests/test_live_smoke.py` 撖阡?????- `warmup_bars = config.live.warmup_minutes`嚗??閮剔 500??- `bars = 0`?orders = 0`?fills = 0`?trades = 0`??- 摰 `live-paper` startup smoke ?蝙??`data/live_paper_startup_smoke.sqlite3`嚗?蝛?store ????霅?`warmup_auto`??撖?quote polling?AR ??skipped-minute event嚗誑??resume 銝?撱?warmup??
### 4.4 ?典?獢?regression tests

?桃?嚗Ⅱ隤?Phase 2 銝憯?Phase 1 replay ???亦?????
?賭誘嚗?
```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest
```

??璇辣嚗?
- Phase 1 replay integration tests ????- Strategy/store/calendar/sizing/indicator tests ????- ?芾身摰?`LUX_LIVE_MARKETDATA=1` ??live smoke tests ??skip??
## 5. ?桀?撌脣??璅?
### Phase 1 撌脣???
- 撱箇? `lux_trader/` package ??CLI??- 撱箇??詨? models?trategy state?aperBroker?QLiteStore??- ?舀 PoC CSV replay??- ?舀 rolling 500 session-bar z-score嚗ddof=0`??- ?舀 QFF trading calendar??- ?舀 position sizing?ees?rade summary??- ?舀 SQLite resume??- 撱箇? Phase 1 unit/integration tests??
### Phase 2 撌脣???
- ?啣? `configs/live.example.toml`??- ?啣? `live-doctor`?warmup-live`?live-paper`??- `live-paper` ?身?芸??? startup warmup嚗--skip-warmup` ?航?瘙??歇??seed bars嚗??fail fast??- ?啣? `qff-warmup-check`嚗?桃皜?Fubon + TAIFEX QFF warmup??- ?啣? Fubon QFF marketdata adapter??- ?啣? ccxt ticker/OHLCV provider??- ?啣? TAIFEX official CSV ZIP downloader??- ?啣? QFF warmup source report??- ?啣? ExpiryBufferContractPolicy嚗?  - eligible active QFF = ??拙??頝?敺漱??喳? 5 ??璆剜??蝝?  - FLAT / ENTRY_PENDING ????唳 active symbol ????敺?撱?500 ??session-bar warmup??  - OPEN / EXIT_PENDING ????唳 active symbol ??蝬剜???蝝??exit signal??  - ?敺??券?箄?憟??敺漱?????璆剜 13:35 force exit??- SQLite `warmup_bars`?bars`?orders`?fills`?trades` 撌脰?閮?`qff_symbol`?qff_expiry`?contract_policy_state`??- `strategy_state` 撌脰?閮?`trading_qff_symbol`?eligible_active_qff_symbol`?pending_symbol_switch`?last_warmup_symbol`??- ?啣? Fubon + TAIFEX + Binance + BitoPro live smoke tests??- ?啣?摰 `live-paper` startup smoke嚗???撖?API auto warmup?arket ticks?inute finalize / skip event ??resume??- Project root 撌脣??`.env` ??`B121371533.pfx`嚗蒂??`.gitignore` 靽風??- `data/taifex_cache/`?data/qff_warmup_check_*.csv`?ubon runtime `log/` 撌脣蕭?乓?
## 6. ?桀?撌脣??葫閰衣???
?桀?蝝??撽?蝯?憒?嚗?
```text
pytest tests/test_contract_policy.py tests/test_live_market_data.py -q
27 passed
```

```text
LUX_LIVE_MARKETDATA=1 pytest tests/test_live_smoke.py -q -m live_marketdata
3 passed
```

```text
pytest
37 passed, 3 skipped
```

QFF-only 撖阡????皜祈岫蝝??

```text
qff-warmup-check passed
qff_symbol=QFFG6
qff_expiry=2026-07-15
contract_policy_state=active
rows=1440
source_rows={"fubon": 294, "taifex": 3826}
source_used_counts={"forward_fill": 869, "fubon": 294, "taifex": 277}
qff_close_filled_nulls=0
```

摰 `warmup-live` CLI 皜祈岫蝝??

```text
Warmup complete: bars_written=1440, qff_symbol=QFFG6
```

SQLite 撽?嚗?
```text
counts={'warmup_bars': 1440, 'bars': 0, 'orders': 0, 'fills': 0, 'trades': 0}
metadata_or_value_nulls=0
symbols=[('QFFG6', '2026-07-15', 'active', 1440)]
```

摰 `live-paper` startup CLI 皜祈岫蝝??

```text
live-doctor passed
qff_candidate_session_counts={"AFTERHOURS": 5, "REGULAR": 0}
qff_active_symbol=QFFG6
qff_active_expiry=2026-07-15

live-paper --reset-store --max-iterations 130
EVENT warmup_auto start
EVENT warmup_auto done_1440
Live-paper stopped: iterations=130, bars_processed=3, skipped_minutes=0, qff_symbol=QFFG6

live-paper --resume --max-iterations 70
WARN stale_tsm skipped_minute
Live-paper stopped: iterations=70, bars_processed=0, skipped_minutes=1, qff_symbol=QFFG6
```

SQLite 撽?嚗?
```text
counts={'warmup_bars': 1440, 'bars': 3, 'orders': 0, 'fills': 0, 'trades': 0, 'market_ticks': 600, 'live_runs': 2}
sources=[('binanceusdm', 200), ('bitopro', 200), ('fubon_qff', 200)]
symbols=[('QFFG6', '2026-07-15', 'active', 1440)]
metadata_or_value_nulls=0
duplicate_bars=0
```

## 7. 撠摰???蝥極雿?
### Phase 2 敺?鋆撥

- Session-aware warmup 撌脣???敺??芷?鋆摰??warmup quality controls??- 閮剖? QFF forward-fill 瘥? warning/fail ?瑼颯?- ??游??渡? warmup quality summary??- ?亙??嫣漱?/?銵????誨蝚砌???weekday + configured holiday list??- 鋆憭?expiry buffer resume ??皜祈岫嚗?憒??楊???亙?????- ?Ⅱ?渡? indicator state ??摮??遣?輻???
### Phase 3 ??撌乩?

- Commit 1嚗歇摰? read-only broker domain skeleton嚗???snapshot/reconciliation ??ake broker ??mismatch ?斗?桀?皜祈岫??- Commit 2嚗歇摰? SQLite reconciliation tables ??`broker-doctor` / `reconcile-brokers` CLI skeleton嚗ake/stub 鞈?瘚頝?- Commit 3嚗歇摰? Fubon read-only adapter嚗 `margin_equity`?single_position`?oday orders嚗?撖?smoke ? `LUX_READONLY_BROKER=1`??- Commit 4嚗歇摰? Binance read-only adapter嚗? `.env` 霈 `BINANCE_API_KEY` / `BINANCE_SECRET`嚗 balance?ositions?pen orders??- Commit 5嚗???Fubon + Binance + Store reconciliation acceptance嚗洵銝??mismatch ??warning + record嚗??餅? `live-paper`??
Commit 1-2 skeleton ?誘嚗?
```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader broker-doctor --config configs/live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config configs/live.example.toml --fake
```

Commit 3-4 read-only smoke 蝝??

```text
broker-doctor: FUBON_QFF positions=0 open_orders=0 margins=5
broker-doctor: BINANCE_TSM positions=0 open_orders=0 margins=1
reconcile-brokers --fubon-readonly --fake-binance: status=matched, issues=0
reconcile-brokers --readonly: status=matched, issues=0
pytest tests/test_readonly_brokers_smoke.py -q -m readonly_broker: 2 passed
```

### Phase 4 撌乩?蝝??
Phase 4 ?格??臬遣蝡?dry-run execution嚗??亦?餈?撖虫??桃?? execution plan嚗?銝隞颱? Fubon/Binance ?祕憪????啣祕雿歇敺??intent-only 閮剛????箏???simulated execution lifecycle嚗live-dry-run` ? `SimulatedExecutionAdapter` ?Ｙ? simulated `DRYRUN-*` orders/fills嚗蒂???靘?`live-execute` ?梁??state applier ?湔 `OPEN` / `FLAT`?rade?nL ??equity??
- Commit 1嚗歇摰? execution intent domain嚗???`PairExecutionPlan`?ExecutionLeg`?ExecutionCheck`?ExecutionPlanStatus`?ntry/exit ? side mapping?OrderRequest -> ExecutionLeg/Plan` 頧?嚗誑??dry-run validator??- Commit 2嚗歇摰? SQLite recorder ??CLI skeleton嚗憓?execution intent tables嚗蒂??fake mode 頝?intent ?Ｙ???霅摨怨? summary??- Commit 3嚗歇摰? strategy order builder refactor嚗??桀? `PairStrategy` ?湔?澆 `broker.place_order()` ?楝敺??箇? order request builder嚗aperBroker 銵蝬剜?銝???- Commit 4嚗歇摰? `live-dry-run` ?祕 market data 瘚?嚗???Phase 2 auto warmup?uote polling?id/ask tradable spread?alendar ??contract policy嚗???砍閮? intent 銝阡?`PAUSED`嚗??啁??砍歇?勗???simulated execution lifecycle ?誨??- Commit 5嚗歇摰? dry-run failure simulation嚗??遙銝?踹仃?辣?脯?瘨artial fill嚗遙雿?摰?蝯??賜雁??recommended `PAUSED`嚗??芸?鋆??- Commit 6嚗歇摰??祕 read-only + dry-run smoke嚗?頝?Phase 3 broker reconciliation嚗?頝?dry-run execution嚗??圈??嗡???瘙?`orders=0` / `fills=0`嚗閬?瘝??祕 broker order API嚗? simulated orders 雿輻 `DRYRUN-*`??
Commit 1 execution intent domain 蝝??

```text
commit: f30d72d feat: add execution intent domain
pytest tests/test_execution_intent.py -q: 11 passed
pytest -q: 93 passed, 6 skipped
```

Commit 1 validator 閬?嚗?
- ??? intent ?? validation??- entry/exit?SHORT_TSM_LONG_QFF` / `LONG_TSM_SHORT_QFF` side mapping 甇?Ⅱ??- missing leg?rong side?ero quantity?FF ??詨?詻rong QFF symbol ??rejected??- `allow_live_order=true` ??rejected嚗hase 4 隞?敺??函?撖阡??
Commit 2 SQLite recorder + CLI skeleton 蝝??

```text
?啣? tables: execution_plans, execution_legs, execution_checks
?啣? CLI: dry-run-doctor, live-dry-run --fake, execution-summary
pytest tests/test_execution_intent.py tests/test_execution_recorder_cli.py -q: 18 passed
pytest -q: 100 passed, 6 skipped
```

Commit 2 撽嚗?
- fake `live-dry-run` ?舐??valid execution intent嚗alidation ??敺誑 `recorded` ??神??SQLite??- rejected fake case ?神??execution checks 銝虫誑 nonzero exit code 蝯???- dry-run recorder 銝神??`orders`?fills`?trades`??- `allow_live_order=true` ?◤ `live-dry-run` ????
Commit 3 strategy order builder refactor 蝝??

```text
commit: f2c9512 feat: refactor strategy order builders
pytest tests/test_strategy_store.py tests/test_replay_integration.py -q: 7 passed
pytest -q: 102 passed, 6 skipped
```

Commit 3 撽嚗?
- `PairStrategy` ?臬??build entry/exit ? `OrderRequest`嚗?敹??喳??broker??- TSM symbol 敺?hardcode ?? strategy 撱箸??嚗?閮凋???`TSM/USDT:USDT`??- replay / PaperBroker path 隞蝙?典?銝蝯?builder 敺?submit嚗??皜祉???霈?
Commit 4 live-dry-run real market data 蝝??

```text
?啣? LiveDryRunRunner
?啣? CLI real mode: live-dry-run --config ... --reset-store --max-iterations ...
pytest tests/test_live_market_data.py -q: 41 passed
pytest -q: 103 passed, 6 skipped
```

Commit 4 撽嚗?
- `live-dry-run` 銝? `--fake` ??韏啁?撖?market data runner嚗窒??startup auto warmup?uote polling?inute finalize ??bid/ask tradable spread decision??- `ENTRY_PENDING` / `EXIT_PENDING` 銝??澆 PaperBroker fill嚗?Ｙ? `PairExecutionPlan` 銝血神??execution tables??- 甇?commit ???? intent ?Ｙ?敺?`PAUSED` 銝?撖?`orders` / `fills` / `trades`嚗?蝥?Phase 5 ?蔭隤踵撌脣?甇方楝敺??simulated execution lifecycle??- ???live 銵??finalized minute 蝣箄? entry/exit signal 敺??典?銝??bar 蝡?瑁? simulated execution嚗???dry-run entry ?神 simulated `DRYRUN-*` orders/fills 銝阡?`OPEN`嚗???dry-run exit/force-exit ?神 trade/PnL 銝血? `FLAT`??
Commit 5 failure simulation 蝝??

```text
?啣? ExecutionSimulationScenario: leg_failure, delay, cancel, partial_fill
?啣? table: execution_simulations
?啣? CLI: simulate-execution --scenario ... [--fake-plan]
pytest tests/test_execution_recorder_cli.py tests/test_execution_intent.py -q: 22 passed
pytest -q: 107 passed, 6 skipped
```

Commit 5 撽嚗?
- simulator ?舫?撠?recorded `PairExecutionPlan` 璅⊥隞颱??踹仃?辣?脯?瘨? partial fill??- simulation ?芸神??`execution_simulations` / `events`嚗?撖怠 `orders`?fills`?trades`??- `simulate-execution --fake-plan` ?臬遣蝡?deterministic plan 敺?交芋?研?- 銝蝙??`--fake-plan` ??CLI ????store ???execution plan ?脰?璅⊥??- ???failure simulation payload ?賢葆 `recommended_state=paused`嚗?蝥?execution gate ?舀?甇日????柴?
Commit 6 real read-only + dry-run smoke 蝝??

```text
?啣? test: tests/test_dry_run_smoke.py
pytest tests/test_dry_run_smoke.py -q: 1 skipped without env gates
pytest -q: 107 passed, 7 skipped
LUX_LIVE_MARKETDATA=1 + LUX_READONLY_BROKER=1 pytest tests/test_dry_run_smoke.py -q -m "live_marketdata and readonly_broker and dry_run_smoke": 1 passed
```

Commit 6 撽嚗?
- smoke ?閬??身摰?`LUX_LIVE_MARKETDATA=1` ??`LUX_READONLY_BROKER=1`嚗?閮剜葫閰衣憓??１?祕 API??- 皜祈岫? Fubon / Binance read-only broker ??reconciliation嚗???`matched` ?匱蝥?- 皜祈岫撖怠 `ENTRY_PENDING` seed state嚗??祕 market data 頝券?蝚砌???finalized minute 敺??dry-run entry execution嚗?靘陷撣?末?箇 entry signal??- `LiveDryRunRunner` 撖阡?摰? auto warmup?arket ticks?inute finalize?xecution plan record ??simulated execution outcome??- ???SQLite 撽??`broker_reconciliation_runs=1`?execution_plans>=1`?execution_outcomes>=1`?execution_legs>=2`嚗???entry ?? simulated `orders>=2`?fills>=2`嚗? order id 雿輻 `DRYRUN-*`嚗瘝? exit嚗trades=0` ?舀迤撣貊???
live-dry-run ?券皜祈岫鋆撥嚗?
```text
?啣? deterministic tests:
- live-dry-run resume 敺??? warmup / bar / execution plan
- EXIT_PENDING seed state ?Ｙ? exit execution plan 銝行芋?祆?鈭?- expiry buffer force-exit ?Ｙ? rollover exit execution plan 銝行芋?祆?鈭?
?游? real smoke:
- 雿輻 data/live_dry_run_full_smoke.sqlite3
- full smoke 摰?敺?銝 store ?? resume 70 iterations
- 撽? warmup_bars 蝬剜? `config.live.warmup_minutes`?ive_runs=2?ars timestamp ?⊿?銴xecution plan ?⊿?銴?```

PowerShell 鈭?撘???撘瘀?

```text
?啣? scripts/lux.ps1嚗摰蝙??Quant ?啣?銝阡? conda run --no-capture-output ?? lux_trader嚗??live terminal UI 鋡?conda run capture??```

?券皜祈岫?誘嚗?
```powershell
Set-Location 'D:\Users\Work place\Project Lux'
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q

$env:LUX_LIVE_MARKETDATA='1'
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config configs/config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader dry-run-doctor --config configs/config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config configs/config.live.smoke.local.toml --readonly
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_dry_run_smoke.py -q -m "live_marketdata and readonly_broker and dry_run_smoke"
Remove-Item Env:\LUX_LIVE_MARKETDATA
Remove-Item Env:\LUX_READONLY_BROKER
```

?剜???soak ???誘嚗?
```powershell
$env:LUX_LIVE_MARKETDATA='1'
.\scripts\lux.ps1 live-dry-run --config configs/config.live.smoke.local.toml --reset-store --max-iterations 900 --no-color
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

Trading calendar closed_dates 鋆撥嚗?
```text
?啣? config: [trading_calendar] closed_dates = 2026 TAIFEX futures market non-trading weekdays
?祆? smoke config ?郊憛怠 2026 TAIFEX closed_dates 摰皜
?啣? live_session_status(timestamp, closed_dates)
live-paper/live-dry-run ??non-trading session 銝?fetch quote?? finalize BAR??頝???Terminal UI 憿舐內: LIVE non-trading session next=MM/DD HH:MM in=HH:MM:SS
live-doctor 憿舐內 live_session?ext_trading_start?ff_book_timestamp?ff_book_age_sec?ff_book_stale
```

撽蝝??

```text
pytest -q: 119 passed, 7 skipped
live-doctor: live_session=closed, next_trading_start=2026-06-22T08:45:00+08:00
real market data doctor: qff_book_timestamp=2026-06-19T04:59:59.032000+08:00, qff_book_stale=true
live-dry-run --reset-store --max-iterations 30: bars_processed=0, plans_recorded=0
SQLite: warmup_bars=1440, market_ticks=0, bars=0, execution_plans=0, live_runs=1
```

### Phase 5 ?蔭隤踵嚗???live-dry-run lifecycle

撌脣???`live-dry-run` ????simulated execution lifecycle???dry-run 銝??胯ecord intent 敺?`PAUSED`???支?銝?撖?Fubon/Binance 憪?隞亙?嚗???execution ?粥摰鈭斗?蝟餌絞??嚗ntry simulated fill 敺?`OPEN`?xit simulated fill 敺? `FLAT`嚗蒂撖怠 simulated orders/fills?rade?nL ??equity??
?詨???嚗?撱箇??拙?蝟餌絞?遣蝡?璇??execution pipeline嚗?
```text
strategy signal
 -> execution plan builder
 -> safety / validation
 -> execution coordinator
 -> execution adapter
 -> execution outcome
 -> state updater / trade recorder
```

撌桀?芸 adapter嚗?
```text
live-dry-run -> SimulatedExecutionAdapter
live-execute -> FubonExecutionAdapter + BinanceExecutionAdapter
```

摰??批捆嚗?
- ?啣? `ExecutionCoordinator`?ExecutionAdapter` protocol?ExecutionOutcome` / `ExecutionOutcomeStatus`嚗絞銝 plan recording?dapter execution?utcome recording ??failure-to-PAUSED policy??- ?啣? `SimulatedExecutionAdapter`嚗??`PairExecutionPlan` 敺??simulated `OrderResult` / `Fill`嚗洵銝??券??漱嚗?潔蝙??plan leg price嚗???slippage / depth / partial fill??- ?啣? SQLite `execution_outcomes`嚗?瘥活 dry-run execution outcome ??execution plan ?嚗? `execution-summary` ??敺?audit 雿輻??- 敺?`PairStrategy` ?賢 `apply_entry_execution(...)` ??`apply_exit_execution(...)`嚗live-paper` ? PaperBroker 銵銝?嚗live-dry-run` ?靘?`live-execute` ?臬?典?銝憟?state / trade / PnL ?湔?摩??- `ENTRY_PENDING` ?? simulated fill 敺?state 霈?`OPEN`嚗EXIT_PENDING` ??rollover force-exit ?? simulated fill 敺?state ??`FLAT` 銝血神??trade?nL?quity??- live mode ??replay/backtest 隤??嚗eplay 隞???PoC ??銝???漱??嚗live-paper` / `live-dry-run` ??finalized bar ?Ｙ? signal 敺? bar 蝡?瑁?嚗?潸票餈?撖虫漱?頂蝯晞?- rejected?ailed?artial?nknown execution outcome 隞???recommended `PAUSED`嚗??芸?鋆???芸??岫??- ?祕 market data smoke 銝?閬? `orders/fills/trades=0`嚗撽瘝??祕 broker order API?imulated order id 雿輻 `DRYRUN-*`嚗? successful entry ?? state ??`OPEN`??
撽蝝??

```text
pytest tests/test_live_market_data.py tests/test_strategy_store.py tests/test_replay_integration.py -q: 52 passed
pytest -q: 120 passed, 7 skipped
```

?桀? `PAUSED` ???歇隤踵?綽?execution rejected?dapter failed?artial fill?nknown order status?econciliation mismatch???嗡?銝摰蝜潛??撣貊??迤撣?dry-run entry/exit 銝???`PAUSED`??
### Phase 5 ??撌乩?

Phase 5 ?格??舀??蔭隤踵摰?敺??梁 execution pipeline ?交??祕???蝵株矽?游歇蝬???shared runtime?ExecutionCoordinator`?ExecutionAdapter` protocol?ExecutionOutcome`?execution_outcomes`?trategy state applier嚗誑??`live-dry-run` same-bar simulated lifecycle??甇?Phase 5 銝?撱箇??虫?憟?execution 蝟餌絞嚗撠釣??real adapter?ive order gate??撖行?鈭文??梯? post-trade reconciliation??
蝚砌???芸? `live-execute` loop嚗??亙 finalized minute ?Ｙ? entry/exit plan 敺??? safety gate ??祕?憪??洵銝??order policy ?∪??孵?????臬?撽??撠祕?桅??堆?銝蕭瘙?雿單?鈭文??
Phase 5 revised commit plan嚗?
- Commit 1嚗歇摰? Live execution gate??銝剜炎??`allow_live_order=true`?[live_execution] enabled=true`?PROJECT_LUX_ALLOW_LIVE_ORDER=1`?FUBON_ALLOW_LIVE_ORDER=1`?BINANCE_ALLOW_LIVE_ORDER=1`?ead-only reconciliation matched????unexpected position/open order?lan freshness ????? plan ?芸銵???- Commit 2嚗歇摰? Execution price / order policy?ive plan ????signal ?嗡???tradable bid/ask?xpected execution price?rder type ??plan age嚗洵銝? market order policy嚗udit 靽? trigger bid/ask?xpected price?ctual fill price??- Commit 3嚗ubon QFF execution adapter? `FutOptOrder` + `sdk.futopt.place_order(...)` ??QFF market order嚗敺憪?/?漱?嚗????柴?亦??? failed/unknown outcome 銝血遣霅?`PAUSED`??- Commit 4嚗inance TSM execution adapter?? `.env` 霈 `BINANCE_API_KEY` / `BINANCE_SECRET`嚗 ccxt USDM private API ??`TSM/USDT:USDT` market order嚗敺 order status?ills?osition嚗?澆???read-only broker ??execution adapter 甈?隤???- Commit 5嚗eal execution coordinator policy?憓?live 撠? coordinator嚗??輸?摨洵銝?摰 `QFF first, Binance second`?FF 憭望?銝?漱????Binance嚗 QFF ??雿?Binance 憭望???隞颱???partial/unknown ??銝像銵?exposure嚗??唾???`exposure_breach` / `single_leg_exposure` ??`imbalanced_pair_exposure`嚗?閰血?撌脫?鈭方??emergency close嚗?敺?敺雁??`PAUSED` 蝑犖撌亦Ⅱ隤??抵??full fill ???strategy state ??`OPEN` ??`FLAT`??- Commit 6嚗歇摰? Post-trade reconciliation??甈?real execution 敺??餉? read-only reconciliation嚗tore state?roker position?pen orders?ecorded fills 敹?銝?湛?隞颱? mismatch ??`PAUSED`??- Commit 7嚗歇摰? `live-execute` integration?窒??`live-paper` / `live-dry-run` ?梁??auto warmup?uote polling?inute finalize?id/ask tradable spread decision?alendar ??contract policy嚗??adapter ?? real execution adapters??- Commit 8嚗eal smoke / minimal live acceptance??閮?pytest ?芾? simulated/fake execution嚗?撖阡 smoke 敹??Ⅱ閮剖? `LUX_LIVE_MARKETDATA=1`?LUX_READONLY_BROKER=1`?PROJECT_LUX_ALLOW_LIVE_ORDER=1`?FUBON_ALLOW_LIVE_ORDER=1`?BINANCE_ALLOW_LIVE_ORDER=1`嚗蒂雿輻璆萄? sizing ????smoke config嚗??嗅?迂銝蝯?entry/exit??
Phase 5 extension point 蝝??

- Commit A嚗歇摰? `LiveRuntime` + `LiveModeHandler`嚗? `live-paper`?live-dry-run`?靘?`live-execute` ?梁??璇?live market data loop??- Commit B嚗歇摰? `live-dry-run` ?寧 shared runtime嚗ry-run 撠惇?摩?葉??`DryRunLiveModeHandler`??- Commit C嚗歇摰? `ExecutionStore` ??CLI helpers cleanup嚗xecution tables ????fake/read-only helper 撌脣?憭批? `SQLiteStore` / `cli.py` ???- Commit D嚗歇摰? Phase 5 extension point嚗憓?`[live_execution]` config?live-order-doctor`?live-execute` CLI ??`LiveExecuteModeHandler`嚗ommit 7 敺?`live-execute` 撌脫銝?shared live runtime ??real execution coordinator嚗? Fubon ?祕 TMF smoke 撠摰?嚗?銝?閬甇???臬祕?柴?
Phase 5 Commit 5 real execution coordinator policy 蝝??

?啣? module: `lux_trader/real_execution.py`

- `RealExecutionCoordinator` ??record live execution plan?? `qff_first=true` ??Fubon leg嚗???Binance leg??- ? full fill ????`filled`嚗蒂?迂 strategy ?梁 applier ?湔 `OPEN` / `FLAT`?rade?nL??- QFF ?漱雿?Binance 憭望??FF partial?inance partial 蝑?撟唾﹛??????exposure breach event嚗遣蝡?reverse emergency close plan ?岫?◢?芥?- Emergency close ??隞??芸??Ｗ儔鈭斗?嚗仃???芰????`critical_manual_intervention_required`嚗?蝯?recommended state ?賣 `PAUSED`??- Fubon adapter ??撖?TMF smoke 隞?pending嚗ommit 5 ?芷? fake adapter deterministic tests嚗??瑁??祕? smoke??
Phase 5 Commit 6 post-trade reconciliation 蝝??

?啣? module: `lux_trader/post_trade_reconciliation.py`

- `LiveExecuteModeHandler` ?冽?甈?real execution outcome 撖怠 `orders` / `fills` / `trades` 敺?蝡??read-only Fubon/Binance brokers 頝?post-trade reconciliation??- Post-trade reconciliation ?蔥?拚?瑼Ｘ嚗?  - read-only broker snapshot 敹???strategy runtime exposure 銝?湛?銝?敺? unexpected open orders??  - SQLite `fills` 銵函敞蝛??signed net exposure 敹???strategy runtime exposure 銝?湛??踹??芣??state 雿?閮?撖行?鈭扎?- 隞颱? `warning` / `error` ?賣?撖怠 `broker_reconciliation_runs` / `broker_reconciliation_issues`嚗???`post_trade_reconciliation_mismatch` event嚗蒂??strategy state 閮剔 `PAUSED`??- `PAUSED` 銝?隞?” exposure 銝摰飛?塚???state 隞???`position_direction` / `tsm_units` / `qff_contracts`嚗econciler ?匱蝥閰?exposure 雿 expected broker state??- Matched case ????`post_trade_reconciliation_matched` event嚗ismatch case ? terminal UI 頛詨 `WARN post_trade_reconciliation ...`??- ?桀??? fake read-only broker deterministic tests嚗?撖?Fubon TMF smoke ????`live-execute` 撖血 smoke 隞?雿??Ⅱ隤??銵?
Phase 5 Commit 7 live-execute integration 蝝??

- `LiveExecuteRunner` ?湔雿輻 shared `LiveRuntime`嚗? `live-paper` / `live-dry-run` ?梁 auto warmup?uote polling?inute finalize?on-trading calendar?id/ask tradable spread decision?ontract switch ??force-exit policy??- `LiveExecuteModeHandler` ?芣??execution layer嚗ignal ?Ｙ?敺遣蝡?live market pair plan嚗漱蝯?`RealExecutionCoordinator` ??real Fubon/Binance execution adapters??- `live-execute` startup gate 銝?閬???摮 execution plan嚗lan freshness / not-executed checks 靽??典蝑 gate 隤?銝哨???coordinator ??execution ?嗡? record outcome??- `live-order-doctor` ?曉? startup gate嚗onfig/env gate?FF-first policy ??latest read-only reconciliation嚗???瘝???plan ??fail??- ?啣? fake provider integration test嚗?蝛?store ?? `live-execute`嚗Ⅱ隤?auto warmup?arket ticks?AR finalize?ive execution plan?rders/fills?ost-trade reconciliation ??`OPEN` state ?冽?蝔頝?- ?祕 Fubon TMF smoke ????`live-execute` live-order acceptance 撠?瑁?嚗?銝?閬?舐鈭箏澆?撖血??
Phase 5 Commit 1 live execution gate 蝝??

```text
?啣? module: lux_trader/live_execution_gate.py
?啣? gate checks:
- safety_allow_live_order
- live_execution_enabled
- execution_order_qff_first
- env_PROJECT_LUX_ALLOW_LIVE_ORDER
- env_FUBON_ALLOW_LIVE_ORDER
- env_BINANCE_ALLOW_LIVE_ORDER
- readonly_reconciliation_present
- readonly_reconciliation_matched
- no_unexpected_positions
- no_unexpected_open_orders
- execution_plan_present
- execution_plan_fresh
- execution_plan_not_executed

live-order-doctor: 雿輻??憟?gate report嚗???PASS/FAIL
live-execute: gate ?芷???fail fast嚗ate ?券?敺??脣 shared live runtime嚗?敺?finalized BAR ?Ｙ? real execution plan
?賊? regression: `tests/test_live_execution_gate.py`?tests/test_execution_recorder_cli.py`?tests/test_live_market_data.py` 撌脤?
```

Phase 5 Commit 2 execution price / order policy 蝝??

```text
?啣? module: lux_trader/execution_price_policy.py
?啣? price policy: live_touch_market
?啣? execution plan / leg audit 甈?:
- order_type = market
- price_policy
- plan_age_seconds
- max_plan_age_seconds
- expected_price
- trigger_bid
- trigger_ask
- trigger_mid
- price_source

live-dry-run execution plan:
- BUY leg expected_price 雿輻 ask
- SELL leg expected_price 雿輻 bid
- Binance TSM leg 雿輻 TSM/USDT book ??USDT/TWD book ?? TWD fair price
- Fubon QFF leg 雿輻 QFF top-of-book
- dry-run fills.price 雿輻 expected_price嚗???simulated actual fill price
- ? bar/trade accounting ?急?蝬剜? bar-based嚗?蝥閬??典???fill-based PnL ?蝡矽??
pytest tests/test_execution_price_policy.py tests/test_execution_intent.py tests/test_execution_recorder_cli.py tests/test_live_market_data.py -q: 77 passed
```

Phase 5 撽??嚗?
- 蝻箔遙銝 config/env safety gate ??蝯??- broker reconciliation mismatch ??蝯??- live execution plan 敹?靽? trigger bid/ask?xpected price?ctual fill price ??plan age??- QFF first ???inance second ????execution audit tables ??strategy state ?湔甇?Ⅱ??- QFF 憭望???Binance adapter 銝?鋡怠?怒?- Binance 憭望??artial fill?nknown order status ??敺?`PAUSED`嚗??芸?鋆???芸??岫??- real execution 敺?post-trade reconciliation 敹? matched嚗??`PAUSED`??- resume 敺?敺?銴撌脣銵???execution plan??- `live-paper`?live-dry-run` 靽?嚗??乩???paper trading ?祕?桀???撌亙??
## 8. Safety ??

- Phase 2 ??Phase 4 ?賭?敺?撖血?閮?- Phase 5 ?身隞?敺?撖血?閮??芣? explicit config + env gate ?券?????迂?撠祕?柴?- 隞亙祕?瘚?頝撽?箸?
- 隞颱? live test 敹??Ⅱ閮剖? `LUX_LIVE_MARKETDATA=1`??- `allow_live_order=true` ??Phase 1 ??Phase 4 敹?鋡急?蝯?Phase 5 ?芾??`live-execute` safety gate ?亙???- `live-execute` ?芾??Phase 5 explicit config/env gate ?券?敺????祕 smoke 撠摰???銝??∩犖?澆?????- 隞颱??踹仃?artial fill?nknown status?ost-trade reconciliation mismatch嚗敹???`PAUSED`嚗?敺???格??岫??- `.env`?.pfx`?ocal smoke config?QLite?AIFEX cache?untime logs ?賭?敺?git??
## 9. Architecture Refactor

2026-06-25 architecture refactor progress:

- Commit 1: Config relocation completed.
  - Created `configs/`.
  - Moved tracked example configs to `configs/replay.example.toml` and
    `configs/live.example.toml`.
  - Moved ignored local configs under `configs/`.
  - Project-local TOML relative paths still resolve from the Project Lux root, so
    `.env`, `data/`, TAIFEX cache, and SQLite paths keep the same deployment meaning.

- Commit 2: Core domain extraction completed.
  - Moved models, strategy, indicator, calendar, sizing, fees, tradable spread, and
    contract policy into `lux_trader/core/`.
  - Added shared Taipei time and contract parsing helpers.
  - Added architecture tests to keep core independent from CLI, runtime, SQLite, and
    external API adapters.

- Commit 3: Market data and integrations completed.
  - Split provider-neutral market data code into `lux_trader/market_data/`.
  - Moved Fubon, Binance, BitoPro, and TAIFEX implementations into
    `lux_trader/integrations/`.
  - Consolidated Fubon auth, response parsing, and contract identity handling.

- Commit 4: Execution, reconciliation, and persistence completed.
  - Moved execution intent, outcome, price policy, simulation, recorder, real
    coordinator, and live execution gate into `lux_trader/execution/`.
  - Split reconciliation into `models.py`, `brokers.py`, `reconciler.py`, and
    `post_trade.py` under `lux_trader/reconciliation/`.
  - Moved SQLite DDL into `lux_trader/persistence/schema.py`.
  - Moved execution SQL helpers into `lux_trader/persistence/execution_queries.py`.
  - Moved reconciliation SQL helpers into
    `lux_trader/persistence/reconciliation_queries.py`.
  - Kept `SQLiteStore` as the single public facade.
  - Kept SQLite schema and persisted JSON payload formats unchanged.
  - Kept compatibility wrappers for previous import paths while internal code moves
    toward the new package layout.

- Commit 5: Live runtime split completed.
  - Added `lux_trader/runtime/live/bootstrap.py` for provider initialization,
    startup preflight, quote fetch caching, and runtime context preparation.
  - Added `lux_trader/runtime/live/warmup.py` for `warmup-live`, QFF warmup check,
    and auto-warmup indicator seeding.
  - Added `lux_trader/runtime/live/contracts.py` for active contract resolution,
    QFF books subscription lifecycle, contract switch state, and force-exit checks.
  - Added `lux_trader/runtime/live/modes.py` for paper, dry-run, and live-execute
    mode handlers plus execution helpers.
  - Added `lux_trader/runtime/live/engine.py` for the shared polling and minute
    finalize loop.
  - Kept `lux_trader/live_runner.py` as a compatibility re-export module.
  - `live-paper`, `live-dry-run`, and `live-execute` continue to use the same live
    runtime engine.

Remaining architecture refactor work:

- Commit 6: split CLI parser, dispatch, and command implementations out of `cli.py`.
- Commit 7: clean up test grouping and documentation after runtime/CLI split.

Validation target remains:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader --help
```
