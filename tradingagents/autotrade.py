"""Daily swing-trade loop on an Interactive Brokers *paper* account.

Run once each weekday at the open (``tradingagents autotrade``). One run:

1. refuses to go on unless every account on the connection is a paper account
   (IBKR paper account IDs start with ``DU``);
2. does nothing on a day the market is closed;
3. reconciles the positions it opened earlier: a bracket whose stop or target
   filled is recorded as closed, an entry that never filled is dropped, and a
   position held ``MAX_HOLD_DAYS`` trading days is sold at market (the time stop);
4. if there are free slots and SPY is above its 200-day average, screens the
   universe and buys the best setups with a bracket order: a limit entry near the
   current price, and a GTC stop and GTC target that IBKR works all day, so the
   exits do not depend on this script running.

It skips a pick that is up more than ``MAX_GAP_PCT`` on the day or reports
earnings inside the hold. It only touches positions it opened (tracked in the
state file and tagged ``ORDER_REF`` at IBKR). Not financial advice.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np

from tradingagents.screener import MAX_HOLD_DAYS, ScreenResult, Signals, plan_trade

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_PREFIX = "DU"
ORDER_REF = "tradingagents-swing"
MAX_GAP_PCT = 3.0  # don't chase: skip a pick already up this much on the day
ENTRY_SLIPPAGE = 0.002  # the limit sits this far above the current price so it fills at the open
EARNINGS_BUFFER_DAYS = 16  # calendar days that cover the 10-session hold
DEFAULT_STATE_PATH = Path.home() / ".tradingagents" / "autotrade" / "state.json"


class NotPaperAccountError(RuntimeError):
    """The connection is not to a paper account, so no order may be sent."""


class Broker(Protocol):
    def accounts(self) -> list[str]: ...
    def market_open_today(self) -> bool: ...
    def positions(self) -> dict[str, int]: ...
    def working_symbols(self) -> set[str]: ...
    def price(self, symbol: str) -> float | None: ...
    def place_bracket(self, symbol: str, shares: int, limit: float, stop: float, target: float) -> int: ...
    def close_position(self, symbol: str, shares: int) -> None: ...


@dataclass
class Action:
    kind: str  # buy, skip, closed, unfilled, time-exit, hold, info
    symbol: str
    detail: str


@dataclass
class DayReport:
    day: str
    account: str
    actions: list[Action] = field(default_factory=list)
    screen: ScreenResult | None = None

    def add(self, kind: str, symbol: str, detail: str) -> None:
        logger.info("%s %s: %s", kind, symbol, detail)
        self.actions.append(Action(kind, symbol, detail))


def require_paper(accounts: list[str]) -> str:
    """The paper account to trade, or ``NotPaperAccountError`` if any account is live."""
    if not accounts:
        raise NotPaperAccountError("IBKR reported no account on this connection")
    live = [a for a in accounts if not a.upper().startswith(PAPER_ACCOUNT_PREFIX)]
    if live:
        raise NotPaperAccountError(
            f"refusing to trade: account(s) {', '.join(live)} are not paper accounts "
            f"(paper IDs start with {PAPER_ACCOUNT_PREFIX}). Log IB Gateway in with your paper username."
        )
    return accounts[0]


def trading_days_held(entry_day: str, today: date) -> int:
    return int(np.busday_count(date.fromisoformat(entry_day), today))


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"positions": {}, "history": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def earnings_within(symbol: str, today: date, days: int = EARNINGS_BUFFER_DAYS) -> date | None:
    """The next earnings date if it falls within ``days``, from Yahoo; None if none or unknown."""
    try:
        from tradingagents.dataflows.vendors.yahoo.earnings import upcoming_earnings_dates

        dates = upcoming_earnings_dates(symbol)
    except Exception as exc:  # unknown earnings must not block the run
        logger.warning("Could not read earnings for %s: %s", symbol, exc)
        return None
    upcoming = [d for d in dates if today <= d <= today + timedelta(days=days)]
    return min(upcoming) if upcoming else None


def _reconcile(broker: Broker, state: dict, today: date, report: DayReport, dry_run: bool) -> None:
    held = broker.positions()
    working = broker.working_symbols()
    for symbol, pos in list(state["positions"].items()):
        shares = held.get(symbol, 0)
        if shares <= 0 and symbol not in working:
            kind = "closed" if pos["status"] == "open" else "unfilled"
            detail = ("stop or target filled" if kind == "closed"
                      else f"entry at {pos['limit']:.2f} did not fill; dropped")
            report.add(kind, symbol, detail)
            state["history"].append({**pos, "symbol": symbol, "status": kind, "exit_date": today.isoformat()})
            del state["positions"][symbol]
            continue
        if shares > 0:
            pos["status"] = "open"
            pos["shares"] = shares
        if pos["status"] != "open":
            report.add("hold", symbol, "entry order still working")
            continue
        days = trading_days_held(pos["entry_date"], today)
        if days >= MAX_HOLD_DAYS:
            report.add("time-exit", symbol, f"held {days} trading days; selling {shares} at market")
            if not dry_run:
                broker.close_position(symbol, shares)
                state["history"].append({**pos, "symbol": symbol, "status": "time-exit",
                                         "exit_date": today.isoformat()})
                del state["positions"][symbol]
        else:
            report.add("hold", symbol, f"day {days} of {MAX_HOLD_DAYS}; stop {pos['stop']:.2f}, "
                                       f"target {pos['target']:.2f}")


def run_day(
    broker: Broker,
    screen_fn: Callable[[int], ScreenResult],
    *,
    capital: float = 10_000,
    risk_pct: float = 0.01,
    max_positions: int = 3,
    state_path: Path = DEFAULT_STATE_PATH,
    today: date | None = None,
    dry_run: bool = False,
    earnings_fn: Callable[[str, date], date | None] = earnings_within,
) -> DayReport:
    """One day of the loop. ``screen_fn(top)`` returns the screen's ``top`` best setups."""
    account = require_paper(broker.accounts())
    today = today or date.today()
    report = DayReport(day=today.isoformat(), account=account)
    if not broker.market_open_today():
        report.add("info", "-", "market closed today; nothing to do")
        return report

    state = load_state(state_path)
    _reconcile(broker, state, today, report, dry_run)

    slots = max_positions - len(state["positions"])
    if slots <= 0:
        report.add("info", "-", f"all {max_positions} slots in use")
        if not dry_run:
            save_state(state_path, state)
        return report

    # More candidates than slots, so the earnings and gap checks have spares to fall back on.
    result = screen_fn(max_positions * 4)
    report.screen = result
    market = result.market
    if market is None or market.close <= market.sma200:
        report.add("info", "SPY", f"no new entries: {result.market_regime}")
        if not dry_run:
            save_state(state_path, state)
        return report

    held = set(broker.positions()) | set(state["positions"])
    for pick in result.picks:
        if slots == 0:
            break
        sig: Signals = pick.signals
        symbol = pick.constituent.ticker
        if symbol in held:
            continue
        live = broker.price(symbol)
        if live is None or not math.isfinite(live) or live <= 0:
            report.add("skip", symbol, "no live price")
            continue
        gap = 100 * (live / sig.close - 1)
        if gap > MAX_GAP_PCT:
            report.add("skip", symbol, f"up {gap:.1f}% today (limit {MAX_GAP_PCT:g}%)")
            continue
        earnings = earnings_fn(pick.constituent.yahoo_symbol, today)
        if earnings:
            report.add("skip", symbol, f"earnings on {earnings.isoformat()} inside the hold")
            continue
        # Re-plan from the live price: same ATR, stop and target distances.
        plan = plan_trade(dataclasses.replace(sig, close=live), capital, max_positions, risk_pct)
        if plan.shares < 1:
            report.add("skip", symbol, "one share is more than a position's slice of capital")
            continue
        limit = round(live * (1 + ENTRY_SLIPPAGE), 2)
        stop, target = round(plan.stop, 2), round(plan.target, 2)
        detail = (f"{plan.shares} sh, limit {limit:.2f}, stop {stop:.2f}, target {target:.2f}, "
                  f"risk ${plan.shares * (limit - stop):,.0f}")
        report.add("buy", symbol, detail + (" (dry run, not sent)" if dry_run else ""))
        order_id = None if dry_run else broker.place_bracket(symbol, plan.shares, limit, stop, target)
        state["positions"][symbol] = {
            "status": "pending", "entry_date": today.isoformat(), "shares": plan.shares,
            "limit": limit, "stop": stop, "target": target, "order_id": order_id,
        }
        slots -= 1

    if not dry_run:
        save_state(state_path, state)
    return report


