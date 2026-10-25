# Polymarket Bitcoin 5-Minute Telegram Bot — Paper Trading

A local Python Telegram bot that discovers active Bitcoin 5-minute Up/Down markets and records simulated YES and NO buy-limit orders. Default prices are **$0.10 per share** for each outcome.

## Safety by design

- Paper trading only. The project contains no code to submit real orders.
- No wallet private key or Polymarket trading credentials are needed.
- `PAPER_MODE=false` causes startup to fail.
- Telegram commands are restricted to numeric user IDs in `TELEGRAM_ALLOWED_USER_IDS`.
- SQLite enforces a unique `(market_id, outcome)` pair to prevent duplicate simulated orders, including after restarts.
- Paper fills are approximated: a simulated buy order is marked filled when the public CLOB book's best ask is at or below the configured limit. This is only a simple simulation, not a guarantee of real-world execution.

## Requirements

- Python 3.10+ (Python 3.11 or 3.12 recommended)
- A Telegram bot token from Telegram's `@BotFather`
- Your numeric Telegram user ID

## Setup on Windows PowerShell

```powershell
mkdir polymarket-btc5m-bot
# Extract the ZIP into this directory, then:
cd polymarket_btc5m_telegram_bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python bot.py
```

If PowerShell blocks activation, you can use the virtual environment's interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bot.py
```

## Configure `.env`

1. Put your BotFather token in `TELEGRAM_BOT_TOKEN`.
2. Put your numeric Telegram user ID in `TELEGRAM_ALLOWED_USER_IDS`.
3. Keep `PAPER_MODE=true`.
4. Keep the default YES/NO prices at `0.10`, or change them.

Never commit `.env` to Git or share the bot token publicly. If a token is exposed, revoke it through BotFather and issue a new one.

## Telegram commands

- `/start` — show help
- `/startbot` — begin market discovery and paper order creation
- `/stopbot` — stop creating new simulated orders
- `/setprice 0.10` — set both YES and NO prices
- `/setprice 0.10 0.12` — set YES price, then NO price
- `/setshares 10` — set shares per outcome for each market
- `/status` — show running state, configuration and order counts
- `/orders` — show the latest 10 simulated orders
- `/help` — show help

At a price of $0.10 and 10 shares, the simulated order value is approximately $1 per outcome before any real-world fees (none are charged in this simulator).

## How it works

1. Polls Gamma's public market API every `POLL_SECONDS`.
2. Filters active markets for common BTC 5-minute Up/Down naming/wording.
3. Maps market outcome labels to CLOB token IDs.
4. Reads public order-book asks for informational paper-fill simulation.
5. Writes one simulated YES and one simulated NO order per discovered market.
6. Re-checks open simulated orders for the simple fill condition.

## Important limitations

- Market naming and Gamma response fields can change. Inspect logs if the bot finds no markets; the filter intentionally avoids guessing token mappings.
- The discovery call scans the first page of 500 active markets. If the desired market is not in that page, adapt the discovery query/pagination to current API behavior.
- Paper fills do not model queue position, latency, partial fills, fees, cancellation races, or order-book changes between polls.
- `/stopbot` prevents new simulated orders; it does not delete existing paper records.
- This project intentionally does not implement live trading. If live trading is added later, use the currently maintained official Polymarket SDK (`Polymarket/py-sdk`), follow its current documentation, keep signing keys outside Telegram, and add explicit live-mode safeguards.

## Troubleshooting

- **Bot exits with missing token/user ID:** complete the `.env` fields and restart.
- **Access denied:** confirm your Telegram numeric user ID is listed exactly in `.env`, then restart.
- **No markets found:** check logs and the public Gamma API. The market filter may need to be updated to reflect current slugs/questions.
- **Network/API errors:** the discovery loop logs the error and retries after the polling delay.
