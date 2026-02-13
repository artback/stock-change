import os
import yfinance as yf
import logging
import yaml
import time
import argparse
import concurrent.futures
import requests
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from datetime import datetime

__version__ = "0.1.14"

# Suppress yfinance logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
console = Console()

DEFAULT_CONFIG_PATH = Path.home() / ".stock_price.yaml"

DEFAULT_HOLDINGS = {
    "SVOL-B.ST": 8367, 
    "INVE-B.ST": 1387, 
    "LIFCO-B.ST": 5,
    "MC.PA": 45, 
    "INDU-C.ST": 21, 
    "IUSA.DE": 720
}

# Simple mapping for common currency symbols
CURRENCY_SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "SEK": "kr", 
    "JPY": "¥", "CHF": "Fr", "CAD": "C$", "AUD": "A$"
}

def load_config():
    config_data = {"holdings": DEFAULT_HOLDINGS, "currency": "EUR"}
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r") as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    if "holdings" in user_config:
                        config_data["holdings"] = user_config["holdings"]
                    if "currency" in user_config:
                        config_data["currency"] = user_config["currency"].upper()
        except Exception as e:
            console.print(f"[red]Error loading config:[/red] {e}")
    return config_data

def validate_currency(currency_code, session):
    """Validates the ISO currency code by trying to fetch a USD rate for it."""
    currency_code = currency_code.upper()
    if len(currency_code) != 3:
        return False
    if currency_code == "USD":
        return True
    try:
        # Check if we can get a rate for this currency against USD
        ticker = yf.Ticker(f"USD{currency_code}=X", session=session)
        if ticker.fast_info.get('lastPrice'):
            return True
    except:
        pass
    return False

def get_rate(source, target, session, cache):
    """Fetches and caches the conversion rate from source to target currency."""
    if source == target:
        return 1.0
    pair = f"{source}{target}=X"
    if pair in cache:
        return cache[pair]
    
    try:
        ticker = yf.Ticker(pair, session=session)
        rate = ticker.fast_info['lastPrice']
        cache[pair] = rate
        return rate
    except:
        # Fallback: Try the inverse if the direct pair fails
        try:
            inverse_pair = f"{target}{source}=X"
            ticker = yf.Ticker(inverse_pair, session=session)
            rate = 1 / ticker.fast_info['lastPrice']
            cache[pair] = rate
            return rate
        except:
            return None

def get_ticker_summary(symbol, qty, target_currency, session, rate_cache):
    try:
        t = yf.Ticker(symbol, session=session)
        fi = t.fast_info
        price = fi.get('lastPrice')
        prev_close = fi.get('previousClose')
        source_currency = fi.get('currency', 'USD')
        
        # Get conversion rate (threaded but uses cache to avoid redundant calls)
        conv = get_rate(source_currency, target_currency, session, rate_cache)
        
        if price is not None and conv is not None:
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty if prev_close else val_now
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            return {
                "symbol": symbol, "qty": qty, "val_now": val_now,
                "val_prev": val_prev, "chg_pct": chg_pct, "ticker_obj": t, 
                "conv": conv, "source_currency": source_currency
            }
    except:
        pass
    return None

def get_dividend_data(summary_data, target_currency):
    try:
        t = summary_data['ticker_obj']
        conv = summary_data['conv']
        source_currency = summary_data['source_currency']
        
        cal = t.calendar
        if cal and 'Ex-Dividend Date' in cal:
            ex_date = cal['Ex-Dividend Date']
            if ex_date and ex_date >= datetime.now().date():
                d_info = t.info
                div_amt = d_info.get('lastDividendValue') or d_info.get('dividendRate') or 0
                if div_amt > 0:
                    return {
                        "symbol": summary_data['symbol'], "ex_date": ex_date,
                        "amt": div_amt, "total_p": (div_amt * conv) * summary_data['qty'],
                        "cur_label": CURRENCY_SYMBOLS.get(source_currency, source_currency)
                    }
    except:
        pass
    return None

def generate_table(summary_results, target_currency):
    symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)
    table = Table(title=f"Portfolio Summary ({target_currency})", header_style="bold cyan")
    table.add_column("Ticker")
    table.add_column("Quantity", justify="right")
    table.add_column(f"Value ({symbol})", justify="right", style="bold white")
    table.add_column("Day %", justify="right")
    
    total_val = 0
    total_prev = 0
    for s in sorted(summary_results, key=lambda x: x['symbol']):
        total_val += s['val_now']
        total_prev += s['val_prev']
        table.add_row(
            s['symbol'], f"{s['qty']:,}", f"{s['val_now']:,.2f} {symbol}",
            Text(f"{s['chg_pct']:+.2f}%", style="green" if s['chg_pct'] >= 0 else "red")
        )
    return table, total_val, total_prev

def fetch_portfolio():
    parser = argparse.ArgumentParser(description="Track stock prices and dividends")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--currency", help="Output currency (e.g. USD, EUR, SEK)")
    args = parser.parse_args()

    config = load_config()
    target_currency = (args.currency or config["currency"]).upper()
    holdings = config["holdings"]
    
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    rate_cache = {}

    with Live(Spinner("dots", text=f"Validating currency {target_currency}..."), console=console, refresh_per_second=4) as live:
        if not validate_currency(target_currency, session):
            live.stop()
            console.print(f"[bold red]ERROR:[/bold red] '{target_currency}' is not a valid ISO currency code.")
            sys.exit(1)

        summary_results = []
        dividend_results = []
        
        live.update(Spinner("dots", text=f"Fetching prices in {target_currency}..."))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(holdings)) as executor:
            futures = [executor.submit(get_ticker_summary, s, q, target_currency, session, rate_cache) for s, q in holdings.items()]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    summary_results.append(res)
                    table, _, _ = generate_table(summary_results, target_currency)
                    live.update(table)

        if summary_results:
            live.update(generate_table(summary_results, target_currency)[0])
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(summary_results)) as executor:
                div_futures = [executor.submit(get_dividend_data, s, target_currency) for s in summary_results]
                for future in concurrent.futures.as_completed(div_futures):
                    res = future.result()
                    if res: dividend_results.append(res)
        
        final_main_table, total_val, total_prev = generate_table(summary_results, target_currency)
        target_symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)
        
        div_table = None
        if dividend_results:
            div_table = Table(title="Upcoming Dividends", header_style="bold magenta")
            div_table.add_column("Ticker")
            div_table.add_column("Ex-Date", justify="center")
            div_table.add_column("Amount", justify="right")
            div_table.add_column(f"Total ({target_symbol})", justify="right", style="green")
            for d in sorted(dividend_results, key=lambda x: x['ex_date']):
                div_table.add_row(d['symbol'], str(d['ex_date']), f"{d['amt']:.2f} {d['cur_label']}", f"{d['total_p']:,.2f} {target_symbol}")

        summary_panel = None
        if total_prev > 0:
            day_chg = ((total_val - total_prev) / total_prev) * 100
            summary_panel = Panel(Text.assemble(
                ("TOTAL VALUE:  ", "white"), (f"{total_val:,.2f} {target_symbol}\n", "bold white"),
                ("DAY CHANGE:   ", "white"), (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red")
            ), border_style="bright_blue", expand=False)
        
        live.update(Text(""))

    console.print(final_main_table)
    if div_table: console.print(div_table)
    if summary_panel: console.print(summary_panel)

if __name__ == "__main__":
    fetch_portfolio()
