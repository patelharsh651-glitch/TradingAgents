# Automated swing trading on an IBKR paper account

`tradingagents autotrade` runs one day of the swing-trade loop against Interactive Brokers through IB Gateway (or TWS). Schedule it for 9:30 AM US Eastern each weekday and it trades your **paper** account on its own.

## What one run does

1. **Paper check.** It reads the account IDs on the connection and stops with exit code 2 unless every one starts with `DU` (IBKR paper accounts). It never sends an order to a live account.
2. **Market check.** On a market holiday it does nothing.
3. **Exits.** For each position it opened earlier:
   - the stop and target are GTC orders resting at IBKR, so they fill during the day without this script running; the next run records the position as closed;
   - after 10 trading days with neither hit, it cancels the stop and target and sells at market (the time stop);
   - an entry that never filled is dropped.
4. **Entries.** If fewer than `--max-positions` (3) are open and SPY is above its 200-day average, it screens the Nasdaq-100 and S&P 500 with the rules in `tradingagents/screener.py` and, for the best setups, it:
   - skips a stock already up more than 3% on the day, or with earnings inside the next ~16 days;
   - re-plans from the live price: stop 1.5 ATR below, target 3 ATR above (2:1);
   - sizes so the stop loses at most `--risk` (1%, $100 on $10k) and a position uses at most a third of `--capital`;
   - sends a bracket: a DAY limit buy 0.2% above the price, with a GTC stop and a GTC target attached.

It only touches positions it opened. They are tracked in `~/.tradingagents/autotrade/state.json` and tagged `tradingagents-swing` at IBKR, so anything else in the paper account is left alone.

## One-time setup

1. **Paper login.** In IBKR Client Portal, open Settings, then Paper Trading Account, and note the paper username (and reset its password if needed). Turn on *Share real-time market data with paper account* there too, or quotes will be 15 minutes delayed.
2. **IB Gateway.** Install [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) and log in with the **paper** username, choosing *Paper Trading*. Then in Configure, Settings:
   - API, Settings: tick *Enable ActiveX and Socket Clients*, untick *Read-Only API*, socket port `4002`, trusted IP `127.0.0.1`;
   - Lock and Exit: set *Auto restart* so it stays logged in from day to day. IBKR still asks for a fresh login about once a week (after the Sunday reset); for a fully unattended login, look at [IBC](https://github.com/IbcAlpha/IBC).
3. **Install.** `pip install -e ".[ibkr]"` in this repo.
4. **Dry run.** With the Gateway running: `tradingagents autotrade --dry-run`. It prints the account ID (check it starts with `DU`), the exits and the orders it would send, and sends nothing.

## Schedule it

The command waits until 9:31 ET (`--not-before`) so the opening print is in, so scheduling it at 9:30 is fine. The times below are your computer's local time: use 9:30 if it is on Eastern time (New York, Toronto), and convert otherwise.

**Windows** (Command Prompt, once):

```bat
schtasks /Create /TN "TradingAgents autotrade" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:30 ^
  /TR "cmd /c tradingagents autotrade >> %USERPROFILE%\tradingagents-autotrade.log 2>&1"
```

**macOS / Linux** (`crontab -e`):

```cron
30 9 * * 1-5  cd ~/tradingagents && tradingagents autotrade >> ~/tradingagents-autotrade.log 2>&1
```

On Linux you can put `CRON_TZ=America/New_York` above that line to pin it to Eastern time. The computer has to be awake and IB Gateway logged in at 9:30.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--capital` | 10000 | Dollars the loop trades with (the paper account has more; this caps it) |
| `--risk` | 1 | % of capital one position may lose at its stop |
| `--max-positions` | 3 | Positions open at once |
| `--port` | 4002 | 4002 is IB Gateway paper, 7497 is TWS paper |
| `--dry-run` | off | Show the plan, send nothing, keep no state |
| `--state` | `~/.tradingagents/autotrade/state.json` | Where open positions and history are kept |

$100 a day on $10,000 is 1% a day, an aggressive stretch target rather than something to expect daily. Stops are part of the plan. Paper trading only; this is not financial advice.
