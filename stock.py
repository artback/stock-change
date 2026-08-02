import argparse
import concurrent.futures
import contextlib
import io
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import yfinance as yf
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import select as select_mod
    import termios
except ImportError:
    termios = None
    select_mod = None

try:
    from importlib.metadata import version

    __version__ = version("stock-price")
except Exception:
    try:
        __version__ = Path(__file__).parent.joinpath("VERSION").read_text().strip()
    except Exception:
        __version__ = "unknown"

# Suppress yfinance logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
console = Console()
# Diagnostics go to stderr so --json keeps stdout to itself.
err_console = Console(stderr=True)

DEFAULT_CONFIG_PATH = Path.home() / ".stock_price.yaml"

DEFAULT_HOLDINGS = {
    "SVOL-B.ST": 8367,
    "INVE-B.ST": 1387,
    "LIFCO-B.ST": 5,
    "MC.PA": 45,
    "INDU-C.ST": 21,
    "IUSA.DE": 720,
}

CURRENCY_SYMBOLS = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "SEK": "kr",
    "JPY": "¥",
    "CHF": "Fr",
    "CAD": "C$",
    "AUD": "A$",
}


# (timezone, open, close) as fractional local hours — e.g. 17.5 == 17:30.
# Times are continuous-session approximations; intraday lunch breaks (e.g.
# Tokyo) are not modelled. Used only to pick the refresh cadence and the
# "Market Closed" tag, so half-hour precision is plenty.
EXCHANGE_SCHEDULES = {
    ".ST": ("Europe/Stockholm", 9, 17.5),
    ".HE": ("Europe/Helsinki", 10, 18.5),
    ".CO": ("Europe/Copenhagen", 9, 17),
    ".OL": ("Europe/Oslo", 9, 16.5),
    ".PA": ("Europe/Paris", 9, 17.5),
    ".DE": ("Europe/Berlin", 9, 17.5),
    ".AS": ("Europe/Amsterdam", 9, 17.5),
    ".BR": ("Europe/Brussels", 9, 17.5),
    ".MI": ("Europe/Rome", 9, 17.5),
    ".MC": ("Europe/Madrid", 9, 17.5),
    ".SW": ("Europe/Zurich", 9, 17.5),
    ".VI": ("Europe/Vienna", 9, 17.5),
    ".L": ("Europe/London", 8, 16.5),
    ".LS": ("Europe/Lisbon", 8, 16.5),
    ".T": ("Asia/Tokyo", 9, 15),
    ".HK": ("Asia/Hong_Kong", 9.5, 16),
    ".SI": ("Asia/Singapore", 9, 17),
    ".AX": ("Australia/Sydney", 10, 16),
    ".NZ": ("Pacific/Auckland", 10, 16.75),
    ".TO": ("America/Toronto", 9.5, 16),
    ".SA": ("America/Sao_Paulo", 10, 17),
    ".MX": ("America/Mexico_City", 8.5, 15),
}

DEFAULT_SCHEDULE = ("America/New_York", 9.5, 16)


def _get_exchange_suffix(symbol):
    dot = symbol.rfind(".")
    if dot > 0:
        return symbol[dot:]
    return None


def is_any_market_open(holdings, now=None):
    if now is None:
        now = datetime.now(ZoneInfo("UTC"))

    suffixes = set()
    for symbol in holdings:
        suffix = _get_exchange_suffix(symbol)
        suffixes.add(suffix)

    schedules = set()
    for suffix in suffixes:
        if suffix and suffix in EXCHANGE_SCHEDULES:
            schedules.add(EXCHANGE_SCHEDULES[suffix])
        else:
            schedules.add(DEFAULT_SCHEDULE)

    for tz_name, open_hour, close_hour in schedules:
        tz = ZoneInfo(tz_name)
        local_now = now.astimezone(tz)
        local_hour = local_now.hour + local_now.minute / 60
        if local_now.weekday() < 5 and open_hour <= local_hour < close_hour:
            return True

    return False


def _has_market_activity(summary_results):
    """Detect if markets actually traded by checking for any price movement.

    On bank holidays, lastPrice == regularMarketPreviousClose for all tickers
    because no trades occurred. When combined with the schedule-based check in
    is_any_market_open(), this reliably detects holidays without needing a
    static calendar — it works for every local/regional holiday automatically.
    """
    if not summary_results:
        return True  # Assume active when we have no data yet
    return any(r.get("chg_pct", 0) != 0 for r in summary_results if r)


def resolve_config_path(config_path=None):
    """Pick the config file to use: CLI arg, then env var, then default path."""
    if config_path:
        return Path(config_path)
    env_path = os.environ.get("STOCK_PRICE_CONFIG")
    return Path(env_path) if env_path else DEFAULT_CONFIG_PATH


def load_config(config_path=None):
    config_data = {"holdings": DEFAULT_HOLDINGS, "currency": "EUR"}

    resolved_path = resolve_config_path(config_path)

    if resolved_path.exists():
        try:
            with open(resolved_path) as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    if "holdings" in user_config:
                        config_data["holdings"] = user_config["holdings"]
                    if "currency" in user_config:
                        config_data["currency"] = user_config["currency"].upper()
        except Exception as e:
            console.print(f"[red]Error loading config ({resolved_path}):[/red] {e}")
    elif config_path:
        console.print(
            f"[yellow]Warning: Config file not found at {config_path}[/yellow]"
        )

    return config_data


# ---------------------------------------------------------------------------
# Editing the config file
#
# Writing YAML back out is lossy with PyYAML — comments and key order are
# dropped — and these files are hand-maintained, so round-trip through ruamel
# when it is installed (it ships with the ``mcp`` extra) and degrade loudly
# rather than silently mangling someone's file.
# ---------------------------------------------------------------------------

try:
    from ruamel.yaml import YAML as _RoundTripYAML
except ImportError:
    _RoundTripYAML = None

PRESERVES_COMMENTS = _RoundTripYAML is not None


def _round_trip_yaml():
    handler = _RoundTripYAML()
    handler.preserve_quotes = True
    return handler


def read_config_document(config_path=None):
    """Read the config file as an editable document.

    Unlike :func:`load_config` this reflects *only* what is on disk: falling
    back to the built-in demo holdings and then writing them out would silently
    add six positions the user never owned.
    """
    path = resolve_config_path(config_path)
    text = path.read_text() if path.exists() else ""

    if not text.strip():
        document = {"holdings": {}, "currency": "EUR"}
    elif _RoundTripYAML is not None:
        document = _round_trip_yaml().load(text)
    else:
        document = yaml.safe_load(text)

    if not isinstance(document, dict):
        raise ValueError(f"{path} does not contain a YAML mapping")
    if not document.get("holdings"):
        document["holdings"] = {}
    return document, path


def write_config_document(document, path):
    """Write the config back atomically, keeping a ``.bak`` of the old file."""
    path = Path(path)
    buffer = io.StringIO()
    if _RoundTripYAML is not None:
        _round_trip_yaml().dump(document, buffer)
    else:
        yaml.safe_dump(dict(document), buffer, default_flow_style=False, sort_keys=False)
    rendered = buffer.getvalue()

    # Re-read what we are about to write before touching the real file, so a
    # bug here can never leave the user with a config they can no longer load.
    reparsed = yaml.safe_load(rendered)
    if not isinstance(reparsed, dict) or "holdings" not in reparsed:
        raise ValueError("refusing to write a config that would not load back")

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(rendered)
    os.replace(tmp, path)
    return path


