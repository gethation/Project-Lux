from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_TOP_LEVEL_MODULES = {
    "binance_execution",
    "brokers",
    "cli",
    "cli_helpers",
    "execution",
    "execution_intent",
    "execution_recorder",
    "execution_simulator",
    "execution_store",
    "fubon_execution",
    "integrations",
    "live_execution_gate",
    "live_market_data",
    "live_runner",
    "market_data",
    "post_trade_reconciliation",
    "readonly_brokers",
    "real_execution",
    "reconciliation",
    "runner",
    "store",
    "terminal_ui",
}
FORBIDDEN_EXTERNAL_MODULES = {"ccxt", "fubon_neo", "sqlite3"}


def test_core_does_not_depend_on_runtime_persistence_or_adapters() -> None:
    core_dir = Path(__file__).resolve().parents[1] / "lux_trader" / "core"
    violations: list[str] = []

    for path in sorted(core_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            external_module = imported_external_module(node)
            if external_module in FORBIDDEN_EXTERNAL_MODULES:
                violations.append(
                    f"{path.name}:{node.lineno} imports {external_module}"
                )
            module = imported_project_module(node)
            if module is None:
                continue
            top_level = module.split(".", 1)[0]
            if top_level in FORBIDDEN_TOP_LEVEL_MODULES:
                violations.append(f"{path.name}:{node.lineno} imports {module}")

    assert violations == []


def test_market_data_services_do_not_import_external_adapters() -> None:
    market_data_dir = (
        Path(__file__).resolve().parents[1]
        / "lux_trader"
        / "market_data"
    )
    violations: list[str] = []

    for path in sorted(market_data_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            external_module = imported_external_module(node)
            if external_module in {"ccxt", "fubon_neo"}:
                violations.append(
                    f"{path.name}:{node.lineno} imports {external_module}"
                )
            module = imported_project_module(node)
            if module and module.split(".", 1)[0] == "integrations":
                violations.append(f"{path.name}:{node.lineno} imports {module}")

    assert violations == []


def test_fubon_raw_parser_has_single_definition() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "lux_trader"
    definitions: list[str] = []

    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "fubon_raw_row":
                definitions.append(path.relative_to(package_dir).as_posix())

    assert definitions == ["integrations/fubon/parsing.py"]


def test_reconciliation_domain_does_not_import_external_adapters() -> None:
    reconciliation_dir = (
        Path(__file__).resolve().parents[1]
        / "lux_trader"
        / "reconciliation"
    )
    violations: list[str] = []

    for path in sorted(reconciliation_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            external_module = imported_external_module(node)
            if external_module in {"ccxt", "fubon_neo", "sqlite3"}:
                violations.append(
                    f"{path.name}:{node.lineno} imports {external_module}"
                )
            module = imported_project_module(node)
            if module and module.split(".", 1)[0] == "integrations":
                violations.append(f"{path.name}:{node.lineno} imports {module}")

    assert violations == []


def test_store_facade_does_not_own_schema_or_split_queries() -> None:
    store_path = Path(__file__).resolve().parents[1] / "lux_trader" / "store.py"
    store_text = store_path.read_text(encoding="utf-8")

    assert "CREATE TABLE" not in store_text
    assert "broker_reconciliation_runs" not in store_text
    assert "execution_plans" not in store_text
    assert "initialize_schema(self.connection)" in store_text
    assert "ExecutionStore(self.connection)" in store_text
    assert "ReconciliationStore(self.connection)" in store_text


def test_live_runner_is_compatibility_reexport_after_runtime_split() -> None:
    package_dir = Path(__file__).resolve().parents[1] / "lux_trader"
    live_runner_path = package_dir / "live_runner.py"
    tree = ast.parse(live_runner_path.read_text(encoding="utf-8"))
    top_level_defs = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    ]

    assert top_level_defs == []
    for name in ("bootstrap.py", "warmup.py", "contracts.py", "modes.py", "engine.py"):
        assert (package_dir / "runtime" / "live" / name).exists()


def imported_project_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        if node.level == 1:
            return node.module
        if node.level == 0 and node.module and node.module.startswith("lux_trader."):
            return node.module.removeprefix("lux_trader.")
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("lux_trader."):
                return alias.name.removeprefix("lux_trader.")
    return None


def imported_external_module(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return node.module.split(".", 1)[0]
    if isinstance(node, ast.Import) and node.names:
        return node.names[0].name.split(".", 1)[0]
    return None
