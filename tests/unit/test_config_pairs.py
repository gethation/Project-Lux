from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.config import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAMES = (
    "config.live.exec.dryrun.local.toml",
    "config.live.exec.local.toml",
    "config.live.smoke.local.toml",
    "live.example.toml",
    "replay.example.toml",
    "replay.fixture.toml",
)


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_every_repository_config_uses_the_single_qff_tsm_pair(name: str) -> None:
    config = load_config(PROJECT_ROOT / "configs" / name)

    assert len(config.pairs) == 1
    assert config.active_pair.id == "qff_tsm"
    assert config.active_pair.tw_leg.display == "QFF"
    assert config.active_pair.us_leg.display == "TSM"
    assert config.active_pair.tw_leg.contract_multiplier == 100.0


def test_replay_fixture_explicitly_preserves_notional_sizing_and_fee() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "replay.fixture.toml")

    assert config.active_pair.sizing.mode == "notional"
    assert config.active_pair.sizing.leg_notional_twd == 1_000_000.0
    assert config.active_pair.fees.tw_leg_fee_per_contract_twd == 5.0


def write_config(tmp_path: Path, sizing_lines: list[str]) -> Path:
    path = tmp_path / "pairs.toml"
    path.write_text(
        "\n".join(
            [
                "[paths]",
                "store_path = 'store.sqlite3'",
                "",
                "[[pairs]]",
                "id = 'configured_pair'",
                "label = 'AAA/BBB'",
                "",
                "[pairs.data]",
                "input_csv = ''",
                "",
                "[pairs.tw_leg]",
                "display = 'AAA'",
                "venue = 'fubon'",
                "product = 'AAA'",
                "symbol = 'auto'",
                "contract_multiplier = 100.0",
                "",
                "[pairs.us_leg]",
                "display = 'BBB'",
                "venue = 'binance'",
                "symbol = 'BBB/USDT:USDT'",
                "adr_share_ratio = 5.0",
                "",
                "[pairs.fx]",
                "venue = 'bitopro'",
                "symbol = 'USDT/TWD'",
                "",
                "[pairs.sizing]",
                *sizing_lines,
                "",
                "[pairs.strategy]",
                "",
                "[pairs.fees]",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_sizing_defaults_to_one_fixed_lot(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, []))

    assert config.active_pair.sizing.mode == "fixed_lots"
    assert config.active_pair.sizing.lots == 1
    assert config.strategy.tw_leg_lots == 1


def test_notional_sizing_requires_leg_notional(tmp_path: Path) -> None:
    with pytest.raises(
        RuntimeError,
        match=r"sizing\.leg_notional_twd is required when mode = 'notional'",
    ):
        load_config(write_config(tmp_path, ["mode = 'notional'"]))