def _normalise_quantity(quantity):
    """Store whole share counts as ints so the YAML stays tidy."""
    value = float(quantity)
    if pd.isna(value):
        raise ValueError("quantity must be a number")
    return int(value) if value.is_integer() else value


def _normalise_symbol(symbol):
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise ValueError("symbol must not be empty")
    return symbol


def _match_existing_symbol(holdings, symbol):
    """Find the key already used for this ticker, whatever its casing."""
    if symbol in holdings:
        return symbol
    lowered = symbol.lower()
    for key in holdings:
        if str(key).lower() == lowered:
            return key
    return None


# A symbol known to be quoted, used to tell "your ticker is wrong" apart from
# "the price service is unreachable" — the two look identical from one lookup.
_SENTINEL_SYMBOL = "AAPL"


def _fast_quote(symbol, attempts=2):
    """Fetch (ticker, fast_info, price) for a symbol, or raise."""

    def _fetch():
        t = yf.Ticker(symbol)
        fi = t.fast_info
        return t, fi, fi.get("lastPrice")

    return _retry(_fetch, attempts=attempts)


def _price_service_reachable():
    try:
        _, _, price = _fast_quote(_SENTINEL_SYMBOL)
        return price is not None and not pd.isna(price)
    except Exception:
        return False


def resolve_symbol(symbol):
    """Check that a ticker exists and is priced.

    ``status`` is ``"ok"`` when the symbol resolved, ``"unknown"`` when it
    doesn't exist (a typo, usually), and ``"unverified"`` when we couldn't
    reach the price service to find out. That last case matters: a rate limit
    or a dropped connection must not be mistaken for a bad ticker and block a
    legitimate edit.
    """
    symbol = _normalise_symbol(symbol)
    info = {"symbol": symbol, "status": "unknown", "name": None, "price": None,
            "currency": None}
    try:
        ticker, fast_info, price = _fast_quote(symbol)
    except Exception:
        # An unknown ticker raises out of fast_info exactly like an outage
        # does, so ask a symbol we know is quoted: if that one answers, the
        # service is fine and the problem is this symbol.
        info["status"] = "unknown" if _price_service_reachable() else "unverified"
        return info

    if price is None or pd.isna(price):
        return info

    info["status"] = "ok"
    info["price"] = float(price)
    info["currency"] = fast_info.get("currency")
    try:
        details = ticker.info
        info["name"] = details.get("shortName") or details.get("longName")
    except Exception:
        pass
    return info


def _apply_holding_change(symbol, config_path, change):
    """Load the config, apply ``change`` to the holdings, and write it back."""
    symbol = _normalise_symbol(symbol)
    document, path = read_config_document(config_path)
    holdings = document["holdings"]
    key = _match_existing_symbol(holdings, symbol) or symbol
    previous = holdings.get(key)

    result = change(holdings, key, previous)

    write_config_document(document, path)
    return {
        "symbol": key,
        "previous_quantity": previous,
        "config_path": str(path),
        "comments_preserved": PRESERVES_COMMENTS,
        **result,
    }


def set_holding(symbol, quantity, config_path=None):
    """Set a holding to an exact quantity, adding the ticker if it is new."""
    quantity = _normalise_quantity(quantity)
    if quantity <= 0:
        raise ValueError("quantity must be positive — use remove_holding to delete")

    def change(holdings, key, previous):
        holdings[key] = quantity
        return {"quantity": quantity, "action": "updated" if previous is not None else "added"}

    return _apply_holding_change(symbol, config_path, change)


def add_shares(symbol, quantity, config_path=None):
    """Add to (or, with a negative quantity, subtract from) a holding."""
    delta = _normalise_quantity(quantity)
    if delta == 0:
        raise ValueError("quantity must not be zero")

    def change(holdings, key, previous):
        total = _normalise_quantity((previous or 0) + delta)
        if total < 0:
            raise ValueError(
                f"cannot subtract {abs(delta)} from {previous or 0} {key} shares"
            )
        if total == 0:
            holdings.pop(key, None)
            return {"quantity": 0, "delta": delta, "action": "removed"}
        holdings[key] = total
        return {
            "quantity": total,
            "delta": delta,
            "action": "updated" if previous is not None else "added",
        }

    return _apply_holding_change(symbol, config_path, change)


def remove_holding(symbol, config_path=None):
    """Drop a ticker from the portfolio entirely."""

    def change(holdings, key, previous):
        if previous is None:
            raise KeyError(f"{key} is not in the portfolio")
        holdings.pop(key, None)
        return {"quantity": 0, "action": "removed"}

    return _apply_holding_change(symbol, config_path, change)


KNOWN_CURRENCIES = {"EUR", "USD", "GBP", "SEK", "JPY", "CHF", "CAD", "AUD", "NOK", "DKK", "CNY", "HKD", "SGD", "NZD", "KRW", "INR", "BRL", "MXN", "ZAR", "TRY", "PLN", "CZK", "HUF", "ILS", "TWD", "THB"}


def validate_currency(currency_code):
    currency_code = currency_code.upper()
    if len(currency_code) != 3:
        return False
    if currency_code in KNOWN_CURRENCIES:
        return True
    if not currency_code.isalpha():
        return False
    try:
        ticker = yf.Ticker(f"USD{currency_code}=X")
        # A successful lookup with no price means the code is genuinely invalid.
        return bool(ticker.fast_info.get("lastPrice"))
    except Exception:
        # Couldn't verify (e.g. offline). Don't block a plausibly valid code —
        # better to attempt the run than to refuse to start with no network.
        return True


