from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...core.contracts import row_get
from ..env import load_dotenv, require_env, resolve_cert_path


def login_fubon_sdk(
    sdk: Any,
    env_path: Path | None,
    *,
    label: str = "Fubon login",
    api_key_env: str = "FUBON_TRADING_API_KEY",
) -> list[Any]:
    load_dotenv(env_path)
    personal_id = require_env("FUBON_PERSONAL_ID")
    cert_path = resolve_cert_path(env_path)
    cert_password = os.getenv("FUBON_CERT_PASSWORD", "").strip() or None
    api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        api_key = os.getenv("FUBON_API_KEY", "").strip()
    password = os.getenv("FUBON_PASSWORD", "").strip()

    if api_key:
        result = sdk.apikey_login(
            personal_id,
            api_key,
            str(cert_path),
            cert_password,
        )
    elif password:
        if cert_password:
            result = sdk.login(
                personal_id,
                password,
                str(cert_path),
                cert_password,
            )
        else:
            result = sdk.login(personal_id, password, str(cert_path))
    else:
        raise RuntimeError(
            f"Set {api_key_env}, FUBON_API_KEY, or FUBON_PASSWORD for Fubon login"
        )
    return checked_result_data(result, label)


def validate_distinct_live_api_keys(env_path: Path | None) -> None:
    """Require isolated market-data/trading keys when both roles are enabled."""
    load_dotenv(env_path)
    market_data_key = os.getenv("FUBON_MARKETDATA_API_KEY", "").strip()
    trading_key = os.getenv("FUBON_TRADING_API_KEY", "").strip()
    if not market_data_key or not trading_key:
        raise RuntimeError(
            "Set both FUBON_MARKETDATA_API_KEY and FUBON_TRADING_API_KEY "
            "for live-execute"
        )
    if market_data_key == trading_key:
        raise RuntimeError(
            "FUBON_MARKETDATA_API_KEY and FUBON_TRADING_API_KEY must be different"
        )


def checked_result_data(
    result: Any,
    label: str,
    *,
    empty_ok: bool = False,
) -> list[Any]:
    if not bool(getattr(result, "is_success", True)):
        message = str(getattr(result, "message", "") or "")
        if empty_ok and is_empty_result_message(message):
            return []
        raise RuntimeError(f"{label} failed: {message}")
    data = getattr(result, "data", result)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def is_empty_result_message(message: str) -> bool:
    normalized = message.strip().lower()
    return (
        "查無任何資料" in normalized
        or "查無資料" in normalized
        or "no data" in normalized
        or "not found" in normalized
    )


def select_futopt_account(accounts: list[Any]) -> Any:
    if not accounts:
        raise RuntimeError("Fubon login returned no accounts")
    for account in accounts:
        if str(row_get(account, "account_type") or "").lower() == "futopt":
            return account
    raise RuntimeError("Fubon login returned no futopt account")

