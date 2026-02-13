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
import pandas as pd

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


def validate_currency(currency_code):
    currency_code = currency_code.upper()
    if len(currency_code) != 3:
        return False
    if currency_code == "USD":
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


def get_ticker_summary(symbol, qty, target_currency, rate_cache):
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        price = fi.get("lastPrice")
        prev_close = fi.get("previousClose")
        source_currency = fi.get("currency", "USD")
        conv = get_rate(source_currency, target_currency, rate_cache)

        if price is not None and conv is not None:
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty if prev_close else val_now
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            return {
                "symbol": symbol,
                "qty": qty,
                "val_now": val_now,
                "val_prev": val_prev,
                "chg_pct": chg_pct,
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
            if ex_date and ex_date >= datetime.now().date():
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


def render_sparkline(values):
    if not values or len(values) < 2:
        return ""
    chars = " ▂▃▄▅▆▇█"
    min_v, max_v = min(values), max(values)
    span = max_v - min_v
    if span <= 0:
        return "█" * len(values)
    return "".join(chars[min(int((v - min_v) / span * 7), 7)] for v in values)


def fetch_history(holdings, target_currency, ticker_to_currency):
    try:
        symbols = list(holdings.keys())
        currencies = set(ticker_to_currency.values())
        rate_pairs = [
            f"{c}{target_currency}=X" for c in currencies if c != target_currency
        ]

        all_to_fetch = symbols + rate_pairs
        df = yf.download(
            all_to_fetch, period="1mo", interval="1d", progress=False, threads=True
        )

        if df.empty:
            return []

        close_data = df["Close"]
        if isinstance(close_data, pd.Series):
            sym = all_to_fetch[0]
            close_data = pd.DataFrame({sym: close_data})

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

        return history_totals
    except Exception:
        return []


def build_display_group(
    summary_results,
    dividend_results,
    target_currency,
    footer_text="",
    history_points=None,
):
    target_symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)

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
    table.add_column("Day %", justify="right", width=10, no_wrap=True)

    total_val = 0
    total_prev = 0
    for s in sorted(summary_results, key=lambda x: x["symbol"]):
        total_val += s["val_now"]
        total_prev += s["val_prev"]
        table.add_row(
            s["symbol"],
            f"{s['qty']:,}",
            f"{s['val_now']:,.2f} {target_symbol}",
            Text(
                f"{s['chg_pct']:+.2f}%", style="green" if s["chg_pct"] >= 0 else "red"
            ),
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

    # 3. Summary Panel
    summary_panel = None
    if total_prev > 0:
        day_chg = ((total_val - total_prev) / total_prev) * 100
        summary_text = Text.assemble(
            ("TOTAL VALUE:  ", "white"),
            (f"{total_val:,.2f} {target_symbol}\n", "bold white"),
            ("DAY CHANGE:   ", "white"),
            (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red"),
        )

        if history_points and len(history_points) > 1:
            spark = render_sparkline(history_points)
            summary_text.append("\n\n")
            summary_text.append("30D DEVELOPMENT:\n", style="dim")
            summary_text.append(spark, style="bright_cyan")

        summary_panel = Panel(summary_text, border_style="bright_blue", expand=False)

    # 4. Footer
    footer = Text(footer_text, style="dim italic") if footer_text else Text("")

    elements = [table]
    if div_table:
        elements.append(div_table)
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
        "-w", "--watch", action="store_true", help="Watch mode: update every 5 seconds"
    )
    parser.add_argument("--config", help="Path to a custom YAML configuration file")
    args = parser.parse_args()

    config = load_config(args.config)
    target_currency = (args.currency or config["currency"]).upper()
    holdings = config["holdings"]

    try:
        if not validate_currency(target_currency):
            console.print(
                f"[bold red]ERROR:[/bold red] '{target_currency}' is not a valid ISO currency code."
            )
            sys.exit(1)

        last_summary = []
        last_dividends = []
        history_points = []
        last_history_update = 0
        ticker_to_currency = {}

        with Live(
            build_display_group([], [], target_currency, "Initializing..."),
            console=console,
            refresh_per_second=4,
            transient=True,
            screen=args.watch,
        ) as live:
            while True:
                current_summary = []
                current_dividends = []
                rate_cache = {}

                # Fetch data
                num_holdings = len(holdings)
                completed = 0
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=num_holdings
                ) as executor:
                    futures = [
                        executor.submit(
                            get_ticker_summary, s, q, target_currency, rate_cache
                        )
                        for s, q in holdings.items()
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        completed += 1
                        if res:
                            current_summary.append(res)
                            ticker_to_currency[res["symbol"]] = res["source_currency"]

                        # Only update if data changed or first run to show progress
                        display_summary = (
                            current_summary if not last_summary else last_summary
                        )
                        live.update(
                            build_display_group(
                                display_summary,
                                last_dividends,
                                target_currency,
                                f"Updating ({completed}/{num_holdings})...",
                                history_points,
                            )
                        )

                # Fetch 30D history if needed (every 120s)
                now = time.time()
                if ticker_to_currency and (
                    now - last_history_update > 120 or not history_points
                ):
                    history_points = fetch_history(
                        holdings, target_currency, ticker_to_currency
                    )
                    last_history_update = now

                if current_summary:
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=len(current_summary)
                    ) as executor:
                        div_futures = [
                            executor.submit(get_dividend_data, s)
                            for s in current_summary
                        ]
                        for future in concurrent.futures.as_completed(div_futures):
                            res = future.result()
                            if res:
                                current_dividends.append(res)

                # Update persistent results
                last_summary, last_dividends = current_summary, current_dividends
                last_update = datetime.now().strftime("%H:%M:%S")

                if not args.watch:
                    break

                # Update display with last update time and wait
                msg = f"Last update: {last_update} | Ctrl+C to exit"
                live.update(
                    build_display_group(
                        last_summary,
                        last_dividends,
                        target_currency,
                        msg,
                        history_points,
                    )
                )

                # Smooth wait loop (5 seconds)
                for _ in range(50):
                    time.sleep(0.1)

        if not args.watch:
            console.print(
                build_display_group(last_summary, last_dividends, target_currency)
            )

    except KeyboardInterrupt:
        console.print("\n[yellow]Watch mode stopped.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    fetch_portfolio()
