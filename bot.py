import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("btc5m-paper-portfolio")

GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com").rstrip("/")
POLL_SECONDS = max(2, int(os.getenv("POLL_SECONDS", "5")))
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(item.strip())
    for item in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if item.strip().isdigit()
}
STARTING_CASH = Decimal(os.getenv("STARTING_CASH", "1000"))
DEFAULT_LIMIT_PRICE = Decimal(os.getenv("LIMIT_PRICE", "0.10"))
DEFAULT_SHARES = Decimal(os.getenv("SHARES_PER_ORDER", "10"))
PAPER_MODE = os.getenv("PAPER_MODE", "true").strip().lower() == "true"

# This project intentionally has no real-order submission path.
if not PAPER_MODE:
    raise RuntimeError("Paper-only simulator: keep PAPER_MODE=true. Live trading is not implemented.")
if not Decimal("0.01") <= DEFAULT_LIMIT_PRICE <= Decimal("0.99"):
    raise RuntimeError("LIMIT_PRICE must be between 0.01 and 0.99 (for example 0.10).")
if STARTING_CASH <= 0 or DEFAULT_SHARES <= 0:
    raise RuntimeError("STARTING_CASH and SHARES_PER_ORDER must be positive.")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def flag_is_true(value: Any) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"})


def flag_is_false(value: Any) -> bool:
    return value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in {"false", "0", "no"})


def event_window_et(start_ts: int, end_ts: int | None = None) -> str:
    """Render the event's actual Unix-time window in US Eastern time (DST-aware)."""
    start = datetime.fromtimestamp(int(start_ts), ZoneInfo("America/New_York"))
    end = datetime.fromtimestamp(int(end_ts if end_ts is not None else start_ts + 300), ZoneInfo("America/New_York"))
    start_clock = start.strftime("%I:%M").lstrip("0")
    end_clock = end.strftime("%I:%M").lstrip("0")
    if start.strftime("%p") == end.strftime("%p"):
        clock = f"{start_clock}–{end_clock} {end.strftime('%p')} ET"
    else:
        clock = f"{start_clock} {start.strftime('%p')}–{end_clock} {end.strftime('%p')} ET"
    return f"{clock} · {start.strftime('%b %d')}"