def _retry(fn, attempts=3, base_delay=0.4):
    """Call ``fn`` and retry transient failures with exponential backoff.

    yfinance regularly returns rate-limit (HTTP 429) and transient network
    errors; a couple of short retries turn most of those into a successful
    fetch instead of a ticker that silently drops out of the table.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2**attempt))


def get_rate(source, target, cache):
    if source == target:
        return 1.0
    pair = f"{source}{target}=X"
    if pair in cache:
        return cache[pair]
    try:
        ticker = yf.Ticker(pair)
        rate = ticker.fast_info["lastPrice"]
        cache[pair] = rate
        return rate
    except Exception:
        try:
            inverse_pair = f"{target}{source}=X"
            ticker = yf.Ticker(inverse_pair)
            rate = 1 / ticker.fast_info["lastPrice"]
            cache[pair] = rate
            return rate
        except Exception:
            return None


def _previous_close_from_history(ticker):
    """Fall back to daily history when fast_info lacks a previous close.

    yfinance's fast_info intermittently returns NaN for
    regularMarketPreviousClose (and previousClose) on some exchanges. Left
    unhandled that forces the daily change to 0, hiding real up/down moves.
    The prior session's close is the most recent daily bar before today, or
    the last bar if today's hasn't appeared yet.
    """
    try:
        closes = ticker.history(period="5d")["Close"].dropna()
        if len(closes) < 1:
            return None
        last_bar = closes.index[-1]
        tz = getattr(last_bar, "tzinfo", None)
        today = (pd.Timestamp.now(tz=tz) if tz else pd.Timestamp.now()).date()
        if last_bar.date() == today:
            return float(closes.iloc[-2]) if len(closes) >= 2 else None
        return float(closes.iloc[-1])
    except Exception:
        return None


def _cached_previous_close(symbol, ticker, cache):
    """Previous-session close from daily history, cached per symbol.

    The prior session's close is stable for the whole trading day, so caching
    it avoids a redundant per-ticker history download on every watch refresh.
    """
    if cache is not None and symbol in cache:
        return cache[symbol]
    value = _previous_close_from_history(ticker)
    if cache is not None and value is not None:
        cache[symbol] = value
    return value


def get_ticker_summary(symbol, qty, target_currency, rate_cache, prev_close_cache=None):
    try:
        def _fetch():
            t = yf.Ticker(symbol)
            fi = t.fast_info
            # Touch lastPrice so the network fetch (and any 429) happens here,
            # inside the retry, rather than lazily later.
            return t, fi, fi.get("lastPrice")

        t, fi, price = _retry(_fetch)
        # regularMarketPreviousClose is the authoritative prior-session close.
        # When it's missing, prefer daily history over fast_info's previousClose
        # — the latter is unreliable and sometimes mirrors today's lastPrice
        # (which would collapse the daily change to 0).
        prev_close = fi.get("regularMarketPreviousClose")
        if prev_close is None or pd.isna(prev_close):
            prev_close = _cached_previous_close(symbol, t, prev_close_cache)
        if prev_close is None or pd.isna(prev_close):
            prev_close = fi.get("previousClose")
        source_currency = fi.get("currency", "USD")
        conv = get_rate(source_currency, target_currency, rate_cache)

        if price is not None and not pd.isna(price) and conv is not None and not pd.isna(conv):
            val_now = (price * conv) * qty
            if prev_close and not pd.isna(prev_close):
                val_prev = (prev_close * conv) * qty
                chg_pct = ((price - prev_close) / prev_close) * 100
            else:
                val_prev = val_now
                chg_pct = 0

            daily_chg_val = val_now - val_prev
            return {
                "symbol": symbol,
                "qty": qty,
                "val_now": val_now,
                "val_prev": val_prev,
                "chg_pct": chg_pct,
                "daily_chg_val": daily_chg_val,
                "ticker_obj": t,
                "conv": conv,
                "source_currency": source_currency,
            }
    except Exception:
        pass
    return None


def get_dividend_data(summary_data):
    try:
        t = summary_data["ticker_obj"]
        cal = t.calendar
        if cal and "Ex-Dividend Date" in cal:
            ex_date = cal["Ex-Dividend Date"]
            if ex_date and not pd.isna(ex_date) and ex_date >= datetime.now().date():
                d_info = t.info
                div_amt = (
                    d_info.get("lastDividendValue") or d_info.get("dividendRate") or 0
                )
                if div_amt > 0:
                    return {
                        "symbol": summary_data["symbol"],
                        "ex_date": ex_date,
                        "amt": div_amt,
                        "total_p": (div_amt * summary_data["conv"])
                        * summary_data["qty"],
                        "cur_label": CURRENCY_SYMBOLS.get(
                            summary_data["source_currency"],
                            summary_data["source_currency"],
                        ),
                    }
    except Exception:
        pass
    return None


def fetch_all_dividends(summary_values):
    summary_values = list(summary_values)
    results = {}
    if not summary_values:
        return results
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(summary_values)
    ) as executor:
        future_to_symbol = {
            executor.submit(get_dividend_data, s): s["symbol"] for s in summary_values
        }
        for future in concurrent.futures.as_completed(future_to_symbol):
            res = future.result()
            if res:
                results[future_to_symbol[future]] = res
    return results


# Analyst consensus is scored on the 1 (Strong Buy) .. 5 (Strong Sell) scale
# the rating aggregators use, so a *lower* score is the more bullish one.
_RATING_WEIGHTS = (
    ("strongBuy", "strong_buy", 1),
    ("buy", "buy", 2),
    ("hold", "hold", 3),
    ("sell", "sell", 4),
    ("strongSell", "strong_sell", 5),
)

_CONSENSUS_THRESHOLDS = (
    (1.5, "Strong Buy"),
    (2.5, "Buy"),
    (3.5, "Hold"),
    (4.5, "Sell"),
)

CONSENSUS_STYLES = {
    "Strong Buy": "bold green",
    "Buy": "green",
    "Hold": "yellow",
    "Sell": "red",
    "Strong Sell": "bold red",
}

# Ignore consensus drift smaller than this — the score moves whenever a single
# analyst joins or drops coverage, which isn't a change of opinion.
_TREND_EPSILON = 0.05


def consensus_label(score):
    """Map a 1..5 analyst score onto its human label."""
    if score is None or pd.isna(score):
        return None
    for threshold, label in _CONSENSUS_THRESHOLDS:
        if score < threshold:
            return label
    return "Strong Sell"


def _score_ratings(row):
    """Return ``(score, counts, total)`` for one recommendations row."""
    counts = {}
    total = 0
    weighted = 0
    for source_key, out_key, weight in _RATING_WEIGHTS:
        n = row.get(source_key)
        n = 0 if n is None or pd.isna(n) else int(n)
        counts[out_key] = n
        total += n
        weighted += n * weight
    if total == 0:
        return None, counts, 0
    return weighted / total, counts, total


def _consensus_trend(score, previous):
    """Direction the consensus moved over the last month.

    Reported in plain-English terms rather than the raw score: because the
    scale is inverted, a *falling* score is an upgrade.
    """
    if score is None or previous is None or pd.isna(score) or pd.isna(previous):
        return None
    if previous - score > _TREND_EPSILON:
        return "up"
    if score - previous > _TREND_EPSILON:
        return "down"
    return "steady"


def get_analyst_data(summary_data):
    """Collect analyst ratings and price targets for one holding.

    Returns ``None`` when the ticker has no coverage to report — index funds
    and most small caps come back with an empty recommendations table and a
    price-target dict holding nothing but the current price.
    """
    try:
        t = summary_data["ticker_obj"]

        score = previous_score = None
        counts = {}
        total = 0
        recs = t.recommendations
        if recs is not None and not recs.empty:
            by_period = {
                str(row.get("period", "")): row for row in recs.to_dict("records")
            }
            score, counts, total = _score_ratings(by_period.get("0m", {}))
            previous_score, _, _ = _score_ratings(by_period.get("-1m", {}))

        targets = t.analyst_price_targets or {}
        current = targets.get("current")
        mean = targets.get("mean")
        upside = None
        if (
            current
            and mean
            and not pd.isna(current)
            and not pd.isna(mean)
        ):
            upside = ((mean - current) / current) * 100

        # Nothing worth showing: no ratings and no target to compare against.
        if score is None and upside is None:
            return None

        price_target = None
        if mean is not None and not pd.isna(mean):
            price_target = {
                "current": current,
                "low": targets.get("low"),
                "mean": mean,
                "median": targets.get("median"),
                "high": targets.get("high"),
                "currency": summary_data.get("source_currency"),
                "upside_pct": upside,
            }

        return {
            "symbol": summary_data["symbol"],
            "score": score,
            "consensus": consensus_label(score),
            "previous_score": previous_score,
            "trend": _consensus_trend(score, previous_score),
            "analyst_count": total,
            "counts": counts,
            "price_target": price_target,
        }
    except Exception:
        pass
    return None


def fetch_all_analysts(summary_values):
    summary_values = list(summary_values)
    results = {}
    if not summary_values:
        return results
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(summary_values)
    ) as executor:
        future_to_symbol = {
            executor.submit(get_analyst_data, s): s["symbol"] for s in summary_values
        }
        for future in concurrent.futures.as_completed(future_to_symbol):
            res = future.result()
            if res:
                results[future_to_symbol[future]] = res
    return results


def analyst_view(symbol):
    """Analyst ratings and price targets for any ticker, held or not.

    Returns ``None`` if the symbol doesn't resolve; ``analysts`` is ``None``
    when it resolves but has no coverage.
    """
    instrument = resolve_symbol(symbol)
    if instrument["status"] == "unknown":
        return None
    return {
        "instrument": instrument,
        "analysts": get_analyst_data(
            {
                "symbol": instrument["symbol"],
                "ticker_obj": yf.Ticker(instrument["symbol"]),
                "source_currency": instrument["currency"],
            }
        ),
    }


def apply_holiday_zeroing(summary_cache, traded_symbols):
    """Zero the daily change for tickers whose exchange didn't trade today.

    yfinance keeps reporting the previous session's delta on a closed
    exchange, which is misleading. ``traded_symbols`` of None means "no
    holiday information yet" — leave the live values untouched. Applied after
    every summary refresh (not just when history is refetched) so the zeroing
    survives the next ``get_ticker_summary`` overwrite instead of flickering.
    """
    if traded_symbols is None:
        return
    for sym, data in summary_cache.items():
        if sym not in traded_symbols:
            data["chg_pct"] = 0
            data["daily_chg_val"] = 0
            data["val_prev"] = data["val_now"]


def _fetch_raw_news(symbol):
    try:
        return yf.Ticker(symbol).news or []
    except Exception:
        return []


def get_news_data(symbols, max_age_days=14):
    news_items = []
    seen_titles = set()
    cutoff = datetime.now(ZoneInfo("UTC")) - pd.Timedelta(days=max_age_days)
    symbols = list(symbols)
    if not symbols:
        return []

    # Fetch each ticker's news concurrently — these are independent network
    # round-trips and dominated cold-start time when done serially. Process
    # results in the original symbol order so title de-duplication stays
    # deterministic (the first symbol to carry a title wins).
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(symbols)) as executor:
        raw_news = list(executor.map(_fetch_raw_news, symbols))

    # strict=True: executor.map yields exactly one result per symbol, so an
    # uneven zip would mean news was being attributed to the wrong ticker.
    for symbol, articles in zip(symbols, raw_news, strict=True):
        try:
            for article in articles[:3]:
                content = article.get("content", {})
                title = content.get("title", "")
                if not title or title in seen_titles:
                    continue
                pub_date_raw = content.get("pubDate", "")
                pub_dt = None
                if pub_date_raw:
                    # An unparseable date just means we can't age-filter this
                    # article; keep it rather than dropping it.
                    with contextlib.suppress(Exception):
                        pub_dt = datetime.fromisoformat(
                            pub_date_raw.replace("Z", "+00:00")
                        )
                if pub_dt and pub_dt < cutoff:
                    continue
                seen_titles.add(title)
                link = ""
                click_through = content.get("clickThroughUrl")
                if click_through:
                    link = click_through.get("url", "")
                if not link:
                    canonical = content.get("canonicalUrl")
                    if canonical:
                        link = canonical.get("url", "")
                provider = content.get("provider", {}).get("displayName", "")
                summary = content.get("summary", "")
                pub_date_str = pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else pub_date_raw[:16]
                news_items.append(
                    {
                        "symbol": symbol,
                        "title": title,
                        "link": link,
                        "provider": provider,
                        "pub_date": pub_date_str,
                        "summary": summary,
                    }
                )
        except Exception:
            continue
    news_items.sort(key=lambda x: x["pub_date"], reverse=True)
    return news_items[:15]


def render_sparkline(values):

    if not values or len(values) < 2:
        return ""

    # Use horizontal segments at different heights for a clean, bold line

    chars = ["⎽", "⎼", "⎻", "⎺"]

    clean_values = [v for v in values if not pd.isna(v)]
    if not clean_values or len(clean_values) < 2:
        return ""

    min_v, max_v = min(clean_values), max(clean_values)

    span = max_v - min_v

    if span <= 0:
        return "─" * len(values)

    return "".join(
        (chars[min(int((v - min_v) / span * 3), 3)] if not pd.isna(v) else " ")
        for v in values
    )


def fetch_history(holdings, target_currency, ticker_to_currency):
    try:
        symbols = list(holdings.keys())
        currencies = set(ticker_to_currency.values())
        rate_pairs = [
            f"{c}{target_currency}=X" for c in currencies if c != target_currency
        ]

        all_to_fetch = symbols + rate_pairs
        df = yf.download(
            all_to_fetch,
            period="1mo",
            interval="1d",
            progress=False,
            threads=True,
            timeout=10,
        )

        if df.empty:
            return [], {}, set()

        close_data = df["Close"]
        if isinstance(close_data, pd.Series):
            sym = all_to_fetch[0]
            close_data = pd.DataFrame({sym: close_data})

        # Identify rows where at least one stock had real trading data
        # (before ffill) so we can exclude pure holiday rows where only
        # forex pairs traded — stale prices + fresh rates skew totals.
        stock_cols = [s for s in symbols if s in close_data.columns]
        has_stock_data = (
            close_data[stock_cols].notna().any(axis=1)
            if stock_cols
            else pd.Series(True, index=close_data.index)
        )

        # Forward-fill then back-fill: ffill handles gaps in the middle
        # (partial holidays), bfill handles NaN at the start of the series
        # (e.g. exchange rate pairs missing on the first trading day would
        # otherwise fall back to rate=1.0 and massively inflate totals).
        close_data = close_data.ffill().bfill()

        # Drop rows with no stock data (e.g. bank holidays where only forex traded)
        close_data = close_data[has_stock_data]

        # Calculate monthly change for each ticker
        monthly_changes = {}
        for sym in symbols:
            if sym in close_data.columns:
                series = close_data[sym].dropna()
                if len(series) >= 2:
                    start_price = series.iloc[0]
                    end_price = series.iloc[-1]
                    monthly_changes[sym] = (
                        (end_price - start_price) / start_price
                    ) * 100

        history_totals = []
        for _, row in close_data.iterrows():
            daily_total = 0
            has_data = False
            for sym, qty in holdings.items():
                if sym in row and not pd.isna(row[sym]):
                    src_curr = ticker_to_currency.get(sym, "USD")
                    rate = 1.0
                    if src_curr != target_currency:
                        r_sym = f"{src_curr}{target_currency}=X"
                        rate = (
                            row[r_sym]
                            if r_sym in row and not pd.isna(row[r_sym])
                            else 1.0
                        )

                    daily_total += row[sym] * qty * rate
                    has_data = True

            if has_data:
                history_totals.append(daily_total)

        # Determine which exchanges actually traded today. Per-exchange holidays
        # mean some may be open while others are closed, but all tickers on one
        # exchange share a calendar — so we decide per exchange, not per ticker
        # (yfinance's daily bar lags for some tickers in the live session).
        #
        # Anchor on the download's own latest session rather than a machine-local
        # "today": comparing a local timestamp against the tz-naive daily index
        # is off-by-a-day when running far from the holdings' exchanges. The
        # latest bar only counts as "today" if it matches the current date in
        # local OR UTC time (so weekends/holidays, where the latest bar is an
        # earlier session, correctly yield no traded exchanges).
        close_raw = df["Close"]
        today_dates = {pd.Timestamp.now().date(), pd.Timestamp.now(tz="UTC").date()}
        traded_today = set()
        if isinstance(close_raw, pd.Series):
            # Single-ticker download: columns aren't keyed by symbol.
            if len(close_raw.index):
                latest = close_raw.index.max()
                if latest.date() in today_dates and pd.notna(close_raw.loc[latest]):
                    traded_today = set(symbols)
        elif len(close_raw.index):
            latest = close_raw.index.max()
            if latest.date() in today_dates:
                latest_row = close_raw.loc[latest]
                traded_suffixes = {
                    _get_exchange_suffix(sym)
                    for sym in symbols
                    if sym in latest_row.index and pd.notna(latest_row[sym])
                }
                traded_today = {
                    sym
                    for sym in symbols
                    if _get_exchange_suffix(sym) in traded_suffixes
                }

        return history_totals, monthly_changes, traded_today
    except Exception:
        return [], {}, set()


def fetch_summaries(
    holdings,
    target_currency,
    rate_cache,
    prev_close_cache=None,
    on_progress=None,
    timeout=15,
):
    """Fetch per-ticker summaries concurrently.

    Returns ``(summaries, ticker_to_currency, failed)`` where ``summaries``
    maps symbol -> summary dict and ``failed`` lists symbols that errored or
    returned no data (so the caller can flag them instead of silently dropping
    them). ``on_progress(completed, total, summaries)`` is called as each
    result arrives, to drive a live progress display.
    """
    summaries = {}
    ticker_to_currency = {}
    total = len(holdings)
    if total == 0:
        return summaries, ticker_to_currency, []

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=total) as executor:
        future_to_symbol = {
            executor.submit(
                get_ticker_summary,
                s,
                q,
                target_currency,
                rate_cache,
                prev_close_cache,
            ): s
            for s, q in holdings.items()
        }
        try:
            for future in concurrent.futures.as_completed(
                future_to_symbol, timeout=timeout
            ):
                symbol = future_to_symbol[future]
                completed += 1
                try:
                    res = future.result()
                except Exception:
                    res = None
                if res:
                    summaries[symbol] = res
                    ticker_to_currency[symbol] = res["source_currency"]
                if on_progress:
                    on_progress(completed, total, summaries)
        except concurrent.futures.TimeoutError:
            pass

    failed = [s for s in holdings if s not in summaries]
    return summaries, ticker_to_currency, failed


def fetch_auxiliary(
    holdings,
    target_currency,
    summaries,
    ticker_to_currency,
    *,
    want_history=False,
    want_dividends=False,
    want_news=False,
    want_analysts=False,
):
    """Refresh whichever of the auxiliary sections are requested, concurrently.

    Returns a dict with keys ``history``/``monthly``/``traded`` (when history
    was requested), ``dividends``, ``news`` and ``analysts`` — present only for
    the sections that were requested. They only read the summary/holdings, so
    running them together avoids serialising independent network round-trips.
    """
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        hist_future = (
            pool.submit(fetch_history, holdings, target_currency, ticker_to_currency)
            if want_history
            else None
        )
        div_future = (
            pool.submit(fetch_all_dividends, list(summaries.values()))
            if want_dividends
            else None
        )
        news_future = (
            pool.submit(get_news_data, list(summaries.keys()))
            if want_news
            else None
        )
        analyst_future = (
            pool.submit(fetch_all_analysts, list(summaries.values()))
            if want_analysts
            else None
        )

        if hist_future is not None:
            history, monthly, traded = hist_future.result()
            result["history"] = history
            result["monthly"] = monthly
            result["traded"] = traded
        if div_future is not None:
            result["dividends"] = div_future.result()
        if news_future is not None:
            result["news"] = news_future.result()
        if analyst_future is not None:
            result["analysts"] = analyst_future.result()
    return result


CACHE_PATH = Path.home() / ".stock_price_cache.json"

_CACHED_SUMMARY_FIELDS = (
    "symbol",
    "qty",
    "val_now",
    "val_prev",
    "chg_pct",
    "daily_chg_val",
    "source_currency",
    "conv",
)


def _cache_key(holdings, currency):
    return f"{currency}:" + ",".join(f"{k}={v}" for k, v in sorted(holdings.items()))


def load_cached_portfolio(holdings, currency, ttl, path=CACHE_PATH):
    """Return a fresh cached snapshot for this portfolio, or None.

    Used to make repeated one-shot invocations instant. Disabled when
    ``ttl <= 0``. The snapshot is only returned if it matches the same
    holdings/currency and is younger than ``ttl`` seconds.
    """
    if ttl <= 0:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return None
    if data.get("key") != _cache_key(holdings, currency):
        return None
    age = time.time() - data.get("timestamp", 0)
    if age < 0 or age > ttl:
        return None
    data["age"] = age
    return data


def save_cached_portfolio(
    holdings, currency, summaries, dividends, news, history_points, monthly_changes,
    path=CACHE_PATH, analysts=None,
):
    """Persist a portfolio snapshot for fast repeated runs. Best-effort."""
    try:
        payload = {
            "key": _cache_key(holdings, currency),
            "timestamp": time.time(),
            "summaries": [
                {f: s[f] for f in _CACHED_SUMMARY_FIELDS if f in s}
                for s in summaries
            ],
            "dividends": [{**d, "ex_date": str(d["ex_date"])} for d in dividends],
            "news": news,
            "history_points": history_points,
            "monthly_changes": monthly_changes,
            "analysts": analysts or {},
        }
        Path(path).write_text(json.dumps(payload))
    except Exception:
        pass


JSON_SCHEMA_VERSION = 1


def _json_safe(value):
    """Recursively coerce a value into something ``json.dumps`` accepts.

    Prices arrive as numpy scalars and missing ones as ``NaN``/``NaT``; both
    would serialise to bare ``NaN``, which is not valid JSON and breaks strict
    parsers. Doing this once over the finished payload makes "the output always
    parses" a structural guarantee rather than a per-field discipline.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if pd.isna(value) else value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def portfolio_payload(
    target_currency,
    summaries,
    dividends,
    news,
    history_points,
    monthly_changes,
    *,
    analysts=None,
    failed=None,
    holdings=None,
    cached=False,
    cache_age=None,
    generated_at=None,
):
    """Build the machine-readable portfolio snapshot behind ``--json``.

    Deliberately separate from both the fetching and the rendering: the live
    and cached paths emit an identical shape, and an out-of-process consumer
    (an MCP server, a cron job) can reuse it without touching the terminal UI.
    """
    analysts = analysts or {}
    monthly_changes = monthly_changes or {}
    failed = set(failed or ())
    history_points = list(history_points or ())

    positions = []
    total_val = 0.0
    total_prev = 0.0
    total_daily = 0.0
    for s in sorted(summaries, key=lambda x: x["symbol"]):
        symbol = s["symbol"]
        val_now = s.get("val_now")
        val_prev = s.get("val_prev")
        daily = s.get("daily_chg_val")

        if val_now is not None and not pd.isna(val_now):
            total_val += val_now
        if val_prev is not None and not pd.isna(val_prev):
            total_prev += val_prev
        elif val_now is not None and not pd.isna(val_now):
            total_prev += val_now
        if daily is not None and not pd.isna(daily):
            total_daily += daily

        positions.append(
            {
                "symbol": symbol,
                # "stale" means the last refresh failed, so the figures are the
                # last known good ones rather than current.
                "status": "stale" if symbol in failed else "ok",
                "quantity": s.get("qty"),
                "value": val_now,
                "previous_value": val_prev,
                "daily_change": daily,
                "daily_change_pct": s.get("chg_pct"),
                "month_change_pct": monthly_changes.get(symbol),
                "source_currency": s.get("source_currency"),
                "fx_rate": s.get("conv"),
                "analysts": analysts.get(symbol),
            }
        )

    # Holdings that never loaded at all would otherwise be missing entirely,
    # making a partial portfolio look complete to whatever consumes this.
    loaded = {s["symbol"] for s in summaries}
    for symbol in sorted(failed - loaded):
        positions.append(
            {
                "symbol": symbol,
                "status": "error",
                "quantity": (holdings or {}).get(symbol),
                "value": None,
                "previous_value": None,
                "daily_change": None,
                "daily_change_pct": None,
                "month_change_pct": None,
                "source_currency": None,
                "fx_rate": None,
                "analysts": None,
            }
        )

    month_change_pct = None
    if len(history_points) > 1 and history_points[0]:
        month_change_pct = (
            (history_points[-1] - history_points[0]) / history_points[0]
        ) * 100

    payload = {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated_at": (generated_at or datetime.now().astimezone()).isoformat(
            timespec="seconds"
        ),
        "currency": target_currency,
        "cached": bool(cached),
        "cache_age_seconds": cache_age,
        "totals": {
            "value": total_val,
            "previous_value": total_prev,
            "daily_change": total_daily,
            "daily_change_pct": (
                ((total_val - total_prev) / total_prev) * 100 if total_prev else 0.0
            ),
            "month_change_pct": month_change_pct,
        },
        "positions": positions,
        "dividends": [
            {**d, "ex_date": str(d["ex_date"])}
            for d in sorted(dividends, key=lambda x: str(x["ex_date"]))
        ],
        "news": list(news or ()),
        "history_points": history_points,
    }
    return _json_safe(payload)


