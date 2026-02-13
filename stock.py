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
from rich.console import Group
from datetime import datetime

__version__ = "0.1.18"

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

def validate_currency(currency_code):
    currency_code = currency_code.upper()
    if len(currency_code) != 3: return False
    if currency_code == "USD": return True
    try:
        ticker = yf.Ticker(f"USD{currency_code}=X")
        if ticker.fast_info.get('lastPrice'): return True
    except: pass
    return False

def get_rate(source, target, cache):
    if source == target: return 1.0
    pair = f"{source}{target}=X"
    if pair in cache: return cache[pair]
    try:
        ticker = yf.Ticker(pair)
        rate = ticker.fast_info['lastPrice']
        cache[pair] = rate
        return rate
    except:
        try:
            inverse_pair = f"{target}{source}=X"
            ticker = yf.Ticker(inverse_pair)
            rate = 1 / ticker.fast_info['lastPrice']
            cache[pair] = rate
            return rate
        except: return None

def get_ticker_summary(symbol, qty, target_currency, rate_cache):
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        price = fi.get('lastPrice')
        prev_close = fi.get('previousClose')
        source_currency = fi.get('currency', 'USD')
        conv = get_rate(source_currency, target_currency, rate_cache)
        
        if price is not None and conv is not None:
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty if prev_close else val_now
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            return {
                "symbol": symbol, "qty": qty, "val_now": val_now,
                "val_prev": val_prev, "chg_pct": chg_pct, "ticker_obj": t, 
                "conv": conv, "source_currency": source_currency
            }
    except: pass
    return None

def get_dividend_data(summary_data):
    try:
        t = summary_data['ticker_obj']
        cal = t.calendar
        if cal and 'Ex-Dividend Date' in cal:
            ex_date = cal['Ex-Dividend Date']
            if ex_date and ex_date >= datetime.now().date():
                d_info = t.info
                div_amt = d_info.get('lastDividendValue') or d_info.get('dividendRate') or 0
                if div_amt > 0:
                    return {
                        "symbol": summary_data['symbol'], "ex_date": ex_date,
                        "amt": div_amt, "total_p": (div_amt * summary_data['conv']) * summary_data['qty'],
                        "cur_label": CURRENCY_SYMBOLS.get(summary_data['source_currency'], summary_data['source_currency'])
                    }
    except: pass
    return None

def build_display_group(summary_results, dividend_results, target_currency, status_msg=""):
    target_symbol = CURRENCY_SYMBOLS.get(target_currency, target_currency)
    
    # 1. Summary Table
    table = Table(title=f"Portfolio Summary ({target_currency})", header_style="bold cyan", expand=True)
    table.add_column("Ticker")
    table.add_column("Quantity", justify="right")
    table.add_column(f"Value ({target_symbol})", justify="right", style="bold white")
    table.add_column("Day %", justify="right")
    
    total_val = 0
    total_prev = 0
    for s in sorted(summary_results, key=lambda x: x['symbol']):
        total_val += s['val_now']
        total_prev += s['val_prev']
        table.add_row(
            s['symbol'], f"{s['qty']:,}", f"{s['val_now']:,.2f} {target_symbol}",
            Text(f"{s['chg_pct']:+.2f}%", style="green" if s['chg_pct'] >= 0 else "red")
        )

    # 2. Dividends Table
    div_table = None
    if dividend_results:
        div_table = Table(title="Upcoming Dividends", header_style="bold magenta", expand=True)
        div_table.add_column("Ticker")
        div_table.add_column("Ex-Date", justify="center")
        div_table.add_column("Amount", justify="right")
        div_table.add_column(f"Total ({target_symbol})", justify="right", style="green")
        for d in sorted(dividend_results, key=lambda x: x['ex_date']):
            div_table.add_row(d['symbol'], str(d['ex_date']), f"{d['amt']:.2f} {d['cur_label']}", f"{d['total_p']:,.2f} {target_symbol}")

    # 3. Summary Panel
    summary_panel = None
    if total_prev > 0:
        day_chg = ((total_val - total_prev) / total_prev) * 100
        summary_panel = Panel(Text.assemble(
            ("TOTAL VALUE:  ", "white"), (f"{total_val:,.2f} {target_symbol}\n", "bold white"),
            ("DAY CHANGE:   ", "white"), (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red")
        ), border_style="bright_blue", expand=True)

    # 4. Footer
    footer = Text(status_msg, style="dim italic") if status_msg else Text("")
    
    elements = [table]
    if div_table: elements.append(div_table)
    if summary_panel: elements.append(summary_panel)
    elements.append(footer)
    
    return Group(*elements)

def fetch_portfolio():
    parser = argparse.ArgumentParser(description="Track stock prices and dividends")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--currency", help="Output currency (e.g. USD, EUR, SEK)")
    parser.add_argument("-w", "--watch", action="store_true", help="Watch mode: update every 5 seconds")
    args = parser.parse_args()

    config = load_config()
    target_currency = (args.currency or config["currency"]).upper()
    holdings = config["holdings"]
    
    try:
        # Initial validation outside Live to keep it clean
        if not validate_currency(target_currency):
            console.print(f"[bold red]ERROR:[/bold red] '{target_currency}' is not a valid ISO currency code.")
            sys.exit(1)

        summary_results = []
        dividend_results = []

        with Live(build_display_group([], [], target_currency, "Initializing..."), console=console, refresh_per_second=4, transient=True) as live:
            while True:
                current_summary = []
                current_dividends = []
                rate_cache = {}
                
                # Update prices
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(holdings)) as executor:
                    futures = [executor.submit(get_ticker_summary, s, q, target_currency, rate_cache) for s, q in holdings.items()]
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            current_summary.append(res)
                            live.update(build_display_group(current_summary, current_dividends, target_currency, "Updating prices..."))

                # Update dividends (using existing ticker objects from summary)
                if current_summary:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(current_summary)) as executor:
                        div_futures = [executor.submit(get_dividend_data, s) for s in current_summary]
                        for future in concurrent.futures.as_completed(div_futures):
                            res = future.result()
                            if res: 
                                current_dividends.append(res)
                                live.update(build_display_group(current_summary, current_dividends, target_currency, "Updating dividends..."))
                
                # Update "persistent" results for this loop
                summary_results, dividend_results = current_summary, current_dividends
                
                if not args.watch:
                    break
                
                # Wait loop for watch mode
                for i in range(5, 0, -1):
                    msg = f"Next refresh in {i}s... (Ctrl+C to exit)"
                    live.update(build_display_group(summary_results, dividend_results, target_currency, msg))
                    time.sleep(1)

        # Final print after Live context (to keep it in history)
        if not args.watch:
            console.print(build_display_group(summary_results, dividend_results, target_currency))
                
    except KeyboardInterrupt:
        console.print("\n[yellow]Watch mode stopped.[/yellow]")
        sys.exit(0)

if __name__ == "__main__":
    fetch_portfolio()
