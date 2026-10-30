"""Swing-trade screener: rank a ticker universe and size the best few setups.

The screen is rules-based and needs no LLM. For every ticker it reads daily
bars up to ``as_of`` (point-in-time, through the data layer's cached loader),
keeps the ones in a healthy uptrend that have pulled back rather than run away,
ranks them, and turns the top ``top`` into a trade plan sized to the capital:

* entry at the last close, stop ``STOP_ATR`` ATRs below, target at ``REWARD_R``
  times the risk above;
* each position risks at most ``risk_pct`` of the capital and holds at most an
  equal slice of it, so all of them can be open at once.

It answers "which few names are worth a closer look", not "what will make
money": the multi-agent analysis (``screen --deep``) is the closer look.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradingagents.universes import Constituent

logger = logging.getLogger(__name__)

# Bars needed for the 200-day average plus a little slack.
MIN_BARS = 210

# Filters a setup must pass.
MIN_PRICE = 10.0
MIN_DOLLAR_VOLUME = 25_000_000  # 20-day average, so a position fills without moving the price
RSI_RANGE = (40.0, 70.0)  # momentum without being overbought
ATR_PCT_RANGE = (1.5, 7.0)  # moves enough to pay for a swing, not so much that a stop is noise
SMA20_DISTANCE_RANGE = (-3.0, 8.0)  # % from the 20-day average: near it, not stretched above it

# The trade plan.
STOP_ATR = 1.5
REWARD_R = 2.0
MAX_HOLD_DAYS = 10

# How the score weighs each ranked signal.
WEIGHTS = {
    "rel_strength": 0.35,
    "ret20": 0.20,
    "volume_ratio": 0.15,
    "pullback": 0.15,
    "atr_fit": 0.15,
}
IDEAL_SMA20_DISTANCE = 1.5  # % above the 20-day average
IDEAL_ATR_PCT = 3.0

MARKET_SYMBOL = "SPY"


@dataclass(frozen=True)
class Signals:
    close: float
    sma20: float
    sma50: float
    sma200: float
    rsi14: float
    atr14: float
    ret20: float  # % return over 20 sessions
    ret60: float  # % return over 60 sessions
    dollar_volume: float  # 20-day average close x volume
    volume_ratio: float  # 5-day average volume over 50-day average
    last_date: str

    @property
    def atr_pct(self) -> float:
        return 100 * self.atr14 / self.close

    @property
    def sma20_distance(self) -> float:
        return 100 * (self.close / self.sma20 - 1)


@dataclass(frozen=True)
class TradePlan:
    entry: float
    stop: float
    target: float
    shares: int

    @property
    def position_value(self) -> float:
        return self.shares * self.entry

    @property
    def risk(self) -> float:
        return self.shares * (self.entry - self.stop)

    @property
    def reward(self) -> float:
        return self.shares * (self.target - self.entry)


@dataclass
class Pick:
    constituent: Constituent
    signals: Signals
    score: float
    plan: TradePlan
    rel_strength: float  # 60-session return minus SPY's, in points

    @property
    def daily_range(self) -> float:
        """Dollars the position typically moves in a day (shares x ATR)."""
        return self.plan.shares * self.signals.atr14


@dataclass
class ScreenResult:
    as_of: str
    capital: float
    annual_target_pct: float
    risk_pct: float
    scanned: int = 0
    passed: int = 0
    picks: list[Pick] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    market: Signals | None = None

    @property
    def deployed(self) -> float:
        return sum(p.plan.position_value for p in self.picks)

    @property
    def total_risk(self) -> float:
        return sum(p.plan.risk for p in self.picks)

    @property
    def daily_range(self) -> float:
        return sum(p.daily_range for p in self.picks)

    @property
    def market_regime(self) -> str:
        m = self.market
        if m is None:
            return "unknown (SPY data unavailable)"
        if m.close > m.sma50 > m.sma200:
            return "risk-on: SPY above its 50- and 200-day averages"
        if m.close > m.sma200:
            return "mixed: SPY above its 200-day but below its 50-day average; consider smaller size"
        return "risk-off: SPY below its 200-day average; swing longs fail more often, consider half size or waiting"

    def goal_notes(self) -> list[str]:
        """Plain statements of how the annual goal compares with what the picks can do."""
        goal = self.capital * self.annual_target_pct / 100
        notes = [
            f"{self.annual_target_pct:g}% a year on ${self.capital:,.0f} is ${goal:,.0f}, about ${goal / 12:,.0f} a month. "
            "There is no daily quota: skip days when nothing passes, and treat the goal as a target, not a promise."
        ]
        risk_unit = self.capital * self.risk_pct
        if risk_unit:
            r_needed = goal / risk_unit
            notes.append(
                f"At {100 * self.risk_pct:g}% risk (${risk_unit:,.0f}) per trade the goal is about {r_needed:.1f}R of net "
                f"profit a year. A full winner makes {REWARD_R:g}R, a stopped trade loses 1R; at a 45% win rate "
                f"that is roughly {r_needed / (0.45 * REWARD_R - 0.55):.0f} trades a year."
            )
        if self.picks:
            reward = sum(p.plan.reward for p in self.picks)
            notes.append(
                f"If every stop is hit you lose about ${self.total_risk:,.0f} "
                f"({100 * self.total_risk / self.capital:.1f}% of capital). "
                f"If every target is hit you make about ${reward:,.0f}"
                + (f" ({100 * reward / goal:.0f}% of the annual goal)." if goal else ".")
            )
        return notes


def compute_signals(bars: pd.DataFrame) -> Signals | None:
    """The screen's signals from daily OHLCV bars, or None with too little history."""
    if bars is None or len(bars) < MIN_BARS:
        return None
    close, high, low, volume = (bars[c].astype(float) for c in ("Close", "High", "Low", "Volume"))

    # Wilder's RSI and ATR (exponential smoothing with alpha = 1/14).
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    last_loss = loss.iloc[-1]
    rsi = 100.0 if last_loss == 0 else 100 - 100 / (1 + gain.iloc[-1] / last_loss)
    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]

    vol50 = volume.iloc[-50:].mean()
    last_date = bars["Date"].iloc[-1] if "Date" in bars.columns else bars.index[-1]
    return Signals(
        close=close.iloc[-1],
        sma20=close.iloc[-20:].mean(),
        sma50=close.iloc[-50:].mean(),
        sma200=close.iloc[-200:].mean(),
        rsi14=float(rsi),
        atr14=float(atr),
        ret20=100 * (close.iloc[-1] / close.iloc[-21] - 1),
        ret60=100 * (close.iloc[-1] / close.iloc[-61] - 1),
        dollar_volume=(close.iloc[-20:] * volume.iloc[-20:]).mean(),
        volume_ratio=volume.iloc[-5:].mean() / vol50 if vol50 else 0.0,
        last_date=str(pd.Timestamp(last_date).date()),
    )


