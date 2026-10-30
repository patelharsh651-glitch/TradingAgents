"""Upcoming earnings dates from Yahoo Finance."""

from __future__ import annotations

from datetime import date

import yfinance as yf


def upcoming_earnings_dates(symbol: str) -> list[date]:
    """The earnings dates Yahoo lists for ``symbol``; empty when it lists none."""
    dates = (yf.Ticker(symbol).calendar or {}).get("Earnings Date") or []
    return [d for d in dates if isinstance(d, date)]
