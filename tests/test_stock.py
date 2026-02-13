from stock import validate_currency, get_rate, load_config, build_display_group


def test_load_config_defaults(mocker):
    mocker.patch("pathlib.Path.exists", return_value=False)
    config = load_config()
    assert "holdings" in config
    assert config["currency"] == "EUR"


def test_validate_currency_usd():
    assert validate_currency("USD") is True


def test_validate_currency_invalid():
    assert validate_currency("INVALID") is False
    assert validate_currency("US") is False


def test_get_rate_same_currency():
    cache = {}
    rate = get_rate("EUR", "EUR", cache)
    assert rate == 1.0
    assert cache == {}


def test_build_display_group():
    summary_results = [
        {
            "symbol": "AAPL",
            "qty": 10,
            "val_now": 1500.0,
            "val_prev": 1450.0,
            "chg_pct": 3.45,
            "source_currency": "USD",
        }
    ]
    history_points = [1400.0, 1420.0, 1450.0, 1500.0]
    group = build_display_group(summary_results, [], "USD", "Status", history_points)
    # group is a rich.console.Group
    assert len(group.renderables) > 0

    # Check if history section is in the output (indirectly by checking rendering)
    from rich.console import Console

    console = Console()
    with console.capture() as capture:
        console.print(group)
    output = capture.get()
    assert "30D DEVELOPMENT" in output


def test_validate_currency_mocked(mocker):
    mock_ticker = mocker.Mock()
    mock_ticker.fast_info = {"lastPrice": 1.1}
    mocker.patch("yfinance.Ticker", return_value=mock_ticker)

    assert validate_currency("EUR") is True
    import yfinance as yf

    yf.Ticker.assert_called_with("USDEUR=X")
