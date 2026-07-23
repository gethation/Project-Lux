"""Rich-based live dashboard reporter.

Implements the same duck-typed reporter protocol as
``terminal_ui.LiveTerminalReporter`` (``live``, ``live_non_trading``, ``bar``,
``warn``, ``event``, ``error``, ``finish``) but renders a multi-panel dashboard:
session, symbols, latest quote/bar, spread/z-score, strategy state, position,
latest decision, and reconciliation/gate status. Recent warnings/events are kept
in an activity log panel.

The engine passes the full ``StrategyRuntimeState`` to ``live``/``bar``; this
module only reads from it (UI layer — never mutates strategy state).
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .core.time import ensure_taipei
from .core.tradable_spread import TradableSpreadSnapshot
from .presentation import direction_text, instrument_text, metric_label
from .terminal_ui import (
    account_pnl_text,
    compact_action,
    format_countdown,
    format_float,
    format_next_open,
    format_pct,
    format_time,
    state_value,
)
from .trade_pnl import format_trade_pnl_values, format_twd, trade_pnl_from_execution


STATE_STYLES = {
    "FLAT": "green",
    "ENTRY_PENDING": "yellow",
    "EXIT_PENDING": "yellow",
    "OPEN": "cyan",
    "LONG": "cyan",
    "SHORT": "cyan",
    "PAUSED": "bold red",
    "ERROR": "bold red",
}


def z_style(zscore: float | None) -> str:
    if zscore is None:
        return "dim"
    magnitude = abs(zscore)
    if magnitude < 1:
        return "green"
    if magnitude < 2:
        return "yellow"
    return "red"


@dataclass
class DashboardState:
    mode: str = ""
    tw_leg_symbol: str | None = None
    tw_leg_expiry: str | None = None
    binance_symbol: str | None = None
    bitopro_symbol: str | None = None
    session: str = "starting"
    next_open_text: str | None = None
    countdown_text: str | None = None
    quote_time: str | None = None
    quote_snapshot: TradableSpreadSnapshot | None = None
    bar_time: str | None = None
    bar_snapshot: TradableSpreadSnapshot | None = None
    bar_pnl: float | None = None
    bar_equity: float | None = None
    bar_account_display: Any = None
    state_text: str = "…"
    position_direction: str | None = None
    us_leg_units: float = 0.0
    tw_leg_contracts: int = 0
    entry_zscore: float | None = None
    decision_text: str | None = None
    decision_time: str | None = None
    reconciliation_text: str | None = None
    reconciliation_time: str | None = None
    gate_text: str | None = None
    margin_level: str | None = None
    margin_guidance: str | None = None
    margin_time: str | None = None
    activity: deque[Text] = field(default_factory=lambda: deque(maxlen=8))


class DashboardReporter:
    def __init__(
        self,
        *,
        mode: str,
        tw_leg_display: str = "TW instrument",
        us_leg_display: str = "US instrument",
        tw_leg_symbol: str | None = None,
        binance_symbol: str | None = None,
        bitopro_symbol: str | None = None,
        gate_text: str | None = None,
        stream: TextIO | None = None,
        color: bool | None = None,
        refresh_per_second: float = 4.0,
    ) -> None:
        self.tw_leg_display = tw_leg_display
        self.us_leg_display = us_leg_display
        self.state = DashboardState(
            mode=mode,
            tw_leg_symbol=tw_leg_symbol,
            binance_symbol=binance_symbol,
            bitopro_symbol=bitopro_symbol,
            gate_text=gate_text,
        )
        force_terminal = True if color else None
        no_color = color is False
        self.console = Console(
            file=stream or sys.stdout,
            force_terminal=force_terminal,
            no_color=no_color,
        )
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )
        self._live.start()

    # ------------------------------------------------------------------
    # Reporter protocol
    # ------------------------------------------------------------------
    def live(
        self,
        timestamp: Any,
        spread_snapshot: TradableSpreadSnapshot,
        strategy_state: Any,
    ) -> None:
        self.state.session = "trading"
        self.state.next_open_text = None
        self.state.countdown_text = None
        self.state.quote_time = format_time(timestamp, with_seconds=True)
        self.state.quote_snapshot = spread_snapshot
        self._absorb_strategy_state(strategy_state)
        self._refresh()

    def live_non_trading(
        self,
        timestamp: Any,
        next_open_at: Any,
        reason: str,
    ) -> None:
        self.state.session = f"non-trading ({reason})" if reason else "non-trading"
        self.state.next_open_text = format_next_open(next_open_at)
        self.state.countdown_text = format_countdown(timestamp, next_open_at)
        self.state.quote_time = format_time(timestamp, with_seconds=True)
        self._refresh()

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
        self.state.session = "trading"
        self.state.bar_time = format_time(timestamp, with_seconds=False)
        self.state.bar_snapshot = spread_snapshot
        self.state.bar_pnl = unrealized_pnl
        self.state.bar_equity = equity
        self.state.bar_account_display = account_display
        self._absorb_strategy_state(strategy_state)
        action_text = str(getattr(action, "value", action))
        if action_text != "none":
            self.state.decision_text = compact_action(action, reason)
            self.state.decision_time = self.state.bar_time
        self._refresh()

    def warn(self, timestamp: Any, code: str, detail: str = "") -> None:
        code = self._present(code)
        detail = self._present(detail)
        self._log("WARN", timestamp, code, detail, "yellow")
        self._absorb_status_event(timestamp, code, detail)
        self._refresh()

    def event(self, timestamp: Any, code: str, detail: str = "") -> None:
        code = self._present(code)
        detail = self._present(detail)
        self._log("EVENT", timestamp, code, detail, "magenta")
        self._absorb_status_event(timestamp, code, detail)
        self._refresh()

    def error(self, timestamp: Any, message: str) -> None:
        self._log("ERR", timestamp, self._present(message), "", "bold red")
        self._refresh()

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
        stamp = format_time(timestamp, with_seconds=True)
        line = Text()
        line.append(f"{stamp} ", style="dim")
        line.append("TRADE EXIT", style="magenta")
        values = format_trade_pnl_values(summary)
        if values is None:
            line.append(" trade_pnl_twd unavailable", style="yellow")
        else:
            net_text = f"net={format_twd(summary.net_pnl_twd)}"
            net_style = "green" if float(summary.net_pnl_twd or 0.0) >= 0 else "red"
            line.append(f" {net_text}", style=net_style)
            line.append(f" {values.split(' ', 1)[1]} TWD", style="dim")
        self.state.activity.append(line)
        self._refresh()

    def finish(self) -> None:
        self._live.stop()

    # ------------------------------------------------------------------
    # State absorption
    # ------------------------------------------------------------------
    def _absorb_strategy_state(self, strategy_state: Any) -> None:
        self.state.state_text = state_value(strategy_state)
        direction = getattr(strategy_state, "position_direction", None)
        self.state.position_direction = (
            direction_text(
                direction,
                tw_leg_display=self.tw_leg_display,
                us_leg_display=self.us_leg_display,
            )
            if direction is not None
            else None
        )
        self.state.us_leg_units = float(getattr(strategy_state, "us_leg_units", 0.0) or 0.0)
        self.state.tw_leg_contracts = int(
            getattr(strategy_state, "tw_leg_contracts", 0) or 0
        )
        self.state.entry_zscore = getattr(strategy_state, "entry_zscore", None)
        trading_symbol = getattr(strategy_state, "trading_tw_leg_symbol", None)
        if trading_symbol:
            self.state.tw_leg_symbol = trading_symbol
            self.state.tw_leg_expiry = getattr(strategy_state, "trading_tw_leg_expiry", None)

    def _absorb_status_event(self, timestamp: Any, code: str, detail: str) -> None:
        code_text = str(code)
        lowered = code_text.lower()
        stamp = format_time(timestamp, with_seconds=True)
        if "reconcil" in lowered:
            self.state.reconciliation_text = (
                f"{code_text} {detail}".strip() if detail else code_text
            )
            self.state.reconciliation_time = stamp
        if "gate" in lowered:
            self.state.gate_text = (
                f"{code_text} {detail}".strip() if detail else code_text
            )
        if lowered == "contract_switch_done" and detail:
            self.state.tw_leg_symbol = detail
            self.state.tw_leg_expiry = None
        if lowered.startswith("margin_"):
            level_by_code = {
                "margin_red_line": "red_line",
                "margin_transfer_required": "transfer",
                "margin_check_failed": "failed",
                "margin_check_disabled": "disabled",
            }
            self.state.margin_level = level_by_code.get(lowered, "ok")
            self.state.margin_guidance = detail or code_text
            self.state.margin_time = stamp

    def _log(
        self,
        label: str,
        timestamp: Any,
        code: str,
        detail: str,
        style: str,
    ) -> None:
        stamp = format_time(timestamp, with_seconds=True)
        line = Text()
        line.append(f"{stamp} ", style="dim")
        line.append(label, style=style)
        line.append(f" {code}")
        if detail:
            line.append(f" {detail}", style="dim")
        self.state.activity.append(line)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        self._live.update(self._render())

    def _render(self) -> Group:
        return Group(
            self._header_panel(),
            self._market_panel(),
            self._strategy_panel(),
            self._margin_panel(),
            self._activity_panel(),
        )

    def _header_panel(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        session_style = (
            "green" if self.state.session == "trading" else "yellow"
        )
        session = Text(self.state.session, style=session_style)
        if self.state.next_open_text:
            session.append(
                f"  next={self.state.next_open_text} in={self.state.countdown_text}",
                style="dim",
            )
        table.add_row("Session", session)
        symbols = " | ".join(
            part
            for part in (
                self._tw_leg_label(),
                self.state.binance_symbol,
                self.state.bitopro_symbol,
            )
            if part
        )
        table.add_row("Symbols", symbols or "-")
        table.add_row("Gate", self.state.gate_text or "-")
        recon = self.state.reconciliation_text or "-"
        if self.state.reconciliation_time:
            recon = f"{recon}  ({self.state.reconciliation_time})"
        recon_style = (
            "red"
            if "mismatch" in recon or "error" in recon
            else ("green" if "matched" in recon else "")
        )
        table.add_row("Reconcile", Text(recon, style=recon_style))
        return Panel(table, title=f"Project Lux — {self.state.mode}", border_style="cyan")

    def _tw_leg_label(self) -> str | None:
        if not self.state.tw_leg_symbol:
            return None
        if self.state.tw_leg_expiry:
            return f"{self.state.tw_leg_symbol} (exp {self.state.tw_leg_expiry})"
        return self.state.tw_leg_symbol

    def _market_panel(self) -> Panel:
        table = Table(expand=True)
        table.add_column("", style="bold", ratio=2)
        table.add_column("mid", ratio=3)
        table.add_column("shortSpread", ratio=3)
        table.add_column("longSpread", ratio=3)
        table.add_row(
            f"QUOTE {self.state.quote_time or '-'}",
            *self._snapshot_cells(self.state.quote_snapshot),
        )
        bar_cells = self._snapshot_cells(self.state.bar_snapshot)
        table.add_row(f"BAR {self.state.bar_time or '-'}", *bar_cells)
        return Panel(table, title="Market", border_style="blue")

    def _snapshot_cells(
        self, snapshot: TradableSpreadSnapshot | None
    ) -> tuple[Text, Text, Text]:
        if snapshot is None:
            dash = Text("-", style="dim")
            return dash, dash, Text("-", style="dim")
        mid = Text(
            f"{format_float(snapshot.mid_spread, digits=2)} "
            f"(z={format_float(snapshot.mid_zscore, digits=2)})",
            style=z_style(snapshot.mid_zscore),
        )
        short = Text(
            f"{format_float(snapshot.short_spread, digits=2)} "
            f"(z={format_float(snapshot.short_zscore, digits=2)})",
            style=z_style(snapshot.short_zscore),
        )
        long_ = Text(
            f"{format_float(snapshot.long_spread, digits=2)} "
            f"(z={format_float(snapshot.long_zscore, digits=2)})",
            style=z_style(snapshot.long_zscore),
        )
        return mid, short, long_

    def _strategy_panel(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        state_text = Text(
            self.state.state_text,
            style=STATE_STYLES.get(self.state.state_text, ""),
        )
        table.add_row("State", state_text)
        if self.state.position_direction:
            position = (
                f"{self.state.position_direction}  "
                f"{metric_label(self.us_leg_display, 'units')}={self.state.us_leg_units:g}  "
                f"{metric_label(self.tw_leg_display, 'contracts')}={self.state.tw_leg_contracts}"
            )
            if self.state.entry_zscore is not None:
                position += f"  entry_z={format_float(self.state.entry_zscore, digits=2)}"
        else:
            position = "flat"
        table.add_row("Position", position)
        decision = self.state.decision_text or "-"
        if self.state.decision_time:
            decision = f"{decision}  ({self.state.decision_time})"
        table.add_row("Decision", decision)
        # Real-account panel (replaces synthetic model pnl/equity): combined
        # position uPnL and per-venue 保證金水位 (equity/notional ratio).
        account_display = self.state.bar_account_display
        upnl = account_pnl_text(account_display)
        stale_suffix = "  (stale)" if getattr(account_display, "stale", False) else ""
        binance_ratio = format_pct(getattr(account_display, "binance_ratio", None))
        fubon_ratio = format_pct(getattr(account_display, "fubon_ratio", None))
        table.add_row("uPnL (TWD)", f"{upnl}{stale_suffix}")
        table.add_row("Margin bina/fubon", f"{binance_ratio} / {fubon_ratio}")
        return Panel(table, title="Strategy", border_style="green")

    def _present(self, value: Any) -> str:
        return instrument_text(
            value,
            tw_leg_display=self.tw_leg_display,
            us_leg_display=self.us_leg_display,
        )

    def _margin_panel(self) -> Panel:
        style_by_level = {
            "ok": "green",
            "rebalance": "yellow",
            "transfer": "yellow",
            "red_line": "bold red",
            "failed": "yellow",
            "disabled": "dim",
        }
        if self.state.margin_level is None:
            body: Text | Table = Text("no margin check yet", style="dim")
        else:
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold")
            table.add_column()
            table.add_row(
                "Level",
                Text(
                    self.state.margin_level,
                    style=style_by_level.get(self.state.margin_level, ""),
                ),
            )
            guidance = self.state.margin_guidance or "-"
            if self.state.margin_time:
                guidance = f"{guidance}  ({self.state.margin_time})"
            table.add_row("Guidance", guidance)
            body = table
        border = (
            "red"
            if self.state.margin_level == "red_line"
            else ("yellow" if self.state.margin_level == "transfer" else "cyan")
        )
        return Panel(body, title="Margin", border_style=border)

    def _activity_panel(self) -> Panel:
        if self.state.activity:
            body: Group | Text = Group(*self.state.activity)
        else:
            body = Text("no events yet", style="dim")
        return Panel(body, title="Activity", border_style="magenta")