def collect_portfolio(
    holdings,
    target_currency,
    *,
    cache_ttl=0,
    want_analysts=True,
    cache_path=CACHE_PATH,
):
    """Fetch a full portfolio snapshot and return it as a JSON-ready payload.

    The quiet counterpart to the live TUI loop — it renders nothing, so stdout
    stays clean for ``--json`` consumers.
    """
    cached = load_cached_portfolio(
        holdings, target_currency, cache_ttl, path=cache_path
    )
    if cached:
        return portfolio_payload(
            target_currency,
            cached.get("summaries", []),
            cached.get("dividends", []),
            cached.get("news", []),
            cached.get("history_points", []),
            cached.get("monthly_changes", {}),
            analysts=cached.get("analysts", {}),
            holdings=holdings,
            cached=True,
            cache_age=int(cached.get("age", 0)),
        )

    summaries, ticker_to_currency, failed = fetch_summaries(
        holdings, target_currency, {}, {}
    )
    aux = fetch_auxiliary(
        holdings,
        target_currency,
        summaries,
        ticker_to_currency,
        want_history=bool(ticker_to_currency),
        want_dividends=bool(summaries),
        want_news=bool(summaries),
        want_analysts=bool(summaries) and want_analysts,
    )
    apply_holiday_zeroing(summaries, aux.get("traded"))

    dividends = list(aux.get("dividends", {}).values())
    news = aux.get("news", [])
    analysts = aux.get("analysts", {})
    history_points = aux.get("history", [])
    monthly_changes = aux.get("monthly", {})

    if summaries:
        save_cached_portfolio(
            holdings,
            target_currency,
            list(summaries.values()),
            dividends,
            news,
            history_points,
            monthly_changes,
            path=cache_path,
            analysts=analysts,
        )

    return portfolio_payload(
        target_currency,
        list(summaries.values()),
        dividends,
        news,
        history_points,
        monthly_changes,
        analysts=analysts,
        failed=failed,
        holdings=holdings,
    )


