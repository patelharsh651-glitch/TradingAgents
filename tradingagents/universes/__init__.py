"""Ticker universes the screener scans: the Nasdaq-100 and the S&P 500 (SPY).

Each universe is a CSV of ``ticker,company,sector`` shipped with the package.
The lists are a snapshot of the index constituents (Wikipedia, October 2026);
index committees rebalance them, so refresh the CSVs when a member changes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from importlib import resources

UNIVERSES = {
    "nasdaq100": "nasdaq100.csv",
    "sp500": "sp500.csv",
}

# Accepted spellings for each universe, so `--universe spy` or `qqq` work too.
_ALIASES = {
    "nasdaq": "nasdaq100", "nasdaq-100": "nasdaq100", "ndx": "nasdaq100", "qqq": "nasdaq100",
    "spy": "sp500", "s&p500": "sp500", "sp-500": "sp500", "s&p": "sp500", "spx": "sp500",
}

AS_OF = "2026-10-22"


@dataclass(frozen=True)
class Constituent:
    ticker: str
    company: str
    sector: str
    universes: tuple[str, ...]

    @property
    def yahoo_symbol(self) -> str:
        """The ticker as Yahoo spells it: share classes take a dash (BRK.B -> BRK-B)."""
        return self.ticker.replace(".", "-")


def resolve(name: str) -> str:
    """The canonical universe name for ``name``; raises ValueError if unknown."""
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in UNIVERSES:
        known = ", ".join([*UNIVERSES, "all"])
        raise ValueError(f"Unknown universe {name!r}; choose from {known}")
    return key


def load_universe(names: str | list[str] = "all") -> list[Constituent]:
    """The constituents of one or more universes, each ticker once, sorted.

    ``names`` is a universe name, a comma-separated string or a list of them;
    ``"all"`` is every universe. A ticker in several universes keeps them all.
    """
    if isinstance(names, str):
        names = [n for n in names.split(",") if n.strip()]
    keys: list[str] = []
    for name in names or ["all"]:
        if name.strip().lower() == "all":
            keys.extend(UNIVERSES)
        else:
            keys.append(resolve(name))

    rows: dict[str, dict] = {}
    for key in dict.fromkeys(keys):
        text = resources.files(__package__).joinpath(UNIVERSES[key]).read_text(encoding="utf-8")
        for row in csv.DictReader(text.splitlines()):
            ticker = row["ticker"].strip().upper()
            entry = rows.setdefault(ticker, {**row, "ticker": ticker, "universes": []})
            entry["universes"].append(key)
    return [
        Constituent(r["ticker"], r["company"], r["sector"], tuple(r["universes"]))
        for _, r in sorted(rows.items())
    ]
