import os
import yfinance as yf
import logging
import yaml
import time
import argparse
import concurrent.futures
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.console import Group
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

try:
    import termios
    import select as select_mod
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


EXCHANGE_SCHEDULES = {
    ".ST": ("Europe/Stockholm", 9, 17),
    ".HE": ("Europe/Helsinki", 10, 18),
    ".CO": ("Europe/Copenhagen", 9, 17),
    ".OL": ("Europe/Oslo", 9, 16),
    ".PA": ("Europe/Paris", 9, 17),
    ".DE": ("Europe/Berlin", 9, 17),
    ".AS": ("Europe/Amsterdam", 9, 17),
    ".BR": ("Europe/Brussels", 9, 17),
    ".MI": ("Europe/Rome", 9, 17),
    ".MC": ("Europe/Madrid", 9, 17),
    ".SW": ("Europe/Zurich", 9, 17),
    ".VI": ("Europe/Vienna", 9, 17),
    ".L": ("Europe/London", 8, 16),
    ".LS": ("Europe/Lisbon", 8, 16),
    ".T": ("Asia/Tokyo", 9, 15),
    ".HK": ("Asia/Hong_Kong", 9, 16),
    ".SI": ("Asia/Singapore", 9, 17),
    ".AX": ("Australia/Sydney", 10, 16),
    ".NZ": ("Pacific/Auckland", 10, 16),
    ".TO": ("America/Toronto", 9, 16),
    ".SA": ("America/Sao_Paulo", 10, 17),
    ".MX": ("America/Mexico_City", 8, 15),
}

DEFAULT_SCHEDULE = ("America/New_York", 9, 16)


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
        if local_now.weekday() < 5 and open_hour <= local_now.hour < close_hour:
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


def load_config(config_path=None):
    config_data = {"holdings": DEFAULT_HOLDINGS, "currency": "EUR"}

    # Priority: 1. CLI Arg, 2. Env Var, 3. Default Path
    resolved_path = Path(config_path) if config_path else None
    if not resolved_path:
        env_path = os.environ.get("STOCK_PRICE_CONFIG")
        resolved_path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH

    if resolved_path.exists():
        try:
            with open(resolved_path, "r") as f:
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


KNOWN_CURRENCIES = {"EUR", "USD", "GBP", "SEK", "JPY", "CHF", "CAD", "AUD", "NOK", "DKK", "CNY", "HKD", "SGD", "NZD", "KRW", "INR", "BRL", "MXN", "ZAR", "TRY", "PLN", "CZK", "HUF", "ILS", "TWD", "THB"}


def validate_currency(currency_code):
    currency_code = currency_code.upper()
    if len(currency_code) != 3:
        return False
    if currency_code in KNOWN_CURRENCIES:
        return True
    try:
        ticker = yf.Ticker(f"USD{currency_code}=X")
        if ticker.fast_info.get("lastPrice"):
            return True
    except Exception:
        pass
    return False


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


def get_ticker_summary(symbol, qty, target_currency, rate_cache):
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        price = fi.get("lastPrice")
        prev_close = fi.get("regularMarketPreviousClose")
        if prev_close is None or pd.isna(prev_close):
            prev_close = fi.get("previousClose")
        if prev_close is None or pd.isna(prev_close):
            prev_close = _previous_close_from_history(t)
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

    for symbol, articles in zip(symbols, raw_news):
        try:
            for article in articles[:3]:
                content = article.get("content", {})
                title = content.get("title", "")
                if not title or title in seen_titles:
                    continue
                pub_date_raw = content.get("pubDate", "")
                pub_dt = None
                if pub_date_raw:
                    try:
                        pub_dt = datetime.fromisoformat(
                            pub_date_raw.replace("Z", "+00:00")
                        )
                    except Exception:
                        pass
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

        # Determine which exchanges actually traded today. Per-exchange
        # holidays mean some may be open while others are closed, but all
        # tickers on one exchange share a calendar — so we decide per
        # exchange, not per ticker. yfinance's daily bar for the in-progress
        # session lags for some tickers, so requiring every ticker to have a
        # today bar would wrongly flag live tickers as untraded (and the
        # watch loop would zero out their real fast_info day change).
        today = pd.Timestamp.now().normalize()
        original_today = (
            df["Close"].loc[today]
            if today in df["Close"].index
            else pd.Series(dtype=float)
        )
        traded_suffixes = {
            _get_exchange_suffix(sym)
            for sym in symbols
            if sym in original_today.index and pd.notna(original_today[sym])
        }
        traded_today = {
            sym for sym in symbols if _get_exchange_suffix(sym) in traded_suffixes
        }

        return history_totals, monthly_changes, traded_today
    except Exception:
        return [], {}, set()