def _analyst_cells(info):
    """Render the (consensus, target upside) table cells for one holding."""
    if not info:
        return Text("-", style="dim"), Text("-", style="dim")

    consensus = info.get("consensus")
    if consensus:
        arrow = {"up": " ↑", "down": " ↓"}.get(info.get("trend"), "")
        consensus_cell = Text(
            f"{consensus}{arrow}", style=CONSENSUS_STYLES.get(consensus, "white")
        )
        count = info.get("analyst_count")
        if count:
            consensus_cell.append(f" {count}", style="dim")
    else:
        consensus_cell = Text("-", style="dim")

    upside = (info.get("price_target") or {}).get("upside_pct")
    if upside is None or pd.isna(upside):
        target_cell = Text("-", style="dim")
    else:
        target_cell = Text(
            f"{upside:+.1f}%", style="green" if upside >= 0 else "red"
        )
    return consensus_cell, target_cell


def build_display_group(
    summary_results,
    dividend_results,
    target_currency,
    footer_text="",
    history_points=None,
    monthly_changes=None,
    news_items=None,
    error_symbols=None,
    holdings=None,
    analyst_results=None,
):
    target_symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)
    monthly_changes = monthly_changes or {}
    error_symbols = set(error_symbols or ())
    # Only widen the table when there is coverage to show — a portfolio of
    # index funds has none, and two columns of "-" is just noise.
    analyst_results = analyst_results or {}
    show_analysts = bool(analyst_results)

    # 1. Summary Table (No expand=True to keep it compact)
    table = Table(
        title=f"Portfolio Summary ({target_currency})", header_style="bold cyan"
    )
    table.add_column("Ticker", width=12, no_wrap=True)
    table.add_column("Quantity", justify="right", width=10, no_wrap=True)
    table.add_column(
        f"Value ({target_symbol})",
        justify="right",
        style="bold white",
        width=15,
        no_wrap=True,
    )
    table.add_column(
        f"Daily ({target_symbol})", justify="right", width=12, no_wrap=True
    )
    table.add_column("Day %", justify="right", width=10, no_wrap=True)
    table.add_column("Month %", justify="right", width=10, no_wrap=True)
    if show_analysts:
        table.add_column("Analysts", justify="right", width=15, no_wrap=True)
        table.add_column("Target", justify="right", width=9, no_wrap=True)

    total_val = 0
    total_prev = 0
    total_daily_chg = 0
    for s in sorted(summary_results, key=lambda x: x["symbol"]):
        val_now = s["val_now"]
        val_prev = s["val_prev"]
        daily_chg = s["daily_chg_val"]
        chg_pct = s["chg_pct"]

        if not pd.isna(val_now):
            total_val += val_now
        if not pd.isna(val_prev):
            total_prev += val_prev
        else:
            total_prev += val_now if not pd.isna(val_now) else 0
        if not pd.isna(daily_chg):
            total_daily_chg += daily_chg

        m_chg = monthly_changes.get(s["symbol"])
        m_text = (
            Text(f"{m_chg:+.2f}%", style="green" if m_chg >= 0 else "red")
            if m_chg is not None and not pd.isna(m_chg)
            else Text("-", style="dim")
        )

        # Flag a ticker whose latest refresh failed — its row is still shown
        # (last-good values) but marked stale so the total isn't silently wrong.
        if s["symbol"] in error_symbols:
            ticker_cell = Text(f"{s['symbol']} ⚠", style="yellow")
        else:
            ticker_cell = s["symbol"]

        row = [
            ticker_cell,
            f"{s['qty']:,}",
            f"{val_now:,.2f} {target_symbol}" if not pd.isna(val_now) else "-",
            Text(
                f"{daily_chg:+,.2f} {target_symbol}",
                style="green" if daily_chg >= 0 else "red",
            )
            if not pd.isna(daily_chg)
            else Text("-", style="dim"),
            Text(f"{chg_pct:+.2f}%", style="green" if chg_pct >= 0 else "red")
            if not pd.isna(chg_pct)
            else Text("-", style="dim"),
            m_text,
        ]
        if show_analysts:
            row.extend(_analyst_cells(analyst_results.get(s["symbol"])))
        table.add_row(*row)

    # Tickers that have never loaded (no cached data at all) would otherwise
    # vanish entirely — show them explicitly as errored so a partial portfolio
    # can't be mistaken for the whole.
    loaded = {s["symbol"] for s in summary_results}
    for sym in sorted(error_symbols - loaded):
        qty = (holdings or {}).get(sym)
        row = [
            Text(f"{sym} ⚠", style="red"),
            f"{qty:,}" if isinstance(qty, (int, float)) else "",
            Text("error", style="dim red"),
            Text("-", style="dim"),
            Text("-", style="dim"),
            Text("-", style="dim"),
        ]
        if show_analysts:
            row.extend((Text("-", style="dim"), Text("-", style="dim")))
        table.add_row(*row)

    if summary_results:
        total_chg_pct = (
            ((total_val - total_prev) / total_prev) * 100
            if total_prev != 0
            else 0
        )
        table.add_section()
        total_row = [
            Text("TOTAL", style="bold"),
            "",
            Text(f"{total_val:,.2f} {target_symbol}", style="bold white"),
            Text(
                f"{total_daily_chg:+,.2f} {target_symbol}",
                style="bold green" if total_daily_chg >= 0 else "bold red",
            ),
            Text(
                f"{total_chg_pct:+.2f}%",
                style="bold green" if total_chg_pct >= 0 else "bold red",
            ),
            Text(""),
        ]
        if show_analysts:
            total_row.extend((Text(""), Text("")))
        table.add_row(*total_row)

    # 2. Dividends Table
    div_table = None
    if dividend_results:
        div_table = Table(title="Upcoming Dividends", header_style="bold magenta")
        div_table.add_column("Ticker", width=12, no_wrap=True)
        div_table.add_column("Ex-Date", justify="center", width=12, no_wrap=True)
        div_table.add_column("Amount", justify="right", width=12, no_wrap=True)
        div_table.add_column(
            f"Total ({target_symbol})",
            justify="right",
            style="green",
            width=15,
            no_wrap=True,
        )
        for d in sorted(dividend_results, key=lambda x: x["ex_date"]):
            div_table.add_row(
                d["symbol"],
                str(d["ex_date"]),
                f"{d['amt']:.2f} {d['cur_label']}",
                f"{d['total_p']:,.2f} {target_symbol}",
            )

    # 3. Sparkline Panel
    summary_panel = None
    if total_val > 0 and history_points and len(history_points) > 1:
        spark = render_sparkline(history_points)
        summary_text = Text()
        summary_text.append("30D TREND: ", style="dim")
        summary_text.append(spark, style="bright_cyan")
        if len(history_points) >= 2:
            month_chg = ((history_points[-1] - history_points[0]) / history_points[0]) * 100
            summary_text.append(
                f"  {month_chg:+.2f}%",
                style="bold green" if month_chg >= 0 else "bold red",
            )
        summary_panel = Panel(summary_text, border_style="bright_blue", expand=False)

    # 4. News Panel
    news_panel = None
    if news_items:
        news_text = Text()
        for i, item in enumerate(news_items):
            if i > 0:
                news_text.append("\n\n")
            news_text.append(f"  {item['symbol']}", style="bold white")
            news_text.append(f"  {item['pub_date']}", style="dim")
            if item["provider"]:
                news_text.append(f"  {item['provider']}", style="dim italic")
            news_text.append("\n  ")
            title_text = Text(item["title"])
            if item["link"]:
                title_text.stylize(f"link {item['link']}")
                title_text.stylize("underline bright_cyan")
            else:
                title_text.stylize("bright_cyan")
            news_text.append_text(title_text)
            if item.get("summary"):
                summary = item["summary"]
                if len(summary) > 200:
                    summary = summary[:200].rsplit(" ", 1)[0] + "..."
                news_text.append(f"\n  {summary}", style="dim")
        news_panel = Panel(
            news_text,
            title="Related News",
            border_style="yellow",
            expand=False,
            padding=(1, 2),
        )

    # 5. Footer
    footer = Text(footer_text, style="dim italic") if footer_text else Text("")

    elements = [table]
    if div_table:
        elements.append(div_table)
    if news_panel:
        elements.append(news_panel)
    if summary_panel:
        elements.append(summary_panel)
    elements.append(footer)

    return Group(*elements)