def event_window_for_slug(slug: str) -> str:
    try:
        start_ts = int(slug.rsplit("-", 1)[1])
        return event_window_et(start_ts, start_ts + 300)
    except (ValueError, IndexError, OverflowError, OSError):
        return "time unavailable · ET"


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else Decimal(default)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def parse_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def connect_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """Create portfolio tables and safely move the older demo order table aside if its schema differs."""
    with connect_db() as con:
        existing = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='simulated_orders'").fetchone()
        if existing:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(simulated_orders)").fetchall()}
            required = {"requested_shares", "filled_shares", "fill_cost", "canceled_shares", "status", "market_slug", "outcome"}
            if not required.issubset(cols):
                backup_name = f"simulated_orders_legacy_{int(time.time())}"
                con.execute(f"ALTER TABLE simulated_orders RENAME TO {backup_name}")
                log.warning("Moved old paper-order table to %s; the new portfolio starts with fresh simulated orders.", backup_name)

        con.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracked_events (
                slug TEXT PRIMARY KEY,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'TRACKING',
                question TEXT NOT NULL DEFAULT '',
                last_detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS simulated_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                outcome TEXT NOT NULL,
                token_id TEXT NOT NULL,
                limit_price TEXT NOT NULL,
                requested_shares TEXT NOT NULL,
                filled_shares TEXT NOT NULL DEFAULT '0',
                fill_cost TEXT NOT NULL DEFAULT '0',
                canceled_shares TEXT NOT NULL DEFAULT '0',
                payout TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL DEFAULT 'OPEN',
                last_mark_bid TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(market_slug, outcome)
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                market_slug TEXT NOT NULL,
                outcome TEXT NOT NULL,
                price TEXT NOT NULL,
                shares TEXT NOT NULL,
                cost TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES simulated_orders(id)
            );
            CREATE TABLE IF NOT EXISTS settlements (
                market_slug TEXT NOT NULL,
                outcome TEXT NOT NULL,
                winning_outcome TEXT NOT NULL,
                filled_shares TEXT NOT NULL,
                payout TEXT NOT NULL,
                cost_basis TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                settled_at TEXT NOT NULL,
                PRIMARY KEY(market_slug, outcome)
            );
            CREATE TABLE IF NOT EXISTS book_levels (
                token_id TEXT NOT NULL,
                price TEXT NOT NULL,
                size TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(token_id, price)
            );
            CREATE TABLE IF NOT EXISTS sent_notifications (
                notification_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
        """)
        defaults = {
            "running": "0",
            "limit_price": str(DEFAULT_LIMIT_PRICE),
            "shares": str(DEFAULT_SHARES),
            "starting_cash": str(STARTING_CASH),
        }
        for key, value in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
        # Persist initial balance once; subsequent restarts do not reset the portfolio.
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('portfolio_initialized', '1')")
        con.commit()


def get_setting(key: str) -> str:
    with connect_db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_setting(key: str, value: str) -> None:
    with connect_db() as con:
        con.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()


def next_upcoming_start(now_ts: int | None = None) -> int:
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    return (now_ts // 300 + 1) * 300


def slug_for_start(start_ts: int) -> str:
    return f"btc-updown-5m-{int(start_ts)}"


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS)


async def reply(update: Update, message: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(message, parse_mode="HTML")


async def require_auth(update: Update) -> bool:
    if authorized(update):
        return True
    await reply(update, "Access denied. Set your numeric Telegram user ID in TELEGRAM_ALLOWED_USER_IDS in .env, then restart the bot.")
    return False


async def notify_admins(app: Application, notification_key: str, message: str) -> None:
    """Send a concise Telegram notification once, persisting dedupe keys across restarts."""
    if not ALLOWED_USER_IDS:
        return
    with connect_db() as con:
        already_sent = con.execute(
            "SELECT 1 FROM sent_notifications WHERE notification_key=?",
            (notification_key,),
        ).fetchone()
        if already_sent:
            return
    sent_any = False
    for user_id in ALLOWED_USER_IDS:
        try:
            await app.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            sent_any = True
        except Exception:
            log.exception("Telegram notification failed for key %s and user %s", notification_key, user_id)
    if sent_any:
        with connect_db() as con:
            con.execute(
                "INSERT OR IGNORE INTO sent_notifications(notification_key, sent_at) VALUES(?, ?)",
                (notification_key, now_iso()),
            )
            con.commit()


def money(value: Decimal) -> str:
    return f"${value:.2f}"


def market_outcomes(market: dict) -> list[str]:
    return [str(x).strip() for x in parse_json_list(market.get("outcomes"))]


def token_map(market: dict) -> dict[str, str]:
    outcomes = market_outcomes(market)
    token_ids = parse_json_list(market.get("clobTokenIds"))
    if not outcomes or len(outcomes) != len(token_ids):
        return {}
    result = {}
    for label, token in zip(outcomes, token_ids):
        name = label.strip().lower()
        if name in {"up", "yes"}:
            result["YES"] = str(token)
        elif name in {"down", "no"}:
            result["NO"] = str(token)
    return result if set(result) == {"YES", "NO"} else {}


async def get_market(session: aiohttp.ClientSession, slug: str) -> dict | None:
    url = f"{GAMMA_API}/markets/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("Gamma lookup failed for %s: %s", slug, exc)
        return None


async def get_book(session: aiohttp.ClientSession, token_id: str) -> dict | None:
    try:
        async with session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.debug("CLOB book unavailable for %s: %s", token_id, exc)
        return None


def parse_levels(book: dict, side: str) -> list[tuple[Decimal, Decimal]]:
    levels = []
    for level in book.get(side, []) or []:
        if not isinstance(level, dict):
            continue
        price = dec(level.get("price"), "-1")
        size = dec(level.get("size"), "0")
        if Decimal("0") < price < Decimal("1.01") and size > 0:
            levels.append((price, size))
    return sorted(levels, key=lambda pair: pair[0], reverse=(side == "bids"))


def get_previous_level_sizes(con: sqlite3.Connection, token_id: str) -> dict[str, Decimal]:
    rows = con.execute("SELECT price, size FROM book_levels WHERE token_id=?", (token_id,)).fetchall()
    return {str(r["price"]): dec(r["size"]) for r in rows}


def save_level_snapshot(con: sqlite3.Connection, token_id: str, asks: list[tuple[Decimal, Decimal]]) -> None:
    now = now_iso()
    current = {str(price): size for price, size in asks}
    previous = get_previous_level_sizes(con, token_id)
    for price in set(previous) - set(current):
        con.execute("UPDATE book_levels SET size='0', updated_at=? WHERE token_id=? AND price=?", (now, token_id, price))
    for price, size in current.items():
        con.execute("""INSERT INTO book_levels(token_id, price, size, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(token_id, price) DO UPDATE SET size=excluded.size, updated_at=excluded.updated_at""",
            (token_id, price, str(size), now))


def event_exists(slug: str) -> bool:
    with connect_db() as con:
        return con.execute("SELECT 1 FROM tracked_events WHERE slug=?", (slug,)).fetchone() is not None


def track_upcoming_event() -> None:
    start = next_upcoming_start()
    slug = slug_for_start(start)
    now = now_iso()
    with connect_db() as con:
        con.execute("INSERT OR IGNORE INTO tracked_events(slug, start_ts, end_ts, state, created_at, updated_at) VALUES(?, ?, ?, 'TRACKING', ?, ?)",
                    (slug, start, start + 300, now, now))
        con.commit()


def open_shares(row: sqlite3.Row) -> Decimal:
    return max(Decimal("0"), dec(row["requested_shares"]) - dec(row["filled_shares"]) - dec(row["canceled_shares"]))


def order_reservations(con: sqlite3.Connection) -> Decimal:
    rows = con.execute("SELECT * FROM simulated_orders WHERE status IN ('OPEN','PARTIAL')").fetchall()
    return sum((open_shares(row) * dec(row["limit_price"]) for row in rows), Decimal("0"))


def portfolio_cash(con: sqlite3.Connection) -> Decimal:
    starting = dec(get_setting("starting_cash"), "1000")
    spent = sum((dec(r[0]) for r in con.execute("SELECT cost FROM fills").fetchall()), Decimal("0"))
    payouts = sum((dec(r[0]) for r in con.execute("SELECT payout FROM settlements").fetchall()), Decimal("0"))
    return starting - spent + payouts


def create_orders_for_market(market: dict, slug: str) -> str:
    tokens = token_map(market)
    if not tokens:
        return "Market found but outcomes/token IDs could not be mapped to Up/Down."
    price = dec(get_setting("limit_price"), "0.10")
    shares = dec(get_setting("shares"), "10")
    market_id = str(market.get("id") or market.get("conditionId") or slug)
    created = []
    with connect_db() as con:
        already = {r["outcome"] for r in con.execute("SELECT outcome FROM simulated_orders WHERE market_slug=?", (slug,)).fetchall()}
        needed = [outcome for outcome in ("YES", "NO") if outcome not in already]
        reserve_needed = price * shares * len(needed)
        cash = portfolio_cash(con)
        reserved = order_reservations(con)
        if cash - reserved < reserve_needed:
            return f"Insufficient virtual available cash: need ${reserve_needed:.2f}, available ${cash-reserved:.2f}. No new orders created."
        now = now_iso()
        for outcome in needed:
            con.execute("""INSERT OR IGNORE INTO simulated_orders
                (market_slug, market_id, question, outcome, token_id, limit_price, requested_shares,
                 filled_shares, fill_cost, canceled_shares, payout, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '0', '0', '0', '0', 'OPEN', ?, ?)""",
                (slug, market_id, str(market.get("question", "")), outcome, tokens[outcome], str(price), str(shares), now, now))
            if con.execute("SELECT changes()").fetchone()[0]:
                created.append(outcome)
        con.execute("UPDATE tracked_events SET question=?, state='ACTIVE', last_detail=?, updated_at=? WHERE slug=?",
                    (str(market.get("question", "")), "Orders created: " + ", ".join(created), now, slug))
        con.commit()
    return f"Created simulated orders for {slug}: {', '.join(created) if created else 'orders already existed'}"


async def fill_open_orders(session: aiohttp.ClientSession, slug: str) -> list[dict]:
    """Simulate fills from newly observed eligible ask liquidity; returns newly recorded fill events."""
    notifications: list[dict] = []
    with connect_db() as con:
        rows = con.execute(
            "SELECT * FROM simulated_orders WHERE market_slug=? AND status IN ('OPEN','PARTIAL') ORDER BY id",
            (slug,),
        ).fetchall()
    for row in rows:
        qty_open = open_shares(row)
        if qty_open <= 0:
            continue
        book = await get_book(session, str(row["token_id"]))
        if book is None:
            continue
        asks = parse_levels(book, "asks")
        bids = parse_levels(book, "bids")
        best_bid = bids[0][0] if bids else None
        limit_price = dec(row["limit_price"])
        with connect_db() as con:
            # REST snapshots cannot reveal queue priority or prove that a trade matched our order.
            # To avoid repeatedly consuming the same unchanged displayed liquidity, only count
            # newly observed incremental ask size at eligible prices. This is a conservative
            # simulation heuristic, not an exchange-confirmed fill.
            previous = get_previous_level_sizes(con, str(row["token_id"]))
            new_liquidity = []
            for price, size in asks:
                prior_size = previous.get(str(price), Decimal("0"))
                incremental = max(Decimal("0"), size - prior_size)
                if price <= limit_price and incremental > 0:
                    new_liquidity.append((price, incremental))
            save_level_snapshot(con, str(row["token_id"]), asks)
            remaining = qty_open
            total_cost = Decimal("0")
            total_filled = Decimal("0")
            fill_rows = []
            for price, liquidity in sorted(new_liquidity, key=lambda x: x[0]):
                if remaining <= 0:
                    break
                take = min(remaining, liquidity)
                if take <= 0:
                    continue
                cost = take * price
                total_filled += take
                total_cost += cost
                remaining -= take
                fill_rows.append((price, take, cost))
            if total_filled > 0:
                new_filled = dec(row["filled_shares"]) + total_filled
                new_cost = dec(row["fill_cost"]) + total_cost
                new_open = dec(row["requested_shares"]) - new_filled - dec(row["canceled_shares"])
                status = "FILLED" if new_open <= 0 else "PARTIAL"
                now = now_iso()
                con.execute(
                    "UPDATE simulated_orders SET filled_shares=?, fill_cost=?, status=?, last_mark_bid=?, updated_at=? WHERE id=?",
                    (str(new_filled), str(new_cost), status, str(best_bid) if best_bid is not None else None, now, row["id"]),
                )
                inserted_fill_ids = []
                for price, qty, cost in fill_rows:
                    cur = con.execute(
                        "INSERT INTO fills(order_id, market_slug, outcome, price, shares, cost, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (row["id"], slug, row["outcome"], str(price), str(qty), str(cost), now),
                    )
                    inserted_fill_ids.append(cur.lastrowid)
                con.commit()
                log.info("PAPER FILL %s %s: %s shares, cost $%s; status=%s", slug, row["outcome"], total_filled, total_cost, status)
                avg_price = total_cost / total_filled if total_filled else Decimal("0")
                notifications.append({
                    "key": f"fill:{inserted_fill_ids[0]}",
                    "outcome": str(row["outcome"]),
                    "new_shares": total_filled,
                    "avg_price": avg_price,
                    "total_filled": new_filled,
                    "requested": dec(row["requested_shares"]),
                    "remaining": max(Decimal("0"), new_open),
                    "status": status,
                    "slug": slug,
                })
            else:
                con.execute(
                    "UPDATE simulated_orders SET last_mark_bid=?, updated_at=? WHERE id=?",
                    (str(best_bid) if best_bid is not None else None, now_iso(), row["id"]),
                )
                con.commit()
    return notifications

async def refresh_position_marks(session: aiohttp.ClientSession) -> None:
    """Refresh stored best-bid marks for all unsettled filled positions."""
    with connect_db() as con:
        rows = con.execute("""SELECT id, token_id FROM simulated_orders
            WHERE status != 'SETTLED' AND CAST(filled_shares AS REAL) > 0""").fetchall()
    for row in rows:
        book = await get_book(session, str(row["token_id"]))
        if book is None:
            continue
        bids = parse_levels(book, "bids")
        best_bid = bids[0][0] if bids else None
        with connect_db() as con:
            con.execute("UPDATE simulated_orders SET last_mark_bid=?, updated_at=? WHERE id=?",
                        (str(best_bid) if best_bid is not None else None, now_iso(), row["id"]))
            con.commit()


def cancel_open_orders_for_event(slug: str, reason: str) -> int:
    canceled_count = 0
    now = now_iso()
    with connect_db() as con:
        rows = con.execute("SELECT * FROM simulated_orders WHERE market_slug=? AND status IN ('OPEN','PARTIAL')", (slug,)).fetchall()
        for row in rows:
            remaining = open_shares(row)
            if remaining <= 0:
                continue
            new_canceled = dec(row["canceled_shares"]) + remaining
            status = "PARTIAL_CANCELLED" if dec(row["filled_shares"]) > 0 else "CANCELLED"
            con.execute("UPDATE simulated_orders SET canceled_shares=?, status=?, updated_at=? WHERE id=?",
                        (str(new_canceled), status, now, row["id"]))
            canceled_count += 1
        con.execute("UPDATE tracked_events SET state=CASE WHEN state='SETTLED' THEN state ELSE 'AWAITING_RESOLUTION' END, last_detail=?, updated_at=? WHERE slug=?",
                    (f"Event trading window ended; canceled {canceled_count} remaining order(s): {reason}", now, slug))
        con.commit()
    if canceled_count:
        log.info("Canceled remaining paper orders for %s (%s order rows): %s", slug, canceled_count, reason)
    return canceled_count


def resolved_winner(market: dict) -> str | None:
    """Only accept an official closed market with unambiguous 1/0 outcome prices."""
    if not flag_is_true(market.get("closed")):
        return None
    outcomes = market_outcomes(market)
    prices = parse_json_list(market.get("outcomePrices"))
    if len(outcomes) != len(prices) or len(outcomes) < 2:
        return None
    winner = None
    for label, raw_price in zip(outcomes, prices):
        p = dec(raw_price, "-1")
        normalized = label.strip().lower()
        mapped = "YES" if normalized in {"up", "yes"} else "NO" if normalized in {"down", "no"} else None
        if mapped and p >= Decimal("0.99"):
            if winner is not None:
                return None
            winner = mapped
        elif p > Decimal("0.01") and p < Decimal("0.99"):
            return None
    return winner


def settle_event(slug: str, market: dict) -> str | None:
    winner = resolved_winner(market)
    if winner is None:
        return None
    now = now_iso()
    with connect_db() as con:
        state_row = con.execute("SELECT state FROM tracked_events WHERE slug=?", (slug,)).fetchone()
        if not state_row or state_row["state"] == "SETTLED":
            return None
        # Resolution can arrive before our loop has marked the event ended; cancel leftovers first.
        rows = con.execute("SELECT * FROM simulated_orders WHERE market_slug=?", (slug,)).fetchall()
        for row in rows:
            if row["status"] in ("OPEN", "PARTIAL"):
                remaining = open_shares(row)
                if remaining > 0:
                    status = "PARTIAL_CANCELLED" if dec(row["filled_shares"]) > 0 else "CANCELLED"
                    con.execute("UPDATE simulated_orders SET canceled_shares=?, status=?, updated_at=? WHERE id=?",
                                (str(dec(row["canceled_shares"]) + remaining), status, now, row["id"]))
        total_pnl = Decimal("0")
        for row in rows:
            already = con.execute("SELECT 1 FROM settlements WHERE market_slug=? AND outcome=?", (slug, row["outcome"])).fetchone()
            if already:
                continue
            filled = dec(row["filled_shares"])
            cost = dec(row["fill_cost"])
            payout = filled if row["outcome"] == winner else Decimal("0")
            pnl = payout - cost
            con.execute("INSERT INTO settlements(market_slug, outcome, winning_outcome, filled_shares, payout, cost_basis, realized_pnl, settled_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (slug, row["outcome"], winner, str(filled), str(payout), str(cost), str(pnl), now))
            con.execute("UPDATE simulated_orders SET payout=?, status='SETTLED', updated_at=? WHERE id=?",
                        (str(payout), now, row["id"]))
            total_pnl += pnl
        con.execute("UPDATE tracked_events SET state='SETTLED', last_detail=?, updated_at=? WHERE slug=?",
                    (f"Official result: {winner}; realized P&L for this event: ${total_pnl:.4f}", now, slug))
        con.commit()
    log.info("SETTLED %s winner=%s event_pnl=$%s", slug, winner, total_pnl)
    return f"{slug} settled: {winner}; event realized P&L ${total_pnl:.4f}"


async def process_tracked_events(app: Application, session: aiohttp.ClientSession) -> None:
    now_ts = int(time.time())
    with connect_db() as con:
        events = con.execute("SELECT * FROM tracked_events WHERE state != 'SETTLED' ORDER BY start_ts").fetchall()

    for event in events:
        slug = str(event["slug"])
        start_ts = int(event["start_ts"])
        end_ts = int(event["end_ts"])
        window = event_window_et(start_ts, end_ts)
        market = await get_market(session, slug)
        # A /reset command may clear the tracked event while this network request is in flight.
        if not event_exists(slug):
            continue

        if market is None:
            if now_ts >= end_ts:
                prior_state = str(event["state"])
                canceled = cancel_open_orders_for_event(slug, "scheduled event end passed; market metadata not yet available")
                if prior_state not in ("AWAITING_RESOLUTION", "SETTLED"):
                    await notify_admins(
                        app, f"closed:{slug}",
                        f"⏹️ <b>BTC 5M CLOSED</b>\n🕒 {window}\n🧹 Cancelled: <b>{canceled}</b> open order(s)\n⏳ Waiting for result · 🧪 Paper",
                    )
            continue

        tokens = token_map(market)
        is_closed = flag_is_true(market.get("closed"))
        # A definitive market-closed flag means stop remaining paper orders immediately,
        # even if the scheduled five-minute end has not yet arrived.
        if is_closed or now_ts >= end_ts:
            prior_state = str(event["state"])
            reason = "Polymarket marked market closed" if is_closed else "scheduled 5-minute trading window ended"
            canceled = cancel_open_orders_for_event(slug, reason)
            if prior_state not in ("AWAITING_RESOLUTION", "SETTLED"):
                await notify_admins(
                    app, f"closed:{slug}",
                    f"⏹️ <b>BTC 5M CLOSED</b>\n🕒 {window}\n🧹 Cancelled: <b>{canceled}</b> open order(s)\n⏳ Waiting for result · 🧪 Paper",
                )
            result = settle_event(slug, market)
            if result:
                log.info(result)
                with connect_db() as con:
                    settlements = con.execute(
                        "SELECT outcome, winning_outcome, filled_shares, payout, cost_basis, realized_pnl FROM settlements WHERE market_slug=? ORDER BY outcome",
                        (slug,),
                    ).fetchall()
                    cash_now = portfolio_cash(con)
                pnl = sum((dec(r["realized_pnl"]) for r in settlements), Decimal("0"))
                winner = settlements[0]["winning_outcome"] if settlements else "UNKNOWN"
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                await notify_admins(
                    app, f"settled:{slug}",
                    f"🏁 <b>BTC 5M SETTLED</b>\n🕒 {window}\n🏆 Winner: <b>{winner}</b>\n{pnl_icon} P&amp;L: <b>{'+' if pnl >= 0 else ''}{money(pnl)}</b>\n💼 Cash: <b>{money(cash_now)}</b>\n🧪 Paper",
                )
            continue

        eligible = (
            not flag_is_true(market.get("closed"))
            and not flag_is_false(market.get("active"))
            and not flag_is_false(market.get("acceptingOrders"))
            and not flag_is_false(market.get("enableOrderBook"))
        )
        # Keep retrying the same upcoming slug if Gamma has not published it yet.
        # Never create orders after the event's scheduled end or once it is marked closed.
        if eligible and tokens:
            with connect_db() as con:
                con.execute(
                    "UPDATE tracked_events SET question=?, updated_at=? WHERE slug=?",
                    (str(market.get("question", "")), now_iso(), slug),
                )
                con.commit()
            with connect_db() as con:
                before = con.execute("SELECT COUNT(*) FROM simulated_orders WHERE market_slug=?", (slug,)).fetchone()[0]
            if before == 0 and get_setting("running") == "1":
                result = create_orders_for_market(market, slug)
                log.info("%s", result)
                with connect_db() as con:
                    created_rows = con.execute(
                        "SELECT outcome, limit_price, requested_shares FROM simulated_orders WHERE market_slug=? ORDER BY outcome",
                        (slug,),
                    ).fetchall()
                if created_rows:
                    await notify_admins(
                        app, f"placed:{slug}",
                        f"🟢 <b>BTC 5M · ORDERS READY</b>\n🕒 {window}\n⬆️ YES · {get_setting('shares')} @ ${get_setting('limit_price')}\n⬇️ NO · {get_setting('shares')} @ ${get_setting('limit_price')}\n💰 Max cost: {money(dec(get_setting('shares')) * dec(get_setting('limit_price')) * 2)}\n🧪 Paper mode",
                    )
            # Pausing prevents new orders but still manages any existing paper orders/positions.
            fill_events = await fill_open_orders(session, slug)
            for fill in fill_events:
                title = "✅ <b>FILLED</b>" if fill["status"] == "FILLED" else "🟡 <b>PARTIAL FILL</b>"
                await notify_admins(
                    app, fill["key"],
                    f"{title} · <b>{fill['outcome']}</b>\n🕒 {window}\n➕ {fill['new_shares']:g} @ ${fill['avg_price']:.3f}\n📦 {fill['total_filled']:g}/{fill['requested']:g} shares\n💸 Cost: {money(fill['new_shares'] * fill['avg_price'])}\n🧪 Paper",
                )
        elif not eligible:
            log.debug("Market %s is not currently accepting orders.", slug)

def portfolio_summary() -> str:
    with connect_db() as con:
        cash = portfolio_cash(con)
        reserved = order_reservations(con)
        rows = con.execute("SELECT * FROM simulated_orders WHERE status != 'SETTLED' AND CAST(filled_shares AS REAL) > 0").fetchall()
        mark_value = Decimal("0")
        cost_basis = Decimal("0")
        for row in rows:
            qty = dec(row["filled_shares"])
            cost_basis += dec(row["fill_cost"])
            bid = dec(row["last_mark_bid"], "-1") if row["last_mark_bid"] is not None else Decimal("-1")
            if bid >= 0:
                mark_value += qty * bid
        unrealized = mark_value - cost_basis
        realized = sum((dec(r[0]) for r in con.execute("SELECT realized_pnl FROM settlements").fetchall()), Decimal("0"))
        total_equity = cash + mark_value
        open_positions = con.execute("SELECT COUNT(*) FROM simulated_orders WHERE status != 'SETTLED' AND CAST(filled_shares AS REAL) > 0").fetchone()[0]
        open_orders = con.execute("SELECT COUNT(*) FROM simulated_orders WHERE status IN ('OPEN','PARTIAL')").fetchone()[0]
    return (
        "💼 <b>VIRTUAL PORTFOLIO</b>\n"
        f"💵 Cash: <b>{money(cash)}</b>\n"
        f"🔒 Reserved: {money(reserved)} · Available: <b>{money(cash-reserved)}</b>\n"
        f"📊 Equity est.: <b>{money(total_equity)}</b>\n"
        f"{ '🟢' if realized >= 0 else '🔴' } Realized P&amp;L: <b>{'+' if realized >= 0 else ''}{money(realized)}</b>\n"
        f"📈 Unrealized P&amp;L est.: {'+' if unrealized >= 0 else ''}{money(unrealized)}\n"
        f"📦 Open orders: {open_orders} · Positions: {open_positions}\n"
        "🧪 Paper mode · equity is an estimate"
    )


def orders_summary() -> str:
    with connect_db() as con:
        rows = con.execute("SELECT market_slug, outcome, limit_price, requested_shares, filled_shares, canceled_shares, status, fill_cost FROM simulated_orders ORDER BY id DESC LIMIT 12").fetchall()
    if not rows:
        return "No simulated orders recorded yet. Use /startbot to begin monitoring."
    lines = ["📋 <b>LATEST PAPER ORDERS</b>"]
    for r in rows:
        remaining = max(Decimal("0"), dec(r["requested_shares"])-dec(r["filled_shares"])-dec(r["canceled_shares"]))
        lines.append(f"<b>{r['outcome']}</b> · {r['status']}\n🕒 {event_window_for_slug(r['market_slug'])}\n📦 {r['filled_shares']}/{r['requested_shares']} filled · {remaining} open\n💵 Limit ${r['limit_price']} · Cost {money(dec(r['fill_cost']))}")
    return "\n\n".join(lines)


def positions_summary() -> str:
    with connect_db() as con:
        rows = con.execute("SELECT market_slug, outcome, filled_shares, fill_cost, last_mark_bid, status FROM simulated_orders WHERE status != 'SETTLED' AND CAST(filled_shares AS REAL) > 0 ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return "No unsettled filled positions."
    lines = ["📈 <b>UNSETTLED POSITIONS</b>"]
    for r in rows:
        qty = dec(r["filled_shares"])
        cost = dec(r["fill_cost"])
        bid = dec(r["last_mark_bid"], "-1") if r["last_mark_bid"] is not None else Decimal("-1")
        mark = f"${qty*bid:.4f}" if bid >= 0 else "not available"
        pnl = f"${qty*bid-cost:.4f}" if bid >= 0 else "not available"
        lines.append(f"<b>{r['outcome']}</b> · {qty} shares · {r['status']}\n🕒 {event_window_for_slug(r['market_slug'])}\n💵 Cost {money(cost)} · Mark {mark} · P&amp;L {pnl}")
    return "\n\n".join(lines)


def history_summary() -> str:
    with connect_db() as con:
        rows = con.execute("SELECT market_slug, winning_outcome, SUM(CAST(payout AS REAL)) payout, SUM(CAST(cost_basis AS REAL)) cost, SUM(CAST(realized_pnl AS REAL)) pnl, MAX(settled_at) settled_at FROM settlements GROUP BY market_slug ORDER BY MAX(settled_at) DESC LIMIT 15").fetchall()
    if not rows:
        return "No officially settled paper events yet."
    lines = ["🏁 <b>SETTLEMENT HISTORY</b>"]
    for r in rows:
        lines.append(f"🕒 {event_window_for_slug(r['market_slug'])}\n🏆 Winner: <b>{r['winning_outcome']}</b> · P&amp;L {money(dec(r['pnl']))}\n💵 Cost {money(dec(r['cost']))} · Payout {money(dec(r['payout']))}")
    return "\n\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, "₿ <b>BTC 5M PAPER BOT</b>\n💵 Virtual balance: $1,000\n📈 YES + 📉 NO · limit orders\n🧪 No real trades\n\n/startbot · /stopbot · /status · /portfolio · /orders · /positions · /history · /setprice 0.10 · /setshares 10 · /reset · /help")


async def cmd_startbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    set_setting("running", "1")
    await reply(update, "🟢 <b>Bot started</b>\nWatching upcoming BTC 5M markets. Paper orders only.")


async def cmd_stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    set_setting("running", "0")
    await reply(update, "⏸️ <b>Bot paused</b>\nNo new events will be tracked. Existing paper positions will still be managed.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    with connect_db() as con:
        total = con.execute("SELECT COUNT(*) FROM simulated_orders").fetchone()[0]
        open_count = con.execute("SELECT COUNT(*) FROM simulated_orders WHERE status IN ('OPEN','PARTIAL')").fetchone()[0]
        fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        tracked = con.execute("SELECT COUNT(*) FROM tracked_events").fetchone()[0]
        settled = con.execute("SELECT COUNT(*) FROM tracked_events WHERE state='SETTLED'").fetchone()[0]
    next_start = next_upcoming_start()
    await reply(update, f"🤖 <b>BOT STATUS</b>\n🕒 Next: {event_window_et(next_start, next_start + 300)}\nState: {'🟢 Running' if get_setting('running') == '1' else '⏸️ Paused'} · 🧪 Paper\n💲 Limit: ${get_setting('limit_price')} · Shares/side: {get_setting('shares')}\n📦 Orders: {total} · Open: {open_count} · Fills: {fills}\n🏁 Events: {tracked} · Settled: {settled}\n⏱ Poll: {POLL_SECONDS}s")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, portfolio_summary())


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, orders_summary())


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, positions_summary())


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, history_summary())


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    if len(context.args) != 1:
        await reply(update, "Usage: /setprice 0.10")
        return
    try:
        price = dec(context.args[0], "-1")
        if not Decimal("0.01") <= price <= Decimal("0.99"):
            raise ValueError
    except ValueError:
        await reply(update, "Price must be between 0.01 and 0.99, for example /setprice 0.10")
        return
    set_setting("limit_price", str(price))
    await reply(update, f"Limit price updated to ${price} per share for new paper orders on both outcomes.")


async def cmd_setshares(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    if len(context.args) != 1:
        await reply(update, "Usage: /setshares 10")
        return
    try:
        shares = dec(context.args[0], "-1")
        if shares <= 0 or shares > Decimal("100000"):
            raise ValueError
    except ValueError:
        await reply(update, "Shares must be greater than 0 and at most 100000.")
        return
    set_setting("shares", str(shares))
    await reply(update, f"New paper orders will request {shares} shares on both outcomes.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update):
        return
    if not context.args or context.args[0].lower() != "confirm":
        await reply(
            update,
            "⚠️ <b>Reset paper portfolio?</b>\nThis deletes all simulated orders, fills, positions and settlement history, and restores the starting balance.\n\nSend <code>/reset confirm</code> to proceed.\nReal Polymarket orders are not affected.",
        )
        return
    # Serialize reset against worker operations so an in-flight API response cannot recreate old records.
    lock = context.application.bot_data.get("reset_lock")
    if lock is None:
        lock = asyncio.Lock()
        context.application.bot_data["reset_lock"] = lock
    async with lock:
        set_setting("running", "0")
        with connect_db() as con:
            for table in ("fills", "settlements", "simulated_orders", "tracked_events", "book_levels", "sent_notifications"):
                con.execute(f"DELETE FROM {table}")
            con.execute("UPDATE settings SET value=? WHERE key='starting_cash'", (str(STARTING_CASH),))
            con.commit()
    await reply(
        update,
        f"♻️ <b>Portfolio reset!</b>\n💵 Balance: <b>{money(STARTING_CASH)}</b>\n🧹 Orders, fills, positions &amp; history cleared.\n⏸️ Bot stopped. Use /startbot when ready.\n🧪 Paper mode only.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update): return
    await reply(update, "🧭 <b>COMMANDS</b>\n/startbot · /stopbot · /status\n/portfolio · /orders · /positions · /history\n/setprice 0.10 · /setshares 10\n/reset → /reset confirm\n🧪 Paper only — no real orders are placed or cancelled.")


async def worker(app: Application) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers={"User-Agent": "btc5m-portfolio-paper-simulator/1.0"}, timeout=timeout) as session:
        while True:
            try:
                lock = app.bot_data.get("reset_lock")
                if lock is None:
                    lock = asyncio.Lock()
                    app.bot_data["reset_lock"] = lock
                async with lock:
                    if get_setting("running") == "1":
                        track_upcoming_event()
                    # Continue managing already tracked events even if new monitoring was stopped.
                    await process_tracked_events(app, session)
                    await refresh_position_marks(session)
                await asyncio.sleep(POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Worker cycle failed; retrying.")
                await asyncio.sleep(POLL_SECONDS)


async def post_init(app: Application) -> None:
    app.bot_data["reset_lock"] = asyncio.Lock()
    app.bot_data["worker_task"] = asyncio.create_task(worker(app))


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("worker_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN. Add it to .env.")
    if not ALLOWED_USER_IDS:
        raise SystemExit("Missing TELEGRAM_ALLOWED_USER_IDS. Add your numeric Telegram user ID to .env.")
    init_db()
    log.info("Starting PAPER ONLY simulator: starting cash=$%s, limit=$%s, shares/outcome=%s, polling=%ss",
             get_setting("starting_cash"), get_setting("limit_price"), get_setting("shares"), POLL_SECONDS)
    if os.getenv("YES_PRICE") or os.getenv("NO_PRICE"):
        log.warning("This version uses LIMIT_PRICE (default 0.10); legacy YES_PRICE/NO_PRICE settings are ignored.")
    app = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("startbot", cmd_startbot))
    app.add_handler(CommandHandler("stopbot", cmd_stopbot))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setprice", cmd_setprice))
    app.add_handler(CommandHandler("setshares", cmd_setshares))
    app.add_handler(CommandHandler("help", cmd_help))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
