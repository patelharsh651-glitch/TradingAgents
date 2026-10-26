"""Render a swing-trade screen, and optionally run the agents on its picks."""

from __future__ import annotations

from rich import box
from rich.markup import escape
from rich.table import Table

from cli.display import console
from tradingagents.screener import MAX_HOLD_DAYS, REWARD_R, STOP_ATR, ScreenResult


def render_screen(result: ScreenResult) -> None:
    console.print(
        f"\n[bold]Swing-trade screen[/bold] as of {result.as_of}: scanned {result.scanned}, "
        f"{result.passed} passed the filters."
    )
    console.print(f"Market: {result.market_regime}")
    if not result.picks:
        console.print("[yellow]No ticker passed every filter today; no trade is a valid answer.[/yellow]")
        _render_failures(result)
        return

    table = Table(title=f"Top {len(result.picks)} for ${result.capital:,.0f}", box=box.SIMPLE_HEAD,
                  pad_edge=False, collapse_padding=True)
    for column in ("#", "Ticker", "Score", "Entry", "Stop", "Target", "Shares", "Cost", "Risk", "Reward"):
        table.add_column(column, justify="left" if column == "Ticker" else "right")
    for rank, pick in enumerate(result.picks, 1):
        p = pick.plan
        table.add_row(
            str(rank), pick.constituent.ticker, f"{pick.score:.0f}",
            f"{p.entry:.2f}", f"{p.stop:.2f}", f"{p.target:.2f}", str(p.shares),
            f"${p.position_value:,.0f}", f"${p.risk:,.0f}", f"${p.reward:,.0f}",
        )
    console.print(table)

    vs_market = "vs SPY" if result.market else "(SPY unavailable, absolute)"
    for rank, pick in enumerate(result.picks, 1):
        s = pick.signals
        console.print(
            f"[dim]{rank}. {pick.constituent.ticker}, {escape(pick.constituent.company)} "
            f"({escape(pick.constituent.sector)}): "
            f"RSI {s.rsi14:.0f}, ATR {s.atr_pct:.1f}%/day, {s.sma20_distance:+.1f}% vs 20-day avg, "
            f"20-day {s.ret20:+.1f}%, {pick.rel_strength:+.1f} pts {vs_market} over 60 days, "
            f"volume {s.volume_ratio:.1f}x normal, moves ~${pick.daily_range:,.0f}/day; "
            f"last bar {s.last_date}[/dim]"
        )

    console.print(
        f"\n[bold]Plan[/bold]: buy near the entry, stop {STOP_ATR} ATR below, target {REWARD_R:g}x the risk above. "
        f"Each position risks at most {100 * result.risk_pct:g}% of capital. "
        f"Exit after {MAX_HOLD_DAYS} trading days if neither level is hit, and check earnings dates before entering."
    )
    console.print(f"Deployed ${result.deployed:,.0f} of ${result.capital:,.0f}.")
    for note in result.goal_notes():
        console.print(f"• {note}")
    _render_failures(result)
    console.print(
        "[dim]A rules-based screen, not financial advice. Run `tradingagents screen --deep` "
        "to have the analyst agents review the picks.[/dim]"
    )


def _render_failures(result: ScreenResult) -> None:
    if result.failures:
        sample = ", ".join(t for t, _ in result.failures[:10])
        more = f" and {len(result.failures) - 10} more" if len(result.failures) > 10 else ""
        console.print(f"[yellow]{len(result.failures)} ticker(s) could not be read: {sample}{more}[/yellow]")
