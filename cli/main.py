import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


@app.command("screen")
def screen_command(
    universe: str = typer.Option(
        "all", "--universe", "-u", help="nasdaq100, sp500 (alias spy), or all; comma-separate several"
    ),
    capital: float = typer.Option(10_000, "--capital", help="Account size to split across the picks, in dollars"),
    annual_target: float = typer.Option(
        25.0, "--annual-target", help="The yearly profit you aim for, as a % of capital"
    ),
    top: int = typer.Option(3, "--top", help="How many picks to return"),
    risk_pct: float = typer.Option(
        1.0, "--risk", help="Most a single position may lose at its stop, as a % of capital"
    ),
    date: str = typer.Option(None, "--date", help="Screen as of this date, YYYY-MM-DD (default: today)"),
    deep: bool = typer.Option(
        False, "--deep", help="Also run the full multi-agent analysis on each pick (needs an LLM key)"
    ),
):
    """Screen the Nasdaq-100 and S&P 500 for the top swing trades, sized to your capital."""
    from cli.screen import render_screen
    from tradingagents.screener import screen
    from tradingagents.universes import load_universe

    try:
        constituents = load_universe(universe)
        if date and not iter_grid(date, date):  # iter_grid validates YYYY-MM-DD
            raise ValueError(f"{date} is in the future; screen as of today or an earlier date")
        console.print(f"[dim]Reading daily bars for {len(constituents)} tickers...[/dim]")

        def show_progress(done, total):
            if done % 100 == 0 or done == total:
                console.print(f"[dim]  {done}/{total}[/dim]")

        result = screen(constituents, date, capital=capital, annual_target_pct=annual_target, top=top,
                        risk_pct=risk_pct / 100, progress=show_progress)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    render_screen(result)

    if deep and result.picks:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        try:
            graph = TradingAgentsGraph(config=DEFAULT_CONFIG)
        except Exception as exc:  # a missing key is a setup error
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None
        for pick in result.picks:
            ticker = pick.constituent.yahoo_symbol
            console.print(f"\n[bold]Agents on {ticker}[/bold] ({result.as_of})...")
            try:
                state, signal = graph.propagate(ticker, result.as_of)
            except Exception as exc:  # a missing key or an outage ends this pick, not the rest
                console.print(f"[red]{ticker}: {escape(str(exc))}[/red]")
                continue
            console.print(f"[bold]{ticker}: {signal}[/bold]")
            console.print(escape(state.get("final_trade_decision", "")))


# Spelled the way people ask for it: `tradingagents analyse-stock`.
app.command("analyse-stock", help="Alias of `screen`.")(screen_command)
app.command("analyze-stock", hidden=True)(screen_command)


@app.command()
def autotrade(
    universe: str = typer.Option("all", "--universe", "-u", help="nasdaq100, sp500 (alias spy), or all"),
    capital: float = typer.Option(10_000, "--capital", help="Capital the loop trades with, in dollars"),
    daily_target: float = typer.Option(100, "--daily-target", help="The profit per day you aim for, in dollars"),
    risk_pct: float = typer.Option(1.0, "--risk", help="Most one position may lose at its stop, as a % of capital"),
    max_positions: int = typer.Option(3, "--max-positions", help="Most positions open at once"),
    host: str = typer.Option("127.0.0.1", "--host", help="IB Gateway / TWS host"),
    port: int = typer.Option(4002, "--port", help="4002 = IB Gateway paper, 7497 = TWS paper"),
    client_id: int = typer.Option(17, "--client-id", help="API client id for this connection"),
    state: str = typer.Option(None, "--state", help="State file (default ~/.tradingagents/autotrade/state.json)"),
    not_before: str = typer.Option(
        "09:31", "--not-before", help="Wait until this US Eastern time so the opening print is in, HH:MM"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be traded without sending orders"),
):
    """Run one day of the swing-trade loop on an IBKR PAPER account: exits, then new entries."""
    import time
    from datetime import datetime, timedelta
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from tradingagents.autotrade import (
        DEFAULT_STATE_PATH,
        IBKRBroker,
        NotPaperAccountError,
        run_day,
    )
    from tradingagents.screener import screen
    from tradingagents.universes import load_universe

    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    hour, minute = (int(x) for x in not_before.split(":"))
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not dry_run and now < start:
        console.print(f"[dim]Waiting until {not_before} ET...[/dim]")
        time.sleep((start - now).total_seconds())

    constituents = load_universe(universe)

    # Screen on yesterday's close: during the session Yahoo's last bar is today's first minutes,
    # which would skew the signals and hide the day's gap from the don't-chase check.
    yesterday = datetime.now(eastern).date() - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)

    def screen_fn(top):
        console.print(f"[dim]Screening {len(constituents)} tickers as of {yesterday}...[/dim]")
        return screen(constituents, yesterday.isoformat(), capital=capital, daily_target=daily_target, top=top, risk_pct=risk_pct / 100)

    try:
        broker = IBKRBroker(host, port, client_id)
    except Exception as exc:  # Gateway not running, wrong port, ib_async missing
        console.print(f"[red]Could not connect to IB Gateway on {host}:{port}: {escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    try:
        report = run_day(broker, screen_fn, capital=capital, risk_pct=risk_pct / 100,
                         max_positions=max_positions, state_path=Path(state) if state else DEFAULT_STATE_PATH,
                         dry_run=dry_run)
    except NotPaperAccountError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    finally:
        broker.close()

    console.print(f"\n[bold]Autotrade {report.day}[/bold] on paper account {report.account}"
                  f"{' (dry run)' if dry_run else ''}")
    if report.screen:
        console.print(f"Market: {report.screen.market_regime}")
    for action in report.actions:
        console.print(f"  {action.kind:<9} {action.symbol:<6} {escape(action.detail)}")
    console.print("[dim]Paper trading only. A rules-based loop, not financial advice.[/dim]")


# The plans `login` and `logout` take: the providers signed in to, not keyed.
Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in with your ChatGPT Plus or Pro plan, for the chatgpt provider."""
    from tradingagents.llm_clients import chatgpt_auth

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = chatgpt_auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except chatgpt_auth.ChatGPTAuthError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Signed in to ChatGPT as {escape(email)}.[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of your ChatGPT plan: revoke this machine's access and forget it."""
    from tradingagents.llm_clients import chatgpt_auth

    outcome = chatgpt_auth.sign_out()
    if outcome == "revoked":
        console.print("Signed out of ChatGPT.")
    elif outcome == "forgotten":
        console.print("[yellow]Signed out here, but OpenAI could not be reached to revoke the sign-in; "
                      "it stays valid at OpenAI until it expires.[/yellow]")
    else:
        console.print("Not signed in to ChatGPT.")


if __name__ == "__main__":
    app()
