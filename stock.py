import os
import yfinance as yf
import logging
import yaml
import time
import argparse
import concurrent.futures
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from datetime import datetime

__version__ = "0.1.11"

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

def load_config():
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
                if config and "holdings" in config:
                    return config["holdings"]
        except Exception as e:
            console.print(f"[red]Error loading config:[/red] {e}")
    return DEFAULT_HOLDINGS

def get_exchange_rate():
    try:
        ticker = yf.Ticker("EURSEK=X")
        rate = ticker.fast_info['lastPrice']
        return 1 / rate if rate else 0.088
    except:
        return 0.088

def get_ticker_summary(symbol, qty, sek_to_eur):
    """Fetches just the price data for the summary table."""
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        price = fi.get('lastPrice')
        prev_close = fi.get('previousClose')
        currency = fi.get('currency', 'EUR')
        conv = sek_to_eur if currency == 'SEK' else 1.0
        
        if price is not None:
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty if prev_close else val_now
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            return {
                "symbol": symbol, "qty": qty, "val_now": val_now,
                "val_prev": val_prev, "chg_pct": chg_pct, "ticker_obj": t, "conv": conv, "currency": currency
            }
    except:
        pass
    return None

def get_dividend_data(summary_data):
    """Fetches dividend data using an existing Ticker object."""
    try:
        t = summary_data['ticker_obj']
        qty = summary_data['qty']
        conv = summary_data['conv']
        currency = summary_data['currency']
        
        cal = t.calendar
        if cal and 'Ex-Dividend Date' in cal:
            ex_date = cal['Ex-Dividend Date']
            if ex_date and ex_date >= datetime.now().date():
                d_info = t.info
                div_amt = d_info.get('lastDividendValue') or d_info.get('dividendRate') or 0
                if div_amt > 0:
                    return {
                        "symbol": summary_data['symbol'], "ex_date": ex_date,
                        "amt": div_amt, "total_p": (div_amt * conv) * qty,
                        "cur_symbol": "kr" if currency == "SEK" else "€"
                    }
    except:
        pass
    return None

def generate_table(summary_results):
    table = Table(title="Portfolio Summary", header_style="bold cyan")
    table.add_column("Ticker")
    table.add_column("Quantity", justify="right")
    table.add_column("Value (€)", justify="right", style="bold white")
    table.add_column("Day %", justify="right")
    
    total_val = 0
    total_prev = 0
    for s in sorted(summary_results, key=lambda x: x['symbol']):
        total_val += s['val_now']
        total_prev += s['val_prev']
        table.add_row(
            s['symbol'], f"{s['qty']:,}", f"{s['val_now']:,.2f} €",
            Text(f"{s['chg_pct']:+.2f}%", style="green" if s['chg_pct'] >= 0 else "red")
        )
    return table, total_val, total_prev

def fetch_portfolio():
    parser = argparse.ArgumentParser(description="Track stock prices and dividends")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()

    holdings = load_config()
    summary_results = []
    dividend_results = []
    
    # Use Live display for real-time updates
    with Live(Spinner("dots", text="Initializing..."), console=console, refresh_per_second=4) as live:
        # Step 1: Exchange rate
        live.update(Spinner("dots", text="Fetching exchange rates..."))
        sek_to_eur = get_exchange_rate()
        
        # Step 2: Fetch Summary Data in parallel
        live.update(Spinner("dots", text="Fetching portfolio prices..."))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(holdings)) as executor:
            futures = [executor.submit(get_ticker_summary, s, q, sek_to_eur) for s, q in holdings.items()]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    summary_results.append(res)
                    # Update the live display as each row comes in
                    table, _, _ = generate_table(summary_results)
                    live.update(table)

        # Step 3: Fetch Dividend Data in parallel using existing Ticker objects
        if summary_results:
            live.update(Panel(generate_table(summary_results)[0], subtitle="Fetching dividends...", subtitle_align="right", border_style="cyan"))
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(summary_results)) as executor:
                div_futures = [executor.submit(get_dividend_data, s) for s in summary_results]
                for future in concurrent.futures.as_completed(div_futures):
                    res = future.result()
                    if res:
                        dividend_results.append(res)
        
        # Final Assembly
        main_table, total_val, total_prev = generate_table(summary_results)
        
        div_table = None
        if dividend_results:
            div_table = Table(title="Upcoming Dividends", header_style="bold magenta")
            div_table.add_column("Ticker")
            div_table.add_column("Ex-Date", justify="center")
            div_table.add_column("Amount", justify="right")
            div_table.add_column("Total (€)", justify="right", style="green")
            for d in sorted(dividend_results, key=lambda x: x['ex_date']):
                div_table.add_row(d['symbol'], str(d['ex_date']), f"{d['amt']:.2f} {d['cur_symbol']}", f"{d['total_p']:,.2f} €")

        summary_panel = None
        if total_prev > 0:
            day_chg = ((total_val - total_prev) / total_prev) * 100
            summary_panel = Panel(Text.assemble(
                ("TOTAL VALUE:  ", "white"), (f"{total_val:,.2f} €\n", "bold white"),
                ("DAY CHANGE:   ", "white"), (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red")
            ), border_style="bright_blue", expand=False)

        # Update live display with final layout
        layout = Layout()
        layout.split_column(Layout(name="main"), Layout(name="div"), Layout(name="sum"))
        live.update(main_table) # Show final main table
        
    # After Live ends, print the final tables normally so they stay in terminal scrollback
    console.print(main_table)
    if div_table: console.print(div_table)
    if summary_panel: console.print(summary_panel)

if __name__ == "__main__":
    fetch_portfolio()