class IBKRBroker:
    """``Broker`` over IB Gateway or TWS, through ib_async (``pip install "tradingagents[ibkr]"``)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 17):
        try:
            from ib_async import IB
        except ImportError as exc:
            raise RuntimeError('autotrade needs ib_async: pip install "tradingagents[ibkr]"') from exc
        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id, timeout=15)
        self.ib.reqMarketDataType(3)  # live where subscribed, delayed otherwise

    def close(self) -> None:
        self.ib.disconnect()

    def _stock(self, symbol: str):
        from ib_async import Stock

        contract = Stock(symbol.replace(".", " ").replace("-", " "), "SMART", "USD")
        self.ib.qualifyContracts(contract)
        return contract

    def accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def market_open_today(self) -> bool:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        details = self.ib.reqContractDetails(self._stock("SPY"))
        if not details:
            return False
        tz = ZoneInfo(details[0].timeZoneId or "US/Eastern")
        today = datetime.now(tz).strftime("%Y%m%d")
        for session in details[0].liquidHours.split(";"):
            if session.startswith(today):
                return "CLOSED" not in session
        return False

    def positions(self) -> dict[str, int]:
        return {p.contract.symbol.replace(" ", "."): int(p.position)
                for p in self.ib.positions() if p.contract.secType == "STK" and p.position}

    def _bot_trades(self, symbol: str | None = None):
        return [t for t in self.ib.openTrades() if t.order.orderRef == ORDER_REF
                and (symbol is None or t.contract.symbol.replace(" ", ".") == symbol)]

    def working_symbols(self) -> set[str]:
        return {t.contract.symbol.replace(" ", ".") for t in self._bot_trades()}

    def price(self, symbol: str) -> float | None:
        [ticker] = self.ib.reqTickers(self._stock(symbol))
        value = ticker.marketPrice()
        return float(value) if value == value else None  # NaN when there is no quote

    def place_bracket(self, symbol: str, shares: int, limit: float, stop: float, target: float) -> int:
        contract = self._stock(symbol)
        bracket = self.ib.bracketOrder("BUY", shares, limit, target, stop)
        bracket.parent.tif = "DAY"
        for order in bracket:
            order.orderRef = ORDER_REF
        bracket.takeProfit.tif = bracket.stopLoss.tif = "GTC"
        for order in bracket:
            self.ib.placeOrder(contract, order)
        self.ib.sleep(1)
        return bracket.parent.orderId

    def close_position(self, symbol: str, shares: int) -> None:
        from ib_async import MarketOrder

        for trade in self._bot_trades(symbol):
            self.ib.cancelOrder(trade.order)
        self.ib.sleep(1)
        order = MarketOrder("SELL", shares, tif="DAY")
        order.orderRef = ORDER_REF
        self.ib.placeOrder(self._stock(symbol), order)
        self.ib.sleep(1)
