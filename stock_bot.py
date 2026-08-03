"""Telegram bot exposing the portfolio to a phone.

Long-polls the Telegram Bot API and answers a handful of read-only commands.
It reports; it never edits holdings and never places trades.

Only chat IDs listed in ``TELEGRAM_ALLOWED_CHAT_IDS`` are answered — without
that, anyone who finds the bot could read the portfolio. With the allowlist
empty the bot runs in setup mode: it reports the sender's own chat ID so it
can be configured, and serves nothing else.

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

SETUP_REPLY = """<b>Not configured yet</b>

Your chat ID is <code>{chat_id}</code>

Set it and restart the bot:
<code>TELEGRAM_ALLOWED_CHAT_IDS={chat_id}</code>

Until then no portfolio data is served."""

HELP = """<b>Portfolio bot</b>

/portfolio - value, day and month change
/holding &lt;TICKER&gt; - one position in detail
/dividends - upcoming payouts and 12m income
/allocation - position weights and currency exposure
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


UP = "\U0001f7e2"
DOWN = "\U0001f534"
FLAT = "\u26aa"


def _money(value, symbol):
    """Whole units: cents are noise on a phone."""
    return f"{value:,.0f} {symbol}"


def _dot(value):
    return UP if value > 0 else DOWN if value < 0 else FLAT


def _line(symbol, value, change=None):
    """One holding as a chat line: marker, bold ticker, then the figures.

    Deliberately not column-aligned — Telegram renders normal text in a
    proportional font, so padding buys nothing and a monospace block to force
    it reads like a terminal dump on a phone.
    """
    parts = [f"{_dot(change if change is not None else 0)} <b>{html.escape(symbol)}</b>"]
    parts.append(html.escape(value))
    if change is not None:
        parts.append(f"{change:+.2f}%")
    return " \u00b7 ".join(parts)


def cmd_portfolio(width=DEFAULT_WIDTH):
    _, currency, summaries, aux, failed = _snapshot(width)
    if not summaries:
        return "Could not load any holdings right now \u2014 try again shortly."

    symbol = stock.CURRENCY_SYMBOLS.get(currency, currency)
    positions = sorted(
        (s for s in summaries.values() if s.get("val_now") is not None),
        key=lambda s: s["val_now"],
        reverse=True,
    )
    total = sum(s["val_now"] for s in positions)
    previous = sum(s.get("val_prev") or s["val_now"] for s in positions)
    if total:
        stock.record_portfolio_value(total, currency)
    day_pct = ((total - previous) / previous * 100) if previous else 0.0

    header = [
        "\U0001f4ca <b>Portfolio</b>",
        f"<b>{html.escape(_money(total, symbol))}</b>  {_dot(day_pct)} {day_pct:+.2f}% today",
    ]

    # Only holdings with a recorded cost basis can contribute here. Saying
    # "all time" over a partial set would read as a whole-portfolio figure,
    # so name the coverage whenever it isn't everything.
    priced = [s for s in positions if s.get("cost_value")]
    cost = sum(s["cost_value"] for s in priced)
    if cost:
        unrealized = sum(s.get("unrealized") or 0 for s in priced)
        scope = (
            "all time"
            if len(priced) == len(positions)
            else f"all time \u00b7 {len(priced)} of {len(positions)} holdings"
        )
        header.append(
            f"{_dot(unrealized)} {html.escape(_money(unrealized, symbol))} "
            f"({unrealized / cost * 100:+.2f}%) {scope}"
        )

    trend = _trend_line(aux, currency)
    if trend:
        header.append(trend)

    # Biggest first: the top of the list is what actually gets read on a phone.
    body = [
        _line(s["symbol"], _money(s["val_now"], symbol), s.get("chg_pct"))
        for s in positions
    ]

    parts = ["\n".join(header), "\n".join(body)]
    if failed:
        parts.append(f"\u26a0\ufe0f stale: {html.escape(', '.join(sorted(failed)))}")
    return "\n\n".join(parts)


def _trend_line(aux, currency):
    """One line for the 30-day move, and the benchmark if one is configured."""
    recorded = stock.load_portfolio_history(currency)
    if len(recorded) >= stock.MIN_RECORDED_HISTORY_POINTS:
        series, label = recorded, "30d"
    else:
        # Say what this actually is: today's basket priced backwards, not the
        # portfolio's own history.
        series, label = list(aux.get("history") or ()), "30d (basket)"
    if len(series) < 2 or not series[0]:
        return None
    change = (series[-1] - series[0]) / series[0] * 100
    line = f"\U0001f4c8 {label} {change:+.2f}%"
    benchmark = aux.get("benchmark")
    if benchmark and benchmark.get("change_pct") is not None:
        line += (
            f"  \u00b7  {html.escape(benchmark['symbol'])} "
            f"{benchmark['change_pct']:+.2f}%"
        )
    return line


def cmd_allocation(width=DEFAULT_WIDTH):
    _, _, summaries, _, _ = _snapshot(width)
    positions = [s for s in summaries.values() if s.get("val_now")]
    if not positions:
        return "No holdings to break down."
    total = sum(s["val_now"] for s in positions)
    positions.sort(key=lambda s: s["val_now"], reverse=True)

    lines = []
    for s in positions:
        weight = s["val_now"] / total * 100
        # A repeated block character keeps its width in a proportional font,
        # so the bars still line up without a monospace block.
        bar = "\u2588" * max(1, round(weight / 5))
        lines.append(f"{bar} <b>{html.escape(s['symbol'])}</b> {weight:.1f}%")

    by_currency = {}
    for s in positions:
        code = s.get("source_currency") or "?"
        by_currency[code] = by_currency.get(code, 0) + s["val_now"]
    exposure = "  ".join(
        f"{html.escape(code)} {value / total * 100:.0f}%"
        for code, value in sorted(by_currency.items(), key=lambda kv: -kv[1])
    )
    return (
        "\U0001f967 <b>Allocation</b>\n\n"
        + "\n".join(lines)
        + f"\n\n\U0001f4b1 {exposure}"
    )


