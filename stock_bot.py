"""Telegram bot exposing the portfolio to a phone.

Long-polls the Telegram Bot API and answers a handful of read-only commands.
It reports; it never edits holdings and never places trades.

Only chat IDs listed in ``TELEGRAM_ALLOWED_CHAT_IDS`` are answered — without
that, anyone who finds the bot could read the portfolio.

Environment:
    TELEGRAM_TOKEN             bot token from @BotFather (required)
    TELEGRAM_ALLOWED_CHAT_IDS  comma-separated chat IDs to serve (required)
    STOCK_PRICE_CONFIG         holdings file, as for the CLI
    STOCK_BOT_WIDTH            render width, default 42 (a phone screen)

Run with ``stock-price-bot``.
"""

import html
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from rich.console import Console

import stock

log = logging.getLogger("stock-bot")

API_ROOT = "https://api.telegram.org/bot"

# Telegram hard-caps a message at 4096 characters.
MAX_MESSAGE = 4096

# Long-poll timeout. Telegram holds the connection open until something
# arrives, so this costs nothing while idle and responds instantly.
POLL_TIMEOUT = 30

# Portfolio fetches are the slow part; a short cache keeps repeated commands
# snappy without serving anything meaningfully stale.
CACHE_TTL = 60

DEFAULT_WIDTH = 42

HELP = """<b>Portfolio bot</b>

/portfolio - value, day and month change
/holding &lt;TICKER&gt; - one position in detail
/dividends - upcoming payouts and 12m income
/news - recent headlines
/help - this message

Read-only: this bot cannot change holdings or place trades."""


def _request(token, method, params=None, timeout=POLL_TIMEOUT + 10):
    """Call one Telegram Bot API method and return its ``result``."""
    url = f"{API_ROOT}{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(f"{method} failed: {payload.get('description')}")
    return payload.get("result")


def send_message(token, chat_id, text):
    """Send one HTML message, trimmed to Telegram's size limit."""
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 32].rsplit("\n", 1)[0] + "\n… (truncated)"
    return _request(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )


