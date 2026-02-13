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
from rich.status import Status
from rich.live import Live
from rich.layout import Layout
from datetime import datetime

__version__ = "0.1.10"

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
        if rate:
            return 1 / rate
    except Exception:
        pass
    return 0.088

def get_ticker_data(symbol, qty, sek_to_eur):
    """Fetches all data for a single ticker."""
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        
        # Summary Data
        price = fi.get('lastPrice')
        prev_close = fi.get('previousClose')
        currency = fi.get('currency', 'EUR')
        conv = sek_to_eur if currency == 'SEK' else 1.0
        
        summary = None
        if price is not None:
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty if prev_close else val_now
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            summary = {
                "symbol": symbol,
                "qty": qty,
                "val_now": val_now,
                "val_prev": val_prev,
                "chg_pct": chg_pct
            }

        # Dividend Data (Fetch separately to allow summary to show first)
        dividend = None
        try:
            cal = t.calendar
            if cal and 'Ex-Dividend Date' in cal:
                ex_date = cal['Ex-Dividend Date']
                if ex_date and ex_date >= datetime.now().date():
                    d_info = t.info
                    div_amt = d_info.get('lastDividendValue') or d_info.get('dividendRate') or 0
                    if div_amt > 0:
                        total_p = (div_amt * conv) * qty
                        dividend = {
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "amt": div_amt,
                            "total_p": total_p,
                            "cur_symbol": "kr" if currency == "SEK" else "€"
                        }
        except:
            pass
            
        return summary, dividend
    except:
        return None, None

def fetch_portfolio():
    parser = argparse.ArgumentParser(description="Track stock prices and dividends")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()

    holdings = load_config()
    
    with Status("[bold green]Fetching exchange rates...", console=console) as status:
        sek_to_eur = get_exchange_rate()
        status.update("[bold green]Fetching portfolio data...")
        
        summary_results = []
        dividend_results = []
        
        # Use ThreadPoolExecutor for parallel fetching
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(holdings)) as executor:
            future_to_ticker = {executor.submit(get_ticker_data, s, q, sek_to_eur): s for s, q in holdings.items()}
            for future in concurrent.futures.as_completed(future_to_ticker):
                s, d = future.result()
                if s: summary_results.append(s)
                if d: dividend_results.append(d)

    # --- UI RENDERING ---
    # Sort results to maintain consistent order
    summary_results.sort(key=lambda x: x['symbol'])
    dividend_results.sort(key=lambda x: x['ex_date'])

    table = Table(title="Portfolio Summary", header_style="bold cyan")
    table.add_column("Ticker")
    table.add_column("Quantity", justify="right")
    table.add_column("Value (€)", justify="right", style="bold white")
    table.add_column("Day %", justify="right")

    total_val_eur = 0
    total_prev_val_eur = 0
    
    for s in summary_results:
        total_val_eur += s['val_now']
        total_prev_val_eur += s['val_prev']
        table.add_row(
            s['symbol'], 
            f"{s['qty']:,}",
            f"{s['val_now']:,.2f} €", 
            Text(f"{s['chg_pct']:+.2f}%", style="green" if s['chg_pct'] >= 0 else "red")
        )

    console.print(table)

    if dividend_results:
        div_table = Table(title="Upcoming Dividends", header_style="bold magenta")
        div_table.add_column("Ticker")
        div_table.add_column("Ex-Date", justify="center")
        div_table.add_column("Amount", justify="right")
        div_table.add_column("Total (€)", justify="right", style="green")
        
        for d in dividend_results:
            div_table.add_row(
                d['symbol'], 
                str(d['ex_date']), 
                f"{d['amt']:.2f} {d['cur_symbol']}", 
                f"{d['total_p']:,.2f} €"
            )
        console.print(div_table)
    
    if total_prev_val_eur > 0:
        day_chg = ((total_val_eur - total_prev_val_eur) / total_prev_val_eur) * 100
        summary_panel = Text.assemble(
            ("TOTAL VALUE:  ", "white"), (f"{total_val_eur:,.2f} €\n", "bold white"),
            ("DAY CHANGE:   ", "white"), (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red")
        )
        console.print(Panel(summary_panel, border_style="bright_blue", expand=False))

if __name__ == "__main__":
    fetch_portfolio()