def cmd_holding(symbol_arg, width=DEFAULT_WIDTH):
    if not symbol_arg:
        return "Usage: /holding &lt;TICKER&gt;   e.g. /holding MC.PA"

    config, currency, summaries, aux, _ = _snapshot(width)
    wanted = symbol_arg.strip().upper()
    match = next((s for s in summaries.values() if s["symbol"].upper() == wanted), None)
    if match is None:
        held = ", ".join(sorted(config["holdings"]))
        return (
            f"{html.escape(wanted)} is not in the portfolio.\n\n"
            f"Holdings: {html.escape(held)}"
        )

    symbol = stock.CURRENCY_SYMBOLS.get(currency, currency)
    lines = [
        f"{_dot(match.get('chg_pct') or 0)} <b>{html.escape(match['symbol'])}</b>",
        "",
        f"<b>{html.escape(_money(match['val_now'], symbol))}</b> "
        f"\u00b7 {match['qty']:,} shares",
        f"Today  {match['chg_pct']:+.2f}%",
    ]
    month = (aux.get("monthly") or {}).get(match["symbol"])
    if month is not None:
        lines.append(f"Month  {month:+.2f}%")
    if match.get("return_pct") is not None:
        lines.append(
            f"Return  {match['return_pct']:+.2f}% "
            f"({html.escape(_money(match['unrealized'], symbol))})"
        )
    analysts = (aux.get("analysts") or {}).get(match["symbol"])
    if analysts:
        if analysts.get("consensus"):
            lines.append(
                f"\n\U0001f3af {html.escape(analysts['consensus'])} "
                f"\u00b7 {analysts.get('analyst_count', 0)} analysts"
            )
        upside = (analysts.get("price_target") or {}).get("upside_pct")
        if upside is not None:
            lines.append(f"Target {upside:+.1f}% from here")
    return "\n".join(lines)


def cmd_dividends(width=DEFAULT_WIDTH):
    _, currency, _, aux, _ = _snapshot(width)
    dividends = list((aux.get("dividends") or {}).values())
    if not dividends:
        return "No dividend data for these holdings."

    symbol = stock.CURRENCY_SYMBOLS.get(currency, currency)
    parts = []

    earning = sorted(
        (d for d in dividends if d.get("ttm_total")),
        key=lambda d: d["ttm_total"],
        reverse=True,
    )
    if earning:
        total = sum(d["ttm_total"] for d in earning)
        parts.append(
            "\U0001f4b0 <b>Dividends</b>\n"
            f"<b>{html.escape(_money(total, symbol))}</b> over the last 12 months"
        )
        parts.append(
            "\n".join(
                f"\u00b7 <b>{html.escape(d['symbol'])}</b> "
                f"{html.escape(_money(d['ttm_total'], symbol))} "
                f"({(d.get('yield_pct') or 0):.2f}%)"
                for d in earning
            )
        )

    upcoming = sorted(
        (d for d in dividends if d.get("ex_date")), key=lambda d: str(d["ex_date"])
    )
    if upcoming:
        parts.append(
            "\U0001f4c5 <b>Upcoming</b>\n"
            + "\n".join(
                f"\u00b7 <b>{html.escape(d['symbol'])}</b> {d['ex_date']} "
                f"\u2192 {html.escape(_money(d['total_p'], symbol))}"
                for d in upcoming
            )
        )
    return "\n\n".join(parts) if parts else "No dividend data for these holdings."


def cmd_news(limit=5):
    _, _, _, aux, _ = _snapshot()
    items = (aux.get("news") or [])[:limit]
    if not items:
        return "No recent news for these holdings."
    blocks = ["\U0001f4f0 <b>Latest news</b>"]
    for item in items:
        title = html.escape(item["title"])
        link = item.get("link")
        headline = f'<a href="{html.escape(link)}">{title}</a>' if link else title
        blocks.append(
            f"<b>{html.escape(item['symbol'])}</b> "
            f"\u00b7 <i>{html.escape(item['pub_date'])}</i>\n{headline}"
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
    if command in ("/allocation", "/a"):
        return cmd_allocation(width)
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

    if not allowed:
        # Setup mode: the allowlist can't be filled in until you know your own
        # chat ID, and the bot is the obvious place to ask. Telling a sender
        # their own ID discloses nothing they don't already have; the
        # portfolio stays behind the allowlist.
        log.warning("unconfigured: reporting chat id %s to sender", chat_id)
        send_message(token, chat_id, SETUP_REPLY.format(chat_id=chat_id))
        return True

    if chat_id not in allowed:
        # Say nothing at all to strangers, but leave a trace.
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

    width = int(os.environ.get("STOCK_BOT_WIDTH", DEFAULT_WIDTH))
    if allowed:
        log.info("listening, serving %d chat(s)", len(allowed))
    else:
        log.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS is not set — setup mode: the bot will "
            "reply with the sender's chat id and serve no portfolio data"
        )

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
