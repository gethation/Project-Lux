from __future__ import annotations

import os
import sys
from typing import Any, Callable, TextIO

from .core.time import ensure_taipei
from .core.tradable_spread import TradableSpreadSnapshot
from .trade_pnl import format_trade_pnl_values, format_twd, trade_pnl_from_execution


ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "bright_white": "\033[97m",
}
ANSI_ERASE_LINE = "\033[2K"


class NullLiveReporter:
    def live(
        self,
        timestamp: Any,
        spread_snapshot: TradableSpreadSnapshot,
        strategy_state: Any,
    ) -> None:
        return

    def live_non_trading(
        self,
        timestamp: Any,
        next_open_at: Any,
        reason: str,
    ) -> None:
        return

    def bar(
        self,
        timestamp: Any,
        spread_snapshot: TradableSpreadSnapshot,
        strategy_state: Any,
        action: Any,
        reason: str,
        unrealized_pnl: float,
        equity: float,
        account_display: Any = None,
    ) -> None:
        return

    def warn(self, timestamp: Any, code: str, detail: str = "") -> None:
        return

    def event(self, timestamp: Any, code: str, detail: str = "") -> None:
        return

    def error(self, timestamp: Any, message: str) -> None:
        return

    def execution(
        self,
        timestamp: Any,
        plan: Any,
        outcome: Any,
        result: Any,
    ) -> None:
        return

    def finish(self) -> None:
        return