def rejection(sig: Signals) -> str | None:
    """Why ``sig`` is not a swing setup, or None when it passes every filter."""
    if sig.close < MIN_PRICE:
        return f"price below ${MIN_PRICE:.0f}"
    if sig.dollar_volume < MIN_DOLLAR_VOLUME:
        return "thinly traded"
    if not sig.close > sig.sma50 > sig.sma200:
        return "not in an uptrend"
    if not RSI_RANGE[0] <= sig.rsi14 <= RSI_RANGE[1]:
        return f"RSI {sig.rsi14:.0f} outside {RSI_RANGE[0]:.0f}-{RSI_RANGE[1]:.0f}"
    if not ATR_PCT_RANGE[0] <= sig.atr_pct <= ATR_PCT_RANGE[1]:
        return f"daily range {sig.atr_pct:.1f}% outside {ATR_PCT_RANGE[0]}-{ATR_PCT_RANGE[1]}%"
    if not SMA20_DISTANCE_RANGE[0] <= sig.sma20_distance <= SMA20_DISTANCE_RANGE[1]:
        return f"{sig.sma20_distance:+.1f}% from the 20-day average"
    return None


def plan_trade(sig: Signals, capital: float, slots: int, risk_pct: float) -> TradePlan:
    """Entry, stop, target and share count for one of ``slots`` equal positions."""
    entry = sig.close
    stop = entry - STOP_ATR * sig.atr14
    target = entry + REWARD_R * (entry - stop)
    risk_budget = capital * risk_pct
    by_risk = risk_budget / (entry - stop)
    by_capital = (capital / slots) / entry
    return TradePlan(entry=entry, stop=stop, target=target, shares=math.floor(min(by_risk, by_capital)))


