# Feature Recommendations for stock-change

Analysis of the current codebase (v0.4.0) with prioritized feature recommendations.

---

## High Impact — Low Effort

### 1. Retry Logic with Exponential Backoff
**Problem:** API calls to yfinance silently return `None` on failure — the user just sees a `-` with no explanation.

**Recommendation:** Wrap `yfinance` calls with a retry decorator (2-3 attempts, exponential backoff). Log warnings on transient failures so users know *why* data is missing.

### 2. Persistent Exchange Rate Cache
**Problem:** Exchange rates are fetched fresh every run. They change slowly but cost an API call per currency pair.

**Recommendation:** Cache rates to a local file (`~/.stock_price_cache.json`) with a configurable TTL (e.g. 1 hour). This cuts startup time significantly for multi-currency portfolios.

### 3. Configurable Sort Order
**Problem:** Holdings are sorted alphabetically. Users may prefer sorting by value, daily change %, or dividend yield.

**Recommendation:** Add a `--sort` flag (`value`, `change`, `change_pct`, `name`, `dividend`) and a `sort` key in the YAML config.

### 4. Ticker Validation on Startup
**Problem:** Invalid ticker symbols silently fail. Users can have a typo in their config and not realize data is missing.

**Recommendation:** Validate all tickers on first run (or when config changes) and warn about unrecognized symbols before proceeding.

---

## High Impact — Medium Effort

### 5. Cost Basis & Gain/Loss Tracking
**Problem:** The tool shows current value and daily change but has no concept of purchase price.

**Recommendation:** Extend the YAML config to accept `{ qty: 100, cost: 150.50 }` per holding. Display total gain/loss (absolute + %) alongside current value. Maintain backward compatibility with the existing `ticker: qty` format.

### 6. Price Alerts
**Problem:** No way to get notified when a stock hits a target price.

**Recommendation:** Add an `alerts` section to the YAML config:
```yaml
alerts:
  AAPL:
    above: 200
    below: 150
```
In watch mode, flash a visual alert (rich `Panel` with red/green border). Optionally support desktop notifications via `notify-send` / `osascript`.

### 7. Portfolio Export (CSV / JSON)
**Problem:** No way to export portfolio data for spreadsheets, dashboards, or record-keeping.

**Recommendation:** Add `--export csv` and `--export json` flags that dump the current portfolio snapshot to stdout or a file. This also enables piping into other tools.

### 8. Multiple Portfolio Support
**Problem:** Single config file limits users to one portfolio view.

**Recommendation:** Support named portfolio sections in the YAML:
```yaml
portfolios:
  retirement:
    currency: USD
    holdings:
      AAPL: 100
  trading:
    currency: EUR
    holdings:
      MC.PA: 45
```
Add `--portfolio <name>` flag. Default to showing all.

---

## Medium Impact — Low Effort

### 9. Total Portfolio Summary Row
**Problem:** Individual stock data is shown but there's no aggregated portfolio total line.

**Recommendation:** Add a summary row at the bottom: total portfolio value, total daily change (absolute + %), and weighted average daily change %.

### 10. Color-Coded Sparklines
**Problem:** Sparkline rendering exists but could be more informative.

**Recommendation:** Color the sparkline green/red based on whether the trend is up or down over the 30-day window. Use `rich` markup for inline coloring.

### 11. Offline / Market-Closed Detection
**Problem:** During market-closed hours, the tool still fetches data every 30 seconds — wasting API calls and showing stale data.

**Recommendation:** Detect if the market is closed (weekends, after-hours) and either skip refreshes or extend the interval to 10+ minutes. Show a `[Market Closed]` indicator.

### 12. `--once` Flag for Scripting
**Problem:** The tool currently supports `--watch` for continuous updates, but there's no explicit single-shot mode for use in scripts or cron jobs.

**Recommendation:** Add `--once` (or make it the default without `--watch`) that fetches, displays, and exits with a proper exit code (0 = success, 1 = partial failure).

---

## Medium Impact — Medium Effort

### 13. Historical Performance Comparison
**Problem:** Only 30-day trend is shown. No way to compare performance over different time periods.

**Recommendation:** Add `--period` flag supporting `1w`, `1m`, `3m`, `6m`, `1y`, `ytd`. Show the percentage change over the selected period alongside the daily change.

### 14. Sector / Tag Grouping
**Problem:** All holdings are displayed in a flat list regardless of sector or asset type.

**Recommendation:** Allow tagging holdings in the config:
```yaml
holdings:
  AAPL:
    qty: 100
    tags: [tech, us]
```
Group display by tag with subtotals.

### 15. Dividend Reinvestment Projection
**Problem:** Dividend calendar shows upcoming payouts but doesn't project forward.

**Recommendation:** Add a `--dividend-summary` view showing: annual dividend income estimate, yield on cost (if cost basis is tracked), and next 12-month payout calendar.

---

## Lower Priority — Higher Effort (Future)

### 16. TUI Dashboard Mode
Use `textual` (from the `rich` ecosystem) to build an interactive terminal UI with keyboard navigation, resizable panes, and real-time charts.

### 17. Webhook / Notification Integrations
Push alerts to Slack, Telegram, or email when price targets are hit or dividends are announced.

### 18. Plugin Architecture
Allow users to add custom data sources or display modules. Useful for crypto, bonds, or alternative assets that yfinance doesn't cover well.

### 19. Tax Reporting
Generate capital gains/losses reports for a tax year. Requires cost basis tracking (#5) as a prerequisite.

---

## Code Quality Improvements (Non-Feature)

These aren't user-facing features but would make all of the above easier to build:

- **Add type hints** — Python 3.10+ is already required; adding type annotations improves maintainability
- **Split `stock.py` into modules** — Separate data fetching, display rendering, and config management
- **Replace silent `except: pass` blocks** — Log warnings instead of swallowing errors
- **Extract magic numbers** — `120`, `600`, `15` second timeouts should be named constants
- **Add structured logging** — Replace `console.print` error messages with proper `logging` calls