class LiveTerminalReporter:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        color: bool | None = None,
        interactive: bool | None = None,
        terminal_width: Callable[[], int | None] | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        if color is None:
            color = is_tty and os.getenv("NO_COLOR") is None
        self.color = color
        self.interactive = is_tty if interactive is None else bool(interactive)
        self._terminal_width = terminal_width or self._detect_terminal_width
        self._live_active = False

    def live(
        self,
        timestamp: Any,
        spread_snapshot: TradableSpreadSnapshot,
        strategy_state: Any,
    ) -> None:
        time_text = format_time(timestamp, with_seconds=True)
        mid_text = f"mid={format_float(spread_snapshot.mid_spread, digits=2)}"
        state_text = state_value(strategy_state)
        short_text = format_spread_block(
            "shortSpread",
            spread_snapshot.short_spread,
            spread_snapshot.short_zscore,
        )
        long_text = format_spread_block(
            "longSpread",
            spread_snapshot.long_spread,
            spread_snapshot.long_zscore,
        )
        plain = f"{time_text} LIVE {mid_text} {short_text} {long_text} {state_text}"
        colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("LIVE", "cyan", "dim"),
                mid_text,
                self._paint_spread_block("shortSpread", spread_snapshot.short_spread, spread_snapshot.short_zscore),
                self._paint_spread_block("longSpread", spread_snapshot.long_spread, spread_snapshot.long_zscore),
                self._paint_state(state_text),
            ]
        )
        compact_plain = (
            f"{time_text} LIVE {mid_text} "
            f"zS={format_float(spread_snapshot.short_zscore, digits=2)} "
            f"zL={format_float(spread_snapshot.long_zscore, digits=2)} {state_text}"
        )
        compact_colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("LIVE", "cyan", "dim"),
                mid_text,
                self._paint_z(
                    f"zS={format_float(spread_snapshot.short_zscore, digits=2)}",
                    spread_snapshot.short_zscore,
                ),
                self._paint_z(
                    f"zL={format_float(spread_snapshot.long_zscore, digits=2)}",
                    spread_snapshot.long_zscore,
                ),
                self._paint_state(state_text),
            ]
        )
        minimal_plain = f"{time_text} LIVE {mid_text} {state_text}"
        minimal_colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("LIVE", "cyan", "dim"),
                mid_text,
                self._paint_state(state_text),
            ]
        )
        self._write_live(
            (
                (plain, colored),
                (compact_plain, compact_colored),
                (minimal_plain, minimal_colored),
            )
        )

    def live_non_trading(
        self,
        timestamp: Any,
        next_open_at: Any,
        reason: str,
    ) -> None:
        time_text = format_time(timestamp, with_seconds=True)
        next_text = format_next_open(next_open_at)
        countdown = format_countdown(timestamp, next_open_at)
        plain = (
            f"{time_text} LIVE non-trading session "
            f"next={next_text} in={countdown}"
        )
        colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("LIVE non-trading session", "yellow"),
                f"next={next_text}",
                f"in={countdown}",
            ]
        )
        compact_plain = f"{time_text} CLOSED next={next_text} in={countdown}"
        compact_colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("CLOSED", "yellow"),
                f"next={next_text}",
                f"in={countdown}",
            ]
        )
        minimal_plain = f"{time_text} CLOSED in={countdown}"
        minimal_colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("CLOSED", "yellow"),
                f"in={countdown}",
            ]
        )
        self._write_live(
            (
                (plain, colored),
                (compact_plain, compact_colored),
                (minimal_plain, minimal_colored),
            )
        )

    def bar(
        self,
        timestamp: Any,
        spread_snapshot: TradableSpreadSnapshot,
        strategy_state: Any,
        action: Any,
        reason: str,
        unrealized_pnl: float,
        equity: float,
        account_display: Any = None,
    ) -> None:
        time_text = format_time(timestamp, with_seconds=False)
        mid_text = f"mid={format_float(spread_snapshot.mid_spread, digits=2)}"
        mid_z_text = f"z={format_float(spread_snapshot.mid_zscore, digits=2)}"
        state_text = state_value(strategy_state)
        action_text = compact_action(action, reason)
        pnl_text = f"pnl={account_pnl_text(account_display)}"
        pnl_status_text = (
            " realized_pnl=PENDING"
            if getattr(strategy_state, "pnl_status", "complete") != "complete"
            else ""
        )
        margin_text = account_margin_text(account_display)
        short_text = format_spread_block(
            "shortSpread",
            spread_snapshot.short_spread,
            spread_snapshot.short_zscore,
        )
        long_text = format_spread_block(
            "longSpread",
            spread_snapshot.long_spread,
            spread_snapshot.long_zscore,
        )
        plain = (
            f"{time_text} BAR  {mid_text} {mid_z_text} {short_text} {long_text} "
            f"{state_text} {action_text} {pnl_text}{pnl_status_text} {margin_text}"
        )
        colored = " ".join(
            [
                self._paint(time_text, "dim"),
                self._paint("BAR ", "bright_white"),
                mid_text,
                self._paint_z(mid_z_text, spread_snapshot.mid_zscore),
                self._paint_spread_block("shortSpread", spread_snapshot.short_spread, spread_snapshot.short_zscore),
                self._paint_spread_block("longSpread", spread_snapshot.long_spread, spread_snapshot.long_zscore),
                self._paint_state(state_text),
                self._paint_action(action_text, action),
                pnl_text,
                *([pnl_status_text.strip()] if pnl_status_text else []),
                margin_text,
            ]
        )
        self._write_permanent(plain, colored)

    def warn(self, timestamp: Any, code: str, detail: str = "") -> None:
        self._write_short(timestamp, "WARN", code, detail, "yellow")

    def event(self, timestamp: Any, code: str, detail: str = "") -> None:
        self._write_short(timestamp, "EVENT", code, detail, "magenta")

    def error(self, timestamp: Any, message: str) -> None:
        self._write_short(timestamp, "ERR", message, "", "red")

    def execution(
        self,
        timestamp: Any,
        plan: Any,
        outcome: Any,
        result: Any,
    ) -> None:
        summary = trade_pnl_from_execution(plan, outcome, result)
        if summary is None:
            return
        time_text = format_time(timestamp, with_seconds=True)
        values = format_trade_pnl_values(summary)
        if values is None:
            plain = f"{time_text} TRADE EXIT trade_pnl_twd unavailable"
            colored = " ".join(
                (
                    self._paint(time_text, "dim"),
                    self._paint("TRADE EXIT", "magenta"),
                    self._paint("trade_pnl_twd unavailable", "yellow"),
                )
            )
        else:
            net_text = f"net={format_twd(summary.net_pnl_twd)}"
            gross_and_fees = values.split(" ", 1)[1]
            net_style = "green" if float(summary.net_pnl_twd or 0.0) >= 0 else "red"
            plain = f"{time_text} TRADE EXIT {values} TWD"
            colored = " ".join(
                (
                    self._paint(time_text, "dim"),
                    self._paint("TRADE EXIT", "magenta"),
                    self._paint(net_text, net_style),
                    f"{gross_and_fees} TWD",
                )
            )
        self._write_permanent(plain, colored)

    def finish(self) -> None:
        self._clear_live_line()

    def _write_short(
        self,
        timestamp: Any,
        label: str,
        code: str,
        detail: str,
        color_name: str,
    ) -> None:
        time_text = format_time(timestamp, with_seconds=True)
        detail_suffix = f" {detail}" if detail else ""
        plain = f"{time_text} {label} {code}{detail_suffix}"
        colored = (
            f"{self._paint(time_text, 'dim')} "
            f"{self._paint(label, color_name)} {code}{detail_suffix}"
        )
        self._write_permanent(plain, colored)

    def _write_live(self, variants: tuple[tuple[str, str], ...]) -> None:
        plain, colored = variants[0]
        if not self.interactive:
            self.stream.write(f"{colored}\n")
            self.stream.flush()
            return

        width = self._current_terminal_width()
        if width is None:
            # A TTY whose width cannot be determined cannot safely use carriage-
            # return refreshes: the rendered text may wrap and leave stale rows.
            self.stream.write(f"{colored}\n")
            self.stream.flush()
            return

        # Keep one column unused. Writing into the final terminal column can set
        # the pending-wrap flag even when the text visually appears to fit.
        available = max(width - 1, 1)
        for candidate_plain, candidate_colored in variants:
            if len(candidate_plain) <= available:
                plain, colored = candidate_plain, candidate_colored
                break
        else:
            plain = truncate_line(variants[-1][0], available)
            colored = plain

        self.stream.write(f"\r{ANSI_ERASE_LINE}{colored}")
        self.stream.flush()
        self._live_active = True

    def _write_permanent(self, plain: str, colored: str) -> None:
        self._clear_live_line()
        self.stream.write(f"{colored}\n")
        self.stream.flush()

    def _clear_live_line(self) -> None:
        if not self._live_active:
            return
        self.stream.write(f"\r{ANSI_ERASE_LINE}")
        self.stream.flush()
        self._live_active = False

    def _current_terminal_width(self) -> int | None:
        try:
            width = self._terminal_width()
        except (OSError, ValueError, TypeError):
            return None
        if width is None:
            return None
        parsed = int(width)
        return parsed if parsed > 0 else None

    def _detect_terminal_width(self) -> int | None:
        try:
            fileno = self.stream.fileno()
            return os.get_terminal_size(fileno).columns
        except (AttributeError, OSError, ValueError):
            return None

    def _paint(self, text: str, *styles: str) -> str:
        if not self.color:
            return text
        prefix = "".join(ANSI[style] for style in styles)
        return f"{prefix}{text}{ANSI['reset']}"

    def _paint_z(self, text: str, zscore: float | None) -> str:
        if zscore is None:
            return self._paint(text, "dim")
        magnitude = abs(zscore)
        if magnitude < 1:
            return self._paint(text, "green")
        if magnitude < 2:
            return self._paint(text, "yellow")
        return self._paint(text, "red")

    def _paint_state(self, text: str) -> str:
        if text == "FLAT":
            return self._paint(text, "green")
        if text in {"ENTRY_PENDING", "EXIT_PENDING"}:
            return self._paint(text, "yellow")
        if text in {"OPEN", "LONG", "SHORT"}:
            return self._paint(text, "cyan")
        if text in {"PAUSED", "ERROR"}:
            return self._paint(text, "red")
        return text

    def _paint_action(self, text: str, action: Any) -> str:
        action_text = action_value(action)
        if action_text == "none":
            return self._paint(text, "dim")
        if action_text in {"entry_signal", "exit_signal"}:
            return self._paint(text, "yellow")
        if action_text in {"entry_fill", "exit_fill", "dry_run_intent"}:
            return self._paint(text, "green")
        if action_text == "live_execution":
            return self._paint(text, "yellow")
        if action_text in {"entry_cancel", "rollover_force_exit", "force_close"}:
            return self._paint(text, "yellow")
        if action_text == "error":
            return self._paint(text, "red")
        return text

    def _paint_spread_block(
        self,
        name: str,
        spread: float | None,
        zscore: float | None,
    ) -> str:
        if not self.color:
            return format_spread_block(name, spread, zscore)
        spread_text = format_float(spread, digits=2)
        z_text = f"z={format_float(zscore, digits=2)}"
        return f"{name}(spread={spread_text},{self._paint_z(z_text, zscore)})"


