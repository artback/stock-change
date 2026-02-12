# Stock Price CLI

A professional terminal-based portfolio tracker that provides real-time stock prices, daily performance, and an upcoming dividend calendar. It handles multiple currencies (EUR/SEK) automatically.

## Features

- 📊 **Real-time Portfolio Summary**: Tracks price, quantity, and daily % change.
- 💰 **Dividend Calendar**: Displays the next ex-dividend date and estimated payout amount.
- 💱 **Multi-Currency Support**: Automatically converts Swedish (SEK) holdings to Euro (EUR) using live exchange rates.
- ⚙️ **External Configuration**: Uses a simple YAML file in your home directory to manage holdings.

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

The CLI looks for a configuration file at `~/.stock_price.yaml`. Create this file to define your stock holdings:

```yaml
holdings:
  AAPL: 10          # US Stock (USD)
  SVOL-B.ST: 100    # Swedish Stock (Auto-converted to EUR)
  MC.PA: 5          # French Stock (EUR)
  IUSA.DE: 50       # German ETF (EUR)
```

## Usage

Once installed, simply run the command from any terminal:

```bash
stock-price
```

## Homebrew Tap Setup (For Maintainers)

To maintain the Homebrew tap at `artback/stock-change`:

1. Create a GitHub repository named `homebrew-stock-change`.
2. Place the `stock-price.rb` formula in a `Formula/` directory within that repo.
3. Update the `url` and `sha256` in the formula whenever a new version is released.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