def build_display_group(
    summary_results,
    dividend_results,
    target_currency,
    footer_text="",
    history_points=None,
    monthly_changes=None,
    news_items=None,
):
    target_symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)
    monthly_changes = monthly_changes or {}

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

        table.add_row(
            s["symbol"],
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
        )

    if summary_results:
        total_chg_pct = (
            ((total_val - total_prev) / total_prev) * 100
            if total_prev != 0
            else 0
        )
        table.add_section()
        table.add_row(
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
        )

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
    args = parser.parse_args()

    config = load_config(args.config)
    target_currency = (args.currency or config["currency"]).upper()
    holdings = config["holdings"]

    # Set terminal title
    if sys.stdout.isatty():
        sys.stdout.write("\033]0;Stock Price\007")
        sys.stdout.flush()

    try:
        if not validate_currency(target_currency):
            console.print(
                f"[bold red]ERROR:[/bold red] '{target_currency}' is not a valid ISO currency code."
            )
            sys.exit(1)

        summary_cache = {}
        dividend_cache = {}
        news_cache = []
        history_points = []
        monthly_changes = {}
        last_history_update = 0
        last_dividend_update = 0
        last_news_update = 0
        traded_symbols = None
        ticker_to_currency = {}
        rate_cache = {}
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
                    # Fetch data
                    num_holdings = len(holdings)
                    completed = 0
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=max(1, num_holdings)
                    ) as executor:
                        future_to_symbol = {
                            executor.submit(
                                get_ticker_summary, s, q, target_currency, rate_cache
                            ): s
                            for s, q in holdings.items()
                        }
                        try:
                            for future in concurrent.futures.as_completed(
                                future_to_symbol, timeout=15
                            ):
                                res = future.result()
                                symbol = future_to_symbol[future]
                                completed += 1
                                if res:
                                    summary_cache[symbol] = res
                                    ticker_to_currency[symbol] = res["source_currency"]

                                live.update(
                                    build_display_group(
                                        list(summary_cache.values()),
                                        list(dividend_cache.values()),
                                        target_currency,
                                        f"Updating ({completed}/{num_holdings})...",
                                        history_points,
                                        monthly_changes,
                                        news_cache,
                                    )
                                )
                        except concurrent.futures.TimeoutError:
                            # Continue with what we have if some requests timed out
                            pass

                    # Re-apply the last known holiday state so zeroed tickers
                    # don't flicker back to their stale delta after the summary
                    # refresh overwrote them.
                    apply_holiday_zeroing(summary_cache, traded_symbols)

                    # History (30D, every 120s), dividends and news (every 600s)
                    # only read the summary/holdings, so refresh whichever are
                    # due concurrently rather than serially — news in particular
                    # dominated cold-start time when run last in a chain.
                    now = time.time()
                    history_due = ticker_to_currency and (
                        now - last_history_update > 120 or not history_points
                    )
                    dividends_due = summary_cache and (
                        now - last_dividend_update > 600 or not dividend_cache
                    )
                    news_due = summary_cache and (
                        now - last_news_update > 600 or not news_cache
                    )

                    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                        hist_future = (
                            pool.submit(
                                fetch_history,
                                holdings,
                                target_currency,
                                ticker_to_currency,
                            )
                            if history_due
                            else None
                        )
                        div_future = (
                            pool.submit(
                                fetch_all_dividends, list(summary_cache.values())
                            )
                            if dividends_due
                            else None
                        )
                        news_future = (
                            pool.submit(get_news_data, list(summary_cache.keys()))
                            if news_due
                            else None
                        )

                        if hist_future is not None:
                            new_history, new_monthly, traded = hist_future.result()
                            if new_history:
                                history_points = new_history
                            if new_monthly:
                                monthly_changes.update(new_monthly)
                            traded_symbols = traded
                            last_history_update = now
                            apply_holiday_zeroing(summary_cache, traded_symbols)

                        if div_future is not None:
                            dividend_cache = div_future.result()
                            last_dividend_update = now

                        if news_future is not None:
                            news_cache = news_future.result()
                            last_news_update = now

                    last_update = datetime.now().strftime("%H:%M:%S")

                    if not args.watch:
                        break

                    market_open = is_any_market_open(holdings) and _has_market_activity(
                        list(summary_cache.values())
                    )
                    effective_interval = interval if market_open else max(interval, 300)
                    market_tag = "" if market_open else " | [Market Closed]"
                    msg = f"Last update: {last_update} | Next in {effective_interval}s{market_tag} | Ctrl+C to exit"
                    live.update(
                        build_display_group(
                            list(summary_cache.values()),
                            list(dividend_cache.values()),
                            target_currency,
                            msg,
                            history_points,
                            monthly_changes,
                            news_cache,
                        )
                    )

                    start_wait = time.time()
                    triggered = False
                    ticks = effective_interval * 10
                    for _ in range(ticks):
                        time.sleep(0.1)
                        if args.watch and sys.stdin.isatty() and select_mod:
                            if select_mod.select([sys.stdin], [], [], 0)[0]:
                                while select_mod.select([sys.stdin], [], [], 0)[0]:
                                    sys.stdin.read(1)
                                triggered = True
                                break
                        if time.time() - start_wait > effective_interval + 5:
                            triggered = True
                            break

                    if triggered:
                        rate_cache.clear()
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
                )
            )

    except KeyboardInterrupt:
        console.print("\n[yellow]Watch mode stopped.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    fetch_portfolio()
