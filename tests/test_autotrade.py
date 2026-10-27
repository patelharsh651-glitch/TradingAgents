"""The daily paper-trading loop: the paper guard, exits, and new entries."""

from __future__ import annotations

from datetime import date

import pytest

import tradingagents.autotrade as at
from tradingagents.screener import Pick, ScreenResult, Signals, TradePlan
from tradingagents.universes import Constituent

TODAY = date(2026, 10, 28)


def _sig(close=100.0, atr=3.0, sma200=80.0) -> Signals:
    return Signals(close=close, sma20=99.0, sma50=95.0, sma200=sma200, rsi14=55.0, atr14=atr, ret20=4.0,
                   ret60=12.0, dollar_volume=1e9, volume_ratio=1.2, last_date="2026-10-27")


def _pick(ticker, close=100.0) -> Pick:
    return Pick(Constituent(ticker, f"{ticker} Inc.", "Tech", ("sp500",)), _sig(close), 80.0,
                TradePlan(close, close - 4.5, close + 9, 22), 5.0)


def _result(*tickers, spy_above_200=True) -> ScreenResult:
    r = ScreenResult(as_of=TODAY.isoformat(), capital=10_000, daily_target=100, risk_pct=0.01)
    r.market = _sig(close=500.0, sma200=450.0 if spy_above_200 else 550.0)
    r.picks = [_pick(t) for t in tickers]
    return r


class FakeBroker:
    def __init__(self, accounts=("DU1234567",), open_today=True, held=None, working=(), prices=None):
        self._accounts = list(accounts)
        self.open_today = open_today
        self.held = dict(held or {})
        self.working = set(working)
        self.prices = prices or {}
        self.brackets = []
        self.closed = []

    def accounts(self):
        return self._accounts

    def market_open_today(self):
        return self.open_today

    def positions(self):
        return self.held

    def working_symbols(self):
        return self.working

    def price(self, symbol):
        return self.prices.get(symbol, 100.0)

    def place_bracket(self, symbol, shares, limit, stop, target):
        self.brackets.append((symbol, shares, limit, stop, target))
        return len(self.brackets)

    def close_position(self, symbol, shares):
        self.closed.append((symbol, shares))


def _run(broker, result, tmp_path, **kw):
    kw.setdefault("earnings_fn", lambda s, d: None)
    return at.run_day(broker, lambda top: result, state_path=tmp_path / "state.json", today=TODAY, **kw)


def test_refuses_live_account(tmp_path):
    broker = FakeBroker(accounts=("U7654321",))
    with pytest.raises(at.NotPaperAccountError):
        _run(broker, _result("AAA"), tmp_path)
    assert broker.brackets == []


def test_refuses_mixed_accounts(tmp_path):
    with pytest.raises(at.NotPaperAccountError):
        _run(FakeBroker(accounts=("DU1", "U2")), _result("AAA"), tmp_path)


def test_market_closed_does_nothing(tmp_path):
    broker = FakeBroker(open_today=False)
    _run(broker, _result("AAA"), tmp_path)
    assert broker.brackets == []


def test_buys_top_picks_with_brackets(tmp_path):
    broker = FakeBroker()
    report = _run(broker, _result("AAA", "BBB", "CCC", "DDD"), tmp_path)
    assert [b[0] for b in broker.brackets] == ["AAA", "BBB", "CCC"]
    symbol, shares, limit, stop, target = broker.brackets[0]
    assert limit == 100.2 and stop == 95.5 and target == 109.0
    assert shares * (limit - stop) <= 100 + shares * 0.2  # ~1% of capital at the stop
    assert shares * 100 <= 10_000 / 3
    state = at.load_state(tmp_path / "state.json")
    assert set(state["positions"]) == {"AAA", "BBB", "CCC"}
    assert report.account == "DU1234567"


def test_skips_gap_up_and_earnings(tmp_path):
    broker = FakeBroker(prices={"AAA": 104.0})
    _run(broker, _result("AAA", "BBB", "CCC"), tmp_path,
         earnings_fn=lambda s, d: date(2026, 11, 2) if s == "BBB" else None)
    assert [b[0] for b in broker.brackets] == ["CCC"]


def test_no_entries_when_spy_below_200_day(tmp_path):
    broker = FakeBroker()
    _run(broker, _result("AAA", spy_above_200=False), tmp_path)
    assert broker.brackets == []


def test_dry_run_sends_nothing_and_keeps_no_state(tmp_path):
    broker = FakeBroker()
    report = _run(broker, _result("AAA"), tmp_path, dry_run=True)
    assert broker.brackets == [] and not (tmp_path / "state.json").exists()
    assert report.actions[0].kind == "buy"


def _seed(tmp_path, **positions):
    at.save_state(tmp_path / "state.json", {"positions": positions, "history": []})


def _pos(entry_date, status="open"):
    return {"status": status, "entry_date": entry_date, "shares": 20, "limit": 100.2, "stop": 95.5,
            "target": 109.0, "order_id": 1}


def test_records_bracket_exit_and_unfilled_entry(tmp_path):
    _seed(tmp_path, AAA=_pos("2026-10-20"), BBB=_pos("2026-10-27", status="pending"))
    broker = FakeBroker()
    report = _run(broker, _result(), tmp_path)
    kinds = {a.symbol: a.kind for a in report.actions}
    assert kinds["AAA"] == "closed" and kinds["BBB"] == "unfilled"
    state = at.load_state(tmp_path / "state.json")
    assert state["positions"] == {} and len(state["history"]) == 2


def test_time_exit_after_ten_trading_days(tmp_path):
    _seed(tmp_path, AAA=_pos("2026-10-14"), BBB=_pos("2026-10-20"))
    broker = FakeBroker(held={"AAA": 20, "BBB": 20, "ZZZ": 5}, working={"AAA", "BBB"})
    _run(broker, _result("CCC"), tmp_path, max_positions=2)
    assert broker.closed == [("AAA", 20)]
    state = at.load_state(tmp_path / "state.json")
    assert set(state["positions"]) == {"BBB", "CCC"}  # ZZZ is not ours and is left alone


def test_pending_entry_that_filled_becomes_open(tmp_path):
    _seed(tmp_path, AAA=_pos("2026-10-27", status="pending"))
    broker = FakeBroker(held={"AAA": 18}, working={"AAA"})
    _run(broker, _result("AAA", "BBB"), tmp_path)
    state = at.load_state(tmp_path / "state.json")
    assert state["positions"]["AAA"]["status"] == "open" and state["positions"]["AAA"]["shares"] == 18
    assert [b[0] for b in broker.brackets] == ["BBB"]