def _score(frame: pd.DataFrame) -> pd.Series:
    """Weighted percentile ranks of each candidate's signals, 0-100."""
    ranked = pd.DataFrame({
        "rel_strength": frame["rel_strength"].rank(pct=True),
        "ret20": frame["ret20"].rank(pct=True),
        "volume_ratio": frame["volume_ratio"].rank(pct=True),
        "pullback": (-(frame["sma20_distance"] - IDEAL_SMA20_DISTANCE).abs()).rank(pct=True),
        "atr_fit": (-(frame["atr_pct"] - IDEAL_ATR_PCT).abs()).rank(pct=True),
    })
    return 100 * sum(ranked[k] * w for k, w in WEIGHTS.items())


def screen(
    constituents: Iterable[Constituent],
    as_of: str | None = None,
    *,
    capital: float = 10_000,
    annual_target_pct: float = 25,
    top: int = 3,
    risk_pct: float = 0.01,
    loader: Callable[[str, str], pd.DataFrame] | None = None,
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
) -> ScreenResult:
    """Scan ``constituents`` as of ``as_of`` and return the ``top`` sized setups.

    ``loader(symbol, as_of)`` returns daily bars; it defaults to the data
    layer's cached Yahoo loader. A ticker whose bars cannot be read is recorded
    in ``failures`` and the scan goes on.
    """
    if capital <= 0 or top < 1 or not 0 < risk_pct < 1:
        raise ValueError("capital must be positive, top at least 1, and risk_pct between 0 and 1")
    if loader is None:
        from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv as loader
    as_of = as_of or date.today().isoformat()
    constituents = list(constituents)
    result = ScreenResult(as_of=as_of, capital=capital, annual_target_pct=annual_target_pct, risk_pct=risk_pct,
                          scanned=len(constituents))

    try:
        result.market = compute_signals(loader(MARKET_SYMBOL, as_of))
    except Exception as exc:  # the regime is context; the screen still runs without it
        logger.warning("Could not read %s: %s", MARKET_SYMBOL, exc)
    market_ret60 = result.market.ret60 if result.market else 0.0

    signals: dict[str, Signals] = {}

    def load(c: Constituent) -> Signals | None:
        return compute_signals(loader(c.yahoo_symbol, as_of))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(load, c): c for c in constituents}
        for done, future in enumerate(as_completed(futures), 1):
            c = futures[future]
            try:
                sig = future.result()
            except Exception as exc:  # one unreadable ticker must not end the scan
                result.failures.append((c.ticker, str(exc)))
            else:
                if sig is None:
                    result.failures.append((c.ticker, f"fewer than {MIN_BARS} daily bars"))
                elif rejection(sig) is None:
                    signals[c.ticker] = sig
            if progress:
                progress(done, len(constituents))

    result.passed = len(signals)
    if not signals:
        return result

    by_ticker = {c.ticker: c for c in constituents}
    frame = pd.DataFrame({
        t: {"rel_strength": s.ret60 - market_ret60, "ret20": s.ret20, "volume_ratio": s.volume_ratio,
            "sma20_distance": s.sma20_distance, "atr_pct": s.atr_pct}
        for t, s in signals.items()
    }).T
    frame["score"] = _score(frame)

    for ticker in frame.sort_values("score", ascending=False).index:
        plan = plan_trade(signals[ticker], capital, top, risk_pct)
        if plan.shares < 1:  # one share costs more than a position's slice of the capital
            continue
        result.picks.append(Pick(by_ticker[ticker], signals[ticker], float(frame.at[ticker, "score"]),
                                 plan, float(frame.at[ticker, "rel_strength"])))
        if len(result.picks) == top:
            break
    return result