def render(renderable, width=DEFAULT_WIDTH):
    """Render a Rich renderable to plain text at phone width.

    ``no_color`` matters: ANSI escapes would show up as literal noise in a
    Telegram message rather than as styling.
    """
    console = Console(width=width, height=200, no_color=True, force_terminal=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def as_code_block(text):
    """Wrap rendered output so Telegram keeps the column alignment."""
    return f"<pre>{html.escape(text)}</pre>"


def _config():
    config = stock.load_config()
    return config, (config["currency"] or "EUR").upper()


def _snapshot(width=DEFAULT_WIDTH):
    """Fetch the portfolio and render the summary table for a phone."""
    config, currency = _config()
    summaries, ticker_to_currency, failed = stock.fetch_summaries(
        config["holdings"], currency, {}, {}, cost_basis=config.get("cost_basis")
    )
    aux = stock.fetch_auxiliary(
        config["holdings"],
        currency,
        summaries,
        ticker_to_currency,
        want_history=bool(ticker_to_currency),
        want_dividends=bool(summaries),
        want_news=bool(summaries),
        want_analysts=bool(summaries),
        benchmark=config.get("benchmark"),
    )
    stock.apply_holiday_zeroing(summaries, aux.get("traded"))
    return config, currency, summaries, aux, failed


def cmd_portfolio(width=DEFAULT_WIDTH):
    config, currency, summaries, aux, failed = _snapshot(width)
    if not summaries:
        return "Could not load any holdings right now — try again shortly."

    total = sum(
        s["val_now"] for s in summaries.values() if s.get("val_now") is not None
    )
    if total:
        stock.record_portfolio_value(total, currency)

    group = stock.build_display_group(
        list(summaries.values()),
        [],
        currency,
        history_points=aux.get("history", []),
        monthly_changes=aux.get("monthly", {}),
        error_symbols=failed,
        holdings=config["holdings"],
        analyst_results=aux.get("analysts", {}),
        benchmark=aux.get("benchmark"),
        portfolio_history=stock.load_portfolio_history(currency),
        max_width=width,
        # Only the table and the trend line belong on a phone.
        max_height=200,
    )
    return as_code_block(render(group, width))


def cmd_holding(symbol, width=DEFAULT_WIDTH):
    if not symbol:
        return "Usage: /holding &lt;TICKER&gt;   e.g. /holding MC.PA"

    config, currency, summaries, aux, _ = _snapshot(width)
    wanted = symbol.strip().upper()
    match = next((s for s in summaries.values() if s["symbol"].upper() == wanted), None)
    if match is None:
        held = ", ".join(sorted(config["holdings"]))
        return f"{html.escape(wanted)} is not in the portfolio.\nHoldings: {html.escape(held)}"

    lines = [f"<b>{html.escape(match['symbol'])}</b>"]
    lines.append(f"Value    {match['val_now']:,.2f} {currency}")
    lines.append(f"Quantity {match['qty']:,}")
    lines.append(f"Day      {match['chg_pct']:+.2f}%")
    month = (aux.get("monthly") or {}).get(match["symbol"])
    if month is not None:
        lines.append(f"Month    {month:+.2f}%")
    if match.get("return_pct") is not None:
        lines.append(
            f"Return   {match['return_pct']:+.2f}%  "
            f"({match['unrealized']:+,.2f} {currency})"
        )
    analysts = (aux.get("analysts") or {}).get(match["symbol"])
    if analysts:
        consensus = analysts.get("consensus")
        if consensus:
            lines.append(f"Analysts {consensus} ({analysts.get('analyst_count', 0)})")
        upside = (analysts.get("price_target") or {}).get("upside_pct")
        if upside is not None:
            lines.append(f"Target   {upside:+.1f}% to mean")
    return "\n".join(lines)


def cmd_dividends(width=DEFAULT_WIDTH):
    config, currency, summaries, aux, _ = _snapshot(width)
    dividends = list((aux.get("dividends") or {}).values())
    if not dividends:
        return "No dividend data for these holdings."
    group = stock.build_display_group(
        list(summaries.values()), dividends, currency,
        holdings=config["holdings"], max_width=width, max_height=200,
    )
    # The summary table is rendered too; keep only the dividends part.
    text = render(group, width)
    marker = text.find("Dividends")
    return as_code_block(text[marker:] if marker != -1 else text)


def cmd_news(limit=5):
    _, _, _, aux, _ = _snapshot()
    items = (aux.get("news") or [])[:limit]
    if not items:
        return "No recent news for these holdings."
    blocks = []
    for item in items:
        title = html.escape(item["title"])
        link = item.get("link")
        headline = f'<a href="{html.escape(link)}">{title}</a>' if link else title
        blocks.append(
            f"<b>{html.escape(item['symbol'])}</b>  "
            f"<i>{html.escape(item['pub_date'])}</i>\n{headline}"
        )
    return "\n\n".join(blocks)


def handle_command(text, width=DEFAULT_WIDTH):
    """Map one incoming message to a reply."""
    parts = text.strip().split()
    if not parts:
        return None
    # Group chats deliver commands as "/portfolio@my_bot".
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if command in ("/start", "/help"):
        return HELP
    if command in ("/portfolio", "/p"):
        return cmd_portfolio(width)
    if command in ("/holding", "/h"):
        return cmd_holding(args[0] if args else None, width)
    if command in ("/dividends", "/d"):
        return cmd_dividends(width)
    if command in ("/news", "/n"):
        return cmd_news()
    return None


def _allowed_chat_ids(raw):
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def process_update(update, token, allowed, width=DEFAULT_WIDTH):
    """Answer one Telegram update. Returns True if a reply was sent."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return False
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = message.get("text") or ""
    if not chat_id or not text.startswith("/"):
        return False

    if chat_id not in allowed:
        # Say nothing useful to strangers, but leave a trace.
        log.warning("ignoring command from chat %s", chat_id)
        return False

    try:
        reply = handle_command(text, width)
    except Exception:
        log.exception("command failed: %s", text)
        reply = "Something went wrong fetching that. Try again shortly."
    if reply is None:
        return False
    send_message(token, chat_id, reply)
    return True


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    token = os.environ.get("TELEGRAM_TOKEN")
    allowed = _allowed_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set")
    if not allowed:
        # Refusing to start is deliberate: an open bot exposes the portfolio
        # to anyone who finds it.
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is not set — refusing to serve")

    width = int(os.environ.get("STOCK_BOT_WIDTH", DEFAULT_WIDTH))
    log.info("listening, serving %d chat(s)", len(allowed))

    offset = None
    backoff = 1
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            updates = _request(token, "getUpdates", params) or []
            backoff = 1
            for update in updates:
                offset = update["update_id"] + 1
                process_update(update, token, allowed, width)
        except KeyboardInterrupt:
            log.info("stopping")
            return
        except Exception:
            # Never die on a transient network or API hiccup: this runs
            # unattended and a crash means a silent bot.
            log.exception("poll failed, retrying in %ss", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
