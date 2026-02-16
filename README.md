# Stock Price CLI

A professional terminal-based portfolio tracker that provides real-time stock prices, daily performance, and an upcoming dividend calendar. It handles multiple currencies automatically and features a smooth, live-updating interface.

![Demo](assets/demo.gif)

## Features

- 📊 **Real-time Portfolio Summary**: Tracks price, quantity, daily change, and monthly % change with a clean, formatted table.
- 💰 **Dividend Calendar**: Displays upcoming ex-dividend dates and estimated payout amounts.
- 💱 **Multi-Currency Support**: Automatically converts holdings to your target currency (USD, EUR, SEK, etc.) using live exchange rates.
- ⌚ **Watch Mode**: Update your portfolio in real-time with the `--watch` flag.
- ⚙️ **External Configuration**: Managed via a simple YAML file in your home directory.
- 🚀 **Automatic Updates**: Centralized versioning and automated Homebrew releases.

## Installation

### 1. Using Homebrew (Recommended)

You can install the CLI using the `artback/stock-change` tap:

```bash
# Tap the repository
brew tap artback/stock-change

# Install the tool
brew install stock-price
```

### 2. Manual Installation (Development)

If you want to run it locally from the source:

```bash
git clone git@github.com:artback/stock-change.git
cd stock-change
python3 -m venv venv
source venv/bin/activate
pip install .
```

## Configuration

The CLI looks for a configuration file at `~/.stock_price.yaml`. Create this file to define your stock holdings and preferred currency:

```yaml
holdings:
  SVOL-B.ST: 8367   # Swedish Stock
  AAPL: 10          # US Stock
  MC.PA: 45         # French Stock
currency: EUR       # Target currency for total value and conversion
```

## Usage

Once installed, simply run the command:

```bash
# Standard view (uses ~/.stock_price.yaml)
stock-price

# Use a custom configuration file
stock-price --config ./my_stocks.yaml

# Use an environment variable for configuration
export STOCK_PRICE_CONFIG="./my_stocks.yaml"
stock-price

# Live watch mode (updates every 5 seconds)
stock-price --watch
```

## Maintenance

This project uses an automated release workflow:
1. Update the version in the `VERSION` file.
2. Push to `main`.
3. GitHub Actions will automatically:
    - Update the Homebrew formula in `artback/homebrew-stock-change`.
    - Regenerate the `demo.gif` using VHS.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
