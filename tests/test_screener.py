"""The swing-trade screen: universes, filters, ranking and position sizing."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

import tradingagents.screener as sc
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.universes import Constituent, load_universe


def _bars(start=100.0, drift=0.002, noise=0.004, span=0.012, n=260, volume=2_000_000, seed=0) -> pd.DataFrame:
    """Daily bars on a random walk with the given drift, ending 2026-10-23.

    ``span`` sets each bar's high and low around its close, so the daily range
    (ATR) is chosen apart from how noisy the closes are.
    """
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + noise * rng.standard_normal(n)))
    dates = pd.bdate_range(end="2026-10-23", periods=n)
    return pd.DataFrame({
        "Date": dates, "Open": close, "High": close * (1 + span), "Low": close * (1 - span),
        "Close": close, "Volume": np.full(n, volume, dtype=float),
    })


def _uptrend_with_pullback(seed=0, start=100.0, volume=2_000_000) -> pd.DataFrame:
    """A steady climb whose last week gives a little back: the setup the screen wants."""
    bars = _bars(start=start, drift=0.003, volume=volume, seed=seed)
    bars.loc[bars.index[-5:], ["Close", "Open", "High", "Low"]] *= np.linspace(0.99, 0.96, 5)[:, None]
    return bars


def _c(ticker: str) -> Constituent:
    return Constituent(ticker, f"{ticker} Inc.", "Technology", ("nasdaq100",))


@pytest.mark.unit
def test_universes_cover_both_indexes_without_duplicates():
    ndx, spx, both = load_universe("nasdaq100"), load_universe("spy"), load_universe("all")
    assert len(ndx) == 101 and len(spx) == 503
    tickers = [c.ticker for c in both]
    assert len(tickers) == len(set(tickers))
    assert {"NVDA", "AAPL", "JPM"} <= set(tickers)
    nvda = next(c for c in both if c.ticker == "NVDA")
    assert set(nvda.universes) == {"nasdaq100", "sp500"}


@pytest.mark.unit
def test_share_classes_use_yahoo_spelling():
    brk = next(c for c in load_universe("sp500") if c.ticker == "BRK.B")
    assert brk.yahoo_symbol == "BRK-B"


@pytest.mark.unit
def test_unknown_universe_is_a_value_error():
    with pytest.raises(ValueError, match="Unknown universe"):
        load_universe("dow")


@pytest.mark.unit
def test_signals_need_enough_history():
    assert sc.compute_signals(_bars(n=100)) is None
    assert sc.compute_signals(_bars()) is not None


@pytest.mark.unit
def test_a_downtrend_is_rejected_and_a_pulled_back_uptrend_passes():
    down = sc.compute_signals(_bars(drift=-0.003))
    assert sc.rejection(down) == "not in an uptrend"
    assert sc.rejection(sc.compute_signals(_uptrend_with_pullback())) is None


@pytest.mark.unit
def test_thin_volume_is_rejected():
    thin = sc.compute_signals(_uptrend_with_pullback(volume=1_000))
    assert sc.rejection(thin) == "thinly traded"


@pytest.mark.unit
def test_position_risks_at_most_the_budget_and_fits_its_slice():
    sig = sc.compute_signals(_uptrend_with_pullback())
    plan = sc.plan_trade(sig, capital=10_000, slots=3, risk_pct=0.01)
    assert plan.shares >= 1
    assert plan.risk <= 100 + 1e-9
    assert plan.position_value <= 10_000 / 3 + 1e-9
    assert plan.stop < plan.entry < plan.target
    assert plan.target - plan.entry == pytest.approx(sc.REWARD_R * (plan.entry - plan.stop))


@pytest.mark.unit
def test_screen_returns_top_three_and_survives_unreadable_tickers():
    frames = {f"T{i}": _uptrend_with_pullback(seed=i) for i in (0, 1, 2, 3, 5, 6)}
    frames["DOWN"] = _bars(drift=-0.003)
    frames["SPY"] = _bars(drift=0.001, noise=0.008)

    def loader(symbol, as_of):
        if symbol == "BROKEN":
            raise VendorUnavailableError("Yahoo Finance is unreachable")
        return frames[symbol]

    universe = [_c(t) for t in [*frames, "BROKEN"] if t != "SPY"]
    result = sc.screen(universe, "2026-10-23", loader=loader, workers=2)

    assert result.scanned == 8
    assert result.passed == 6
    assert len(result.picks) == 3
    assert [p.score for p in result.picks] == sorted((p.score for p in result.picks), reverse=True)
    assert "DOWN" not in {p.constituent.ticker for p in result.picks}
    assert result.failures == [("BROKEN", "Yahoo Finance is unreachable")]
    assert result.deployed <= 10_000
    assert result.market_regime.startswith("risk-on")
    assert any("$2,500" in note for note in result.goal_notes())


@pytest.mark.unit
def test_a_share_dearer_than_its_slice_is_skipped():
    frames = {"PRICEY": _uptrend_with_pullback(start=50_000), "CHEAP": _uptrend_with_pullback(seed=3)}
    result = sc.screen([_c("PRICEY"), _c("CHEAP")], "2026-10-23", loader=lambda s, d: frames.get(s))
    assert [p.constituent.ticker for p in result.picks] == ["CHEAP"]


@pytest.mark.unit
def test_cli_screen_prints_a_plan(monkeypatch):
    import cli.main as m

    frames = {f"T{i}": _uptrend_with_pullback(seed=i) for i in range(4)}
    monkeypatch.setattr("tradingagents.universes.load_universe", lambda names: [_c(t) for t in frames])
    real_screen = sc.screen
    monkeypatch.setattr(sc, "screen", lambda *a, **kw: real_screen(*a, **kw, loader=lambda s, d: frames.get(s)))

    result = CliRunner().invoke(m.app, ["analyse-stock", "--capital", "10000", "--annual-target", "25"])

    assert result.exit_code == 0, result.output
    assert "Top 3 for $10,000" in result.output
    assert "not financial advice" in result.output


@pytest.mark.unit
def test_cli_screen_rejects_a_bad_universe():
    import cli.main as m

    result = CliRunner().invoke(m.app, ["screen", "--universe", "dow"])
    assert result.exit_code == 1
    assert "Unknown universe" in result.output
