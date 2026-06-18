# Project Lux

MVP pairs-trading architecture for replaying the QFF/TSM proof of concept through a
single-process loop, a paper broker, and a SQLite state store.

The first milestone intentionally does not use Fubon or Binance live APIs. It reads
the PoC CSV, recomputes the rolling z-score, runs the strategy state machine, records
paper orders/fills/trades, and supports resume from SQLite.

## Environment

This machine uses Miniconda. Run Python commands through the `Quant` environment:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python --version
```

Install test tooling:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pip install -r requirements-dev.txt
```

## Commands

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader doctor --config config.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader replay --config config.example.toml --reset-store
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader summary --config config.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest
```

## Safety

This milestone has no live trading path. `doctor` fails if live trading is enabled in
the config. Future live order code must require explicit environment and config gates.