def fetch_portfolio():
    parser = argparse.ArgumentParser(description="Track stock prices and dividends")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("-c", "--currency", help="Output currency (e.g. USD, EUR, SEK)")
    parser.add_argument(
        "-w", "--watch", action="store_true", help="Watch mode: auto-refresh"
    )
    parser.add_argument(
        "-i", "--interval", type=int, default=30, help="Watch mode refresh interval in seconds (default: 30)"
    )
    parser.add_argument("--config", help="Path to a custom YAML configuration file")
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=int(os.environ.get("STOCK_PRICE_CACHE_TTL", "0")),
        help="Reuse cached results younger than N seconds for instant repeat "
        "runs (0 = disabled; one-shot mode only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the portfolio as JSON on stdout instead of rendering a table",
    )
    parser.add_argument(
        "--no-analysts",
        action="store_true",
        help="Skip analyst ratings and price targets",
    )
    args = parser.parse_args()

    if args.json and args.watch:
        parser.error("--json cannot be combined with --watch (it is one-shot)")

    config = load_config(args.config)
    target_currency = (args.currency or config["currency"]).upper()
    holdings = config["holdings"]

    # Set terminal title (never in --json mode: stdout belongs to the payload)
    if sys.stdout.isatty() and not args.json:
        sys.stdout.write("\033]0;Stock Price\007")
        sys.stdout.flush()

    try:
        if not validate_currency(target_currency):
            err_console.print(
                f"[bold red]ERROR:[/bold red] '{target_currency}' is not a valid ISO currency code."
            )
            sys.exit(1)

        if args.json:
            payload = collect_portfolio(
                holdings,
                target_currency,
                cache_ttl=args.cache_ttl,
                want_analysts=not args.no_analysts,
            )
            print(json.dumps(payload, indent=2, allow_nan=False))
            return

        # Instant repeat runs: render a fresh-enough cached snapshot and exit.
        if not args.watch:
            cached = load_cached_portfolio(holdings, target_currency, args.cache_ttl)
            if cached:
                age = int(cached.get("age", 0))
                console.print(
                    build_display_group(
                        cached.get("summaries", []),
                        cached.get("dividends", []),
                        target_currency,
                        f"(cached {age}s ago — run again for fresh data)",
                        cached.get("history_points", []),
                        cached.get("monthly_changes", {}),
                        cached.get("news", []),
                        analyst_results=cached.get("analysts", {}),
                    )
                )
                return

        summary_cache = {}
        dividend_cache = {}
        news_cache = []
        analyst_cache = {}
        history_points = []
        monthly_changes = {}
        last_history_update = 0
        last_dividend_update = 0
        last_news_update = 0
        last_analyst_update = 0
        traded_symbols = None
        failed_symbols = []
        ticker_to_currency = {}
        rate_cache = {}
        prev_close_cache = {}
        last_update = "Initializing..."
        interval = max(5, args.interval)

        with Live(
            build_display_group([], [], target_currency, "Initializing..."),
            console=console,
            refresh_per_second=4,
            transient=True,
            screen=args.watch,
        ) as live:
            old_attr = None
            if args.watch and sys.stdin.isatty() and termios:
                try:
                    sys.stdout.write("\033[?1004h")
                    sys.stdout.flush()
                    old_attr = termios.tcgetattr(sys.stdin)
                    new_attr = termios.tcgetattr(sys.stdin)
                    new_attr[3] &= ~termios.ICANON
                    new_attr[3] &= ~termios.ECHO
                    termios.tcsetattr(sys.stdin, termios.TCSANOW, new_attr)
                except Exception:
                    pass

            try:
                while True:
                    # The auxiliary caches are rebound further down each cycle.
                    # This callback fires during fetch_summaries just below —
                    # before that happens — so it should show the previous
                    # cycle's values while prices refresh. Binding them as
                    # defaults captures exactly that, and keeps it true even if
                    # the callback ever stops being invoked synchronously.
                    def on_progress(
                        completed,
                        total,
                        partial,
                        _dividends=dividend_cache,
                        _history=history_points,
                        _news=news_cache,
                        _failed=failed_symbols,
                        _analysts=analyst_cache,
                    ):
                        merged = dict(summary_cache)
                        merged.update(partial)
                        live.update(
                            build_display_group(
                                list(merged.values()),
                                list(_dividends.values()),
                                target_currency,
                                f"Updating ({completed}/{total})...",
                                _history,
                                monthly_changes,
                                _news,
                                error_symbols=_failed,
                                holdings=holdings,
                                analyst_results=_analysts,
                            )
                        )

                    summaries, ttc, failed_symbols = fetch_summaries(
                        holdings,
                        target_currency,
                        rate_cache,
                        prev_close_cache,
                        on_progress=on_progress,
                    )
                    # Merge fresh data over the cache so a ticker that failed
                    # this cycle keeps its last-good values (flagged stale)
                    # rather than vanishing from the table.
                    summary_cache.update(summaries)
                    ticker_to_currency.update(ttc)

                    # Re-apply the last known holiday state so zeroed tickers
                    # don't flicker back to their stale delta after the refresh.
                    apply_holiday_zeroing(summary_cache, traded_symbols)

                    # History (30D, every 120s), dividends and news (every 600s)
                    # only read the summary/holdings, so refresh whichever are
                    # due concurrently rather than serially.
                    now = time.time()
                    aux = fetch_auxiliary(
                        holdings,
                        target_currency,
                        summary_cache,
                        ticker_to_currency,
                        want_history=bool(
                            ticker_to_currency
                            and (now - last_history_update > 120 or not history_points)
                        ),
                        want_dividends=bool(
                            summary_cache
                            and (now - last_dividend_update > 600 or not dividend_cache)
                        ),
                        want_news=bool(
                            summary_cache
                            and (now - last_news_update > 600 or not news_cache)
                        ),
                        # Analyst data moves on a scale of days and is served by
                        # the rate-limit-prone fundamentals endpoint, so keep it
                        # firmly off the fast refresh path.
                        want_analysts=bool(
                            summary_cache
                            and not args.no_analysts
                            and (now - last_analyst_update > 600 or not analyst_cache)
                        ),
                    )
                    if "history" in aux:
                        if aux["history"]:
                            history_points = aux["history"]
                        if aux["monthly"]:
                            monthly_changes.update(aux["monthly"])
                        traded_symbols = aux["traded"]
                        last_history_update = now
                        apply_holiday_zeroing(summary_cache, traded_symbols)
                    if "dividends" in aux:
                        dividend_cache = aux["dividends"]
                        last_dividend_update = now
                    if "news" in aux:
                        news_cache = aux["news"]
                        last_news_update = now
                    if "analysts" in aux:
                        # Keep the previous values when a refresh comes back
                        # empty (rate limit) rather than blanking the columns.
                        if aux["analysts"]:
                            analyst_cache = aux["analysts"]
                        last_analyst_update = now

                    last_update = datetime.now().strftime("%H:%M:%S")

                    if summary_cache:
                        save_cached_portfolio(
                            holdings,
                            target_currency,
                            list(summary_cache.values()),
                            list(dividend_cache.values()),
                            news_cache,
                            history_points,
                            monthly_changes,
                            analysts=analyst_cache,
                        )

                    if not args.watch:
                        break

                    # A trading day means at least one held exchange printed a
                    # bar today (real data), or — failing that — some price
                    # actually moved. Far more reliable than movement alone for
                    # picking the off-hours refresh cadence.
                    trading_day = (
                        traded_symbols is None
                        or len(traded_symbols) > 0
                        or _has_market_activity(list(summary_cache.values()))
                    )
                    market_open = is_any_market_open(holdings) and trading_day
                    effective_interval = interval if market_open else max(interval, 300)
                    market_tag = "" if market_open else " | [Market Closed]"
                    fail_tag = (
                        f" | ⚠ {len(failed_symbols)} failed" if failed_symbols else ""
                    )
                    msg = (
                        f"Last update: {last_update} | Next in {effective_interval}s"
                        f"{market_tag}{fail_tag} | Ctrl+C to exit"
                    )
                    live.update(
                        build_display_group(
                            list(summary_cache.values()),
                            list(dividend_cache.values()),
                            target_currency,
                            msg,
                            history_points,
                            monthly_changes,
                            news_cache,
                            error_symbols=failed_symbols,
                            holdings=holdings,
                            analyst_results=analyst_cache,
                        )
                    )

                    start_wait = time.time()
                    triggered = False
                    ticks = effective_interval * 10
                    for _ in range(ticks):
                        time.sleep(0.1)
                        if (
                            args.watch
                            and sys.stdin.isatty()
                            and select_mod
                            and select_mod.select([sys.stdin], [], [], 0)[0]
                        ):
                            while select_mod.select([sys.stdin], [], [], 0)[0]:
                                sys.stdin.read(1)
                            triggered = True
                            break
                        if time.time() - start_wait > effective_interval + 5:
                            triggered = True
                            break

                    if triggered:
                        rate_cache.clear()
                        prev_close_cache.clear()
                        continue
            finally:
                if args.watch and sys.stdin.isatty() and termios:
                    sys.stdout.write("\033[?1004l")
                    sys.stdout.flush()
                    if old_attr:
                        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_attr)

        if not args.watch:
            console.print(
                build_display_group(
                    list(summary_cache.values()),
                    list(dividend_cache.values()),
                    target_currency,
                    "",
                    history_points,
                    monthly_changes,
                    news_cache,
                    error_symbols=failed_symbols,
                    holdings=holdings,
                    analyst_results=analyst_cache,
                )
            )

    except KeyboardInterrupt:
        console.print("\n[yellow]Watch mode stopped.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    fetch_portfolio()
