# Stock Price CLI

A clean, terminal-based stock portfolio tracker and dividend calendar. It supports multiple currencies (EUR/SEK) and displays real-time data using `yfinance`.

![Example Output](https://via.placeholder.com/600x400.png?text=Stock+Price+CLI+Output)

## Features

- 📊 **Portfolio Summary**: Live prices, quantities, and daily percentage changes.
- 💰 **Dividend Calendar**: Upcoming ex-dividend dates and estimated payouts.
- 💱 **Auto Currency Conversion**: Automatic EUR/SEK conversion for Swedish and International stocks.
- ⚙️ **YAML Configuration**: Manage your holdings easily in a separate config file.

## Installation

### Using Homebrew (Recommended)

You can install this directly from GitHub once you've set up your tap:

```bash
brew tap <your-username>/stock-price
brew install stock-price
```

### Manual Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/jonathan/stock_price.git
   cd stock_price
   ```
2. Install using pip:
   ```bash
   pip install .
   ```

## Configuration

Create a configuration file at `~/.stock_price.yaml` to define your holdings:

```yaml
holdings:
  SVOL-B.ST: 8367
  INVE-B.ST: 1387
  MC.PA: 45
  IUSA.DE: 720
```

## Usage

Simply run:
```bash
stock-price
```

## Dependencies

- [yfinance](https://github.com/ranaroussi/yfinance)
- [rich](https://github.com/Textualize/rich)
- [PyYAML](https://pyyaml.org/)