def compact_warning_code(kind: str | None, payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    if kind == "market_data_stale":
        source = str(payload.get("source") or "data")
        return f"stale_{source}"
    if kind == "leg_timestamp_skew":
        return "skew"
    if kind == "missing_required_quote":
        return "missing_quote"
    if kind == "missing_tw_leg_forward_fill":
        return "missing_tw_leg"
    return str(kind or "warning")


def compact_reason(reason: str) -> str:
    mapping = {
        "entry_zscore_crossed": "zscore_crossed",
        "exit_zscore_crossed": "zscore_crossed",
        "entry_delay_exceeded": "delay_exceeded",
        "rollover_force_exit": "expiry_buffer",
        "live minute skipped": "skipped_minute",
        "dry_run_entry_intent_recorded": "entry_intent_recorded",
        "dry_run_entry_intent_rejected": "entry_intent_rejected",
        "dry_run_exit_intent_recorded": "exit_intent_recorded",
        "dry_run_exit_intent_rejected": "exit_intent_rejected",
    }
    return mapping.get(reason, reason)


def compact_action(action: Any, reason: str) -> str:
    action_text = action_value(action)
    suffix = compact_reason(reason)
    if action_text == "none":
        return action_text
    if suffix in {action_text, reason, f"{action_text}ed"}:
        return action_text
    if reason in {"entry_filled", "exit_filled"}:
        return action_text
    return f"{action_text}/{suffix}"


def format_time(timestamp: Any, *, with_seconds: bool) -> str:
    fmt = "%H:%M:%S" if with_seconds else "%H:%M"
    return ensure_taipei(timestamp).strftime(fmt)


def format_next_open(timestamp: Any) -> str:
    return ensure_taipei(timestamp).strftime("%m/%d %H:%M")


def format_countdown(timestamp: Any, next_open_at: Any) -> str:
    seconds = max(
        int(
            (
                ensure_taipei(next_open_at) - ensure_taipei(timestamp)
            ).total_seconds()
        ),
        0,
    )
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_float(value: float | None, *, digits: int) -> str:
    if value is None:
        return "NA"
    return f"{value:.{digits}f}"


def truncate_line(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return f"{value[: width - 3]}..."


def format_spread_block(
    name: str,
    spread: float | None,
    zscore: float | None,
) -> str:
    return (
        f"{name}(spread={format_float(spread, digits=2)},"
        f"z={format_float(zscore, digits=2)})"
    )


def format_money(value: float) -> str:
    return f"{value:,.0f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value * 100:.0f}%"


def account_pnl_text(account_display: Any) -> str:
    """pnl= token sourced from the real account (combined uPnL); NA when absent."""
    combined = getattr(account_display, "combined_upnl_twd", None)
    if combined is None:
        return "NA"
    return format_money(combined)


def account_margin_text(account_display: Any) -> str:
    """Replaces the old eq= token: per-venue 保證金水位 (equity/notional ratio)."""
    binance_ratio = getattr(account_display, "binance_ratio", None)
    fubon_ratio = getattr(account_display, "fubon_ratio", None)
    prefix = "~" if getattr(account_display, "stale", False) else ""
    text = (
        f"margin({prefix}bina={format_pct(binance_ratio)},"
        f"fubon={format_pct(fubon_ratio)})"
    )
    error_reason = getattr(account_display, "error_reason", None)
    if error_reason:
        return f"{text} account_error={error_reason}"
    return text


def state_value(value: Any) -> str:
    # Accepts a StrategyState enum, a full StrategyRuntimeState, or plain text.
    runtime_state = value
    state = getattr(runtime_state, "state", runtime_state)
    text = str(getattr(state, "value", state)).upper()
    if text != "OPEN":
        return text

    direction = getattr(runtime_state, "position_direction", None)
    direction_text = str(getattr(direction, "value", direction) or "").lower()
    if direction_text == "long_us_short_tw":
        return "LONG"
    if direction_text == "short_us_long_tw":
        return "SHORT"
    return text


def action_value(value: Any) -> str:
    return str(getattr(value, "value", value))
