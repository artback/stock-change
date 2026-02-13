import pytest
from stock import validate_currency, get_rate, load_config, generate_table

def test_load_config_defaults(mocker):
    # Mock Path.exists globally to return False
    mocker.patch("pathlib.Path.exists", return_value=False)
    config = load_config()
    assert "holdings" in config
    assert config["currency"] == "EUR"

def test_validate_currency_usd():
    # USD is hardcoded as True
    assert validate_currency("USD") is True

def test_validate_currency_invalid():
    assert validate_currency("INVALID") is False
    assert validate_currency("US") is False

def test_get_rate_same_currency():
    cache = {}
    rate = get_rate("EUR", "EUR", cache)
    assert rate == 1.0
    assert cache == {}

def test_generate_table():
    summary_results = [
        {
            "symbol": "AAPL",
            "qty": 10,
            "val_now": 1500.0,
            "val_prev": 1450.0,
            "chg_pct": 3.45
        }
    ]
    table, total_val, total_prev = generate_table(summary_results, "USD")
    assert total_val == 1500.0
    assert total_prev == 1450.0
    assert table.title == "Portfolio Summary (USD)"
    assert len(table.rows) == 1

def test_validate_currency_mocked(mocker):
    # Mock yfinance Ticker to test validation logic
    mock_ticker = mocker.Mock()
    mock_ticker.fast_info = {"lastPrice": 1.1}
    mocker.patch("yfinance.Ticker", return_value=mock_ticker)
    
    assert validate_currency("EUR") is True
    import yfinance as yf
    yf.Ticker.assert_called_with("USDEUR=X")
