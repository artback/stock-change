import os
import yfinance as yf
import logging
import yaml
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from datetime import datetime

# Suppress yfinance logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

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
    """Loads holdings from ~/.stock_price.yaml or returns defaults."""
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
                if config and "holdings" in config:
                    return config["holdings"]
        except Exception as e:
            print(f"Error loading config: {e}")
    return DEFAULT_HOLDINGS

def get_exchange_rate():
    """Fetches EUR/SEK exchange rate using yfinance."""
    try:
        ticker = yf.Ticker("EURSEK=X")
        rate = ticker.fast_info['last_price']
        return 1 / rate
    except Exception:
        return 0.088

def fetch_portfolio():
    console = Console()
    holdings = load_config()
    sek_to_eur = get_exchange_rate()

    # --- UI SETUP ---
    table = Table(title="Portfolio Summary", header_style="bold cyan")
    table.add_column("Ticker")
    table.add_column("Quantity", justify="right")
    table.add_column("Value (€)", justify="right", style="bold white")
    table.add_column("Day %", justify="right")

    div_table = Table(title="Upcoming Dividends", header_style="bold magenta")
    div_table.add_column("Ticker")
    div_table.add_column("Ex-Date", justify="center")
    div_table.add_column("Amount", justify="right")
    div_table.add_column("Total (€)", justify="right", style="green")

    total_val_eur = 0
    total_prev_val_eur = 0
    found_divs = False
    
    # Process Holdings
    for ticker_symbol, qty in holdings.items():
        try:
            t = yf.Ticker(ticker_symbol)
            info = t.fast_info
            
            price = info['last_price']
            prev_close = info['previous_close']
            currency = info.get('currency', 'EUR')
            
            conv = sek_to_eur if currency == 'SEK' else 1.0
            
            val_now = (price * conv) * qty
            val_prev = (prev_close * conv) * qty
            
            total_val_eur += val_now
            total_prev_val_eur += val_prev
            
            chg_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0
            
            table.add_row(
                ticker_symbol, 
                f"{qty:,}",
                f"{val_now:,.2f} €", 
                Text(f"{chg_pct:+.2f}%", style="green" if chg_pct >= 0 else "red")
            )
            
            # Future Dividends from calendar
            cal = t.calendar
            if cal and 'Ex-Dividend Date' in cal:
                ex_date = cal['Ex-Dividend Date']
                if ex_date and ex_date >= datetime.now().date():
                    div_amt = t.info.get('lastDividendValue') or t.info.get('dividendRate') or 0
                    if div_amt > 0:
                        total_p = (div_amt * conv) * qty
                        cur_symbol = "kr" if currency == "SEK" else "€"
                        div_table.add_row(
                            ticker_symbol, 
                            str(ex_date), 
                            f"{div_amt:.2f} {cur_symbol}", 
                            f"{total_p:,.2f} €"
                        )
                        found_divs = True
        except Exception:
            continue

    # --- FINAL DISPLAY ---
    console.print(table)
    if found_divs:
        console.print(div_table)
    
    if total_prev_val_eur > 0:
        day_chg = ((total_val_eur - total_prev_val_eur) / total_prev_val_eur) * 100
        summary = Text.assemble(
            ("TOTAL VALUE:  ", "white"), (f"{total_val_eur:,.2f} €\n", "bold white"),
            ("DAY CHANGE:   ", "white"), (f"{day_chg:+.2f}%", "bold green" if day_chg >= 0 else "bold red")
        )
        console.print(Panel(summary, border_style="bright_blue", expand=False))

if __name__ == "__main__":
    fetch_portfolio()
