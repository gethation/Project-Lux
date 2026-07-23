from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


READONLY_BROKER_ENV = "LUX_READONLY_BROKER"


def readonly_broker_enabled() -> bool:
    return os.getenv(READONLY_BROKER_ENV, "").strip() == "1"


def require_readonly_broker_env() -> None:
    """Integrations-layer twin of the CLI gate, for adapters that reach a broker
    account directly. Kept here so integrations never import from cli."""
    if not readonly_broker_enabled():
        raise RuntimeError(
            f"Set {READONLY_BROKER_ENV}=1 to query real broker accounts"
        )


def resolve_cert_path(env_path: Path | None) -> Path:
    value = os.getenv("FUBON_CERT_PATH", "").strip()
    root = env_path.parent if env_path is not None else Path.cwd()
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        return path.resolve()
    candidates = sorted(root.glob("*.pfx"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    raise RuntimeError("Set FUBON_CERT_PATH or place exactly one .pfx next to .env")

