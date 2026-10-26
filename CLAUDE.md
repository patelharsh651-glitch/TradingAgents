# Notes for Claude

## "Analyse stock"

When the user asks to "analyse stock" (or "analyze stocks", "top 3 for swing trade") without naming a ticker, run the swing-trade screen rather than a single-ticker analysis:

```bash
tradingagents analyse-stock --capital 10000 --daily-target 100 --top 3
```

The defaults are the user's: $10,000 capital, a $100/day goal, the top 3 picks from the Nasdaq-100 and S&P 500. Report each pick's entry, stop, target, share count and risk, the market regime line, and the goal notes. Always say that $100/day on $10k (1%/day) is aggressive, that stops are part of the plan, and that this is not financial advice. Add `--deep` when the user wants the analyst agents to review the picks (needs an LLM key). A named ticker ("analyse NVDA") is a normal run: `tradingagents --ticker NVDA`.

The screen reads prices from Yahoo Finance, so the machine running it needs access to `query1.finance.yahoo.com` / `query2.finance.yahoo.com`.
