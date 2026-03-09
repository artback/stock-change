import math
from datetime import date, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
from rich.console import Console

from stock import (
    CURRENCY_SYMBOLS,
    DEFAULT_HOLDINGS,
    KNOWN_CURRENCIES,
    build_display_group,
    fetch_history,
    get_dividend_data,
    get_rate,
    get_ticker_summary,
    load_config,
    render_sparkline,
    validate_currency,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _render(group):
    c = Console(width=120)
    with c.capture() as cap:
        c.print(group)
    return cap.get()


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_defaults_when_no_file(self, mocker):
        mocker.patch("pathlib.Path.exists", return_value=False)
        config = load_config()
        assert config["holdings"] == DEFAULT_HOLDINGS
        assert config["currency"] == "EUR"

    def test_loads_yaml_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("holdings:\n  AAPL: 10\ncurrency: sek\n")
        config = load_config(str(cfg))
        assert config["holdings"] == {"AAPL": 10}
        assert config["currency"] == "SEK"

    def test_partial_yaml_only_currency(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("currency: usd\n")
        config = load_config(str(cfg))
        assert config["holdings"] == DEFAULT_HOLDINGS
        assert config["currency"] == "USD"

    def test_partial_yaml_only_holdings(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("holdings:\n  MSFT: 5\n")
        config = load_config(str(cfg))
        assert config["holdings"] == {"MSFT": 5}
        assert config["currency"] == "EUR"

    def test_empty_yaml_returns_defaults(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        config = load_config(str(cfg))
        assert config["holdings"] == DEFAULT_HOLDINGS

    def test_missing_explicit_path_warns(self, capsys):
        config = load_config("/nonexistent/path.yaml")
        assert config["holdings"] == DEFAULT_HOLDINGS
        # Warning printed to rich console (stderr or stdout)

    def test_env_var_config_path(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "env_config.yaml"
        cfg.write_text("currency: gbp\n")
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(cfg))
        config = load_config()
        assert config["currency"] == "GBP"

    def test_invalid_yaml_prints_error(self, tmp_path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("{{{{bad yaml")
        config = load_config(str(cfg))
        assert config["holdings"] == DEFAULT_HOLDINGS


# ---------------------------------------------------------------------------
# validate_currency
# ---------------------------------------------------------------------------

class TestValidateCurrency:
    def test_usd(self):
        assert validate_currency("USD") is True

    def test_known_currencies(self):
        for cur in ["EUR", "SEK", "GBP", "JPY", "CHF"]:
            assert validate_currency(cur) is True

    def test_case_insensitive(self):
        assert validate_currency("eur") is True
        assert validate_currency("Sek") is True

    def test_too_short(self):
        assert validate_currency("US") is False

    def test_too_long(self):
        assert validate_currency("INVALID") is False

    def test_unknown_valid_via_api(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.fast_info = {"lastPrice": 1.5}
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        assert validate_currency("XYZ") is True

    def test_unknown_invalid_via_api(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.fast_info = {}
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        assert validate_currency("QQQ") is False

    def test_api_exception_returns_false(self, mocker):
        mocker.patch("yfinance.Ticker", side_effect=Exception("network"))
        assert validate_currency("QQQ") is False


# ---------------------------------------------------------------------------
# get_rate
# ---------------------------------------------------------------------------

class TestGetRate:
    def test_same_currency(self):
        cache = {}
        assert get_rate("EUR", "EUR", cache) == 1.0
        assert cache == {}

    def test_cache_hit(self):
        cache = {"EURUSD=X": 1.1}
        assert get_rate("EUR", "USD", cache) == 1.1

    def test_direct_pair(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.fast_info = {"lastPrice": 1.08}
        mocker.patch("yfinance.Ticker", return_value=mock_ticker)
        cache = {}
        rate = get_rate("EUR", "USD", cache)
        assert rate == 1.08
        assert cache["EURUSD=X"] == 1.08

    def test_inverse_pair_fallback(self, mocker):
        direct = MagicMock()
        direct.fast_info.__getitem__ = MagicMock(side_effect=KeyError)
        inverse = MagicMock()
        inverse.fast_info = {"lastPrice": 0.5}

        mocker.patch("yfinance.Ticker", side_effect=[direct, inverse])
        cache = {}
        rate = get_rate("AAA", "BBB", cache)
        assert rate == pytest.approx(2.0)
        assert "AAABBB=X" in cache

    def test_both_fail_returns_none(self, mocker):
        mocker.patch("yfinance.Ticker", side_effect=Exception("fail"))
        assert get_rate("AAA", "BBB", {}) is None


# ---------------------------------------------------------------------------
# get_ticker_summary
# ---------------------------------------------------------------------------

class TestGetTickerSummary:
    def _mock_ticker(self, mocker, price=150.0, prev_close=145.0, currency="USD"):
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": price,
            "regularMarketPreviousClose": prev_close,
            "currency": currency,
        }
        mocker.patch("yfinance.Ticker", return_value=mock)
        return mock

    def test_basic(self, mocker):
        self._mock_ticker(mocker, price=150.0, prev_close=145.0)
        result = get_ticker_summary("AAPL", 10, "USD", {})
        assert result is not None
        assert result["symbol"] == "AAPL"
        assert result["qty"] == 10
        assert result["val_now"] == pytest.approx(1500.0)
        assert result["val_prev"] == pytest.approx(1450.0)
        assert result["chg_pct"] == pytest.approx((150 - 145) / 145 * 100)
        assert result["daily_chg_val"] == pytest.approx(50.0)

    def test_with_currency_conversion(self, mocker):
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": 100.0,
            "regularMarketPreviousClose": 100.0,
            "currency": "SEK",
        }
        mocker.patch("yfinance.Ticker", return_value=mock)
        cache = {"SEKEUR=X": 0.1}
        result = get_ticker_summary("TEST.ST", 50, "EUR", cache)
        assert result is not None
        assert result["val_now"] == pytest.approx(500.0)  # 100 * 0.1 * 50

    def test_no_prev_close(self, mocker):
        fi = {
            "lastPrice": 100.0,
            "regularMarketPreviousClose": None,
            "previousClose": None,
            "currency": "USD",
        }
        mock = MagicMock()
        mock.fast_info = fi
        mocker.patch("yfinance.Ticker", return_value=mock)
        result = get_ticker_summary("AAPL", 10, "USD", {})
        assert result is not None
        assert result["chg_pct"] == 0

    def test_nan_price_returns_none(self, mocker):
        fi = {
            "lastPrice": float("nan"),
            "regularMarketPreviousClose": None,
            "currency": "USD",
        }
        mock = MagicMock()
        mock.fast_info = fi
        mocker.patch("yfinance.Ticker", return_value=mock)
        assert get_ticker_summary("BAD", 1, "USD", {}) is None

    def test_exception_returns_none(self, mocker):
        mocker.patch("yfinance.Ticker", side_effect=Exception("network"))
        assert get_ticker_summary("FAIL", 1, "USD", {}) is None


# ---------------------------------------------------------------------------
# get_dividend_data
# ---------------------------------------------------------------------------

class TestGetDividendData:
    def test_upcoming_dividend(self, mocker):
        future_date = date(2099, 12, 31)
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Ex-Dividend Date": future_date}
        mock_ticker.info = {"lastDividendValue": 2.5}

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        result = get_dividend_data(summary)
        assert result is not None
        assert result["symbol"] == "AAPL"
        assert result["ex_date"] == future_date
        assert result["amt"] == 2.5
        assert result["total_p"] == pytest.approx(25.0)

    def test_past_dividend_ignored(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Ex-Dividend Date": date(2020, 1, 1)}
        mock_ticker.info = {"lastDividendValue": 2.5}

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        assert get_dividend_data(summary) is None

    def test_no_calendar(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = None

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        assert get_dividend_data(summary) is None

    def test_no_ex_dividend_key(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": date(2099, 1, 1)}

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        assert get_dividend_data(summary) is None

    def test_zero_dividend_amount(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Ex-Dividend Date": date(2099, 12, 31)}
        mock_ticker.info = {"lastDividendValue": 0, "dividendRate": 0}

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        assert get_dividend_data(summary) is None

    def test_exception_returns_none(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = property(lambda self: (_ for _ in ()).throw(Exception()))
        type(mock_ticker).calendar = property(lambda self: (_ for _ in ()).throw(Exception("fail")))

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "USD",
        }
        assert get_dividend_data(summary) is None

    def test_currency_symbol_in_result(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Ex-Dividend Date": date(2099, 12, 31)}
        mock_ticker.info = {"lastDividendValue": 5.0}

        summary = {
            "symbol": "MC.PA",
            "ticker_obj": mock_ticker,
            "conv": 1.0,
            "qty": 10,
            "source_currency": "EUR",
        }
        result = get_dividend_data(summary)
        assert result["cur_label"] == "€"

    def test_uses_dividend_rate_fallback(self):
        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Ex-Dividend Date": date(2099, 12, 31)}
        mock_ticker.info = {"lastDividendValue": None, "dividendRate": 3.0}

        summary = {
            "symbol": "AAPL",
            "ticker_obj": mock_ticker,
            "conv": 2.0,
            "qty": 5,
            "source_currency": "USD",
        }
        result = get_dividend_data(summary)
        assert result is not None
        assert result["amt"] == 3.0
        assert result["total_p"] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# render_sparkline
# ---------------------------------------------------------------------------

class TestRenderSparkline:
    def test_empty(self):
        assert render_sparkline([]) == ""
        assert render_sparkline(None) == ""

    def test_single_value(self):
        assert render_sparkline([42.0]) == ""

    def test_flat_values(self):
        result = render_sparkline([10.0, 10.0, 10.0, 10.0])
        assert result == "────"

    def test_ascending(self):
        result = render_sparkline([1.0, 2.0, 3.0, 4.0])
        assert len(result) == 4
        assert result[0] != result[-1]

    def test_with_nan(self):
        result = render_sparkline([1.0, float("nan"), 3.0, 4.0])
        assert len(result) == 4
        assert " " in result  # NaN → space

    def test_all_nan(self):
        assert render_sparkline([float("nan"), float("nan")]) == ""

    def test_two_values(self):
        result = render_sparkline([1.0, 10.0])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# fetch_history
# ---------------------------------------------------------------------------

class TestFetchHistory:
    def test_basic(self, mocker):
        dates = pd.date_range("2024-01-01", periods=3)
        close_data = pd.DataFrame(
            {"AAPL": [100.0, 105.0, 110.0]},
            index=dates,
        )
        df = pd.DataFrame({"Close": close_data["AAPL"]})
        df.columns = pd.MultiIndex.from_tuples([("Close", "AAPL")])

        # Build a proper MultiIndex DataFrame
        data = {("Close", "AAPL"): [100.0, 105.0, 110.0]}
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)

        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"AAPL": 10}
        ticker_to_currency = {"AAPL": "USD"}
        totals, changes = fetch_history(holdings, "USD", ticker_to_currency)

        assert len(totals) == 3
        assert totals[0] == pytest.approx(1000.0)
        assert totals[-1] == pytest.approx(1100.0)
        assert "AAPL" in changes
        assert changes["AAPL"] == pytest.approx(10.0)

    def test_empty_download(self, mocker):
        mocker.patch("yfinance.download", return_value=pd.DataFrame())
        totals, changes = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
        assert totals == []
        assert changes == {}

    def test_exception_returns_empty(self, mocker):
        mocker.patch("yfinance.download", side_effect=Exception("network"))
        totals, changes = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
        assert totals == []
        assert changes == {}

    def test_with_currency_conversion(self, mocker):
        dates = pd.date_range("2024-01-01", periods=3)
        data = {
            ("Close", "TEST.ST"): [100.0, 110.0, 120.0],
            ("Close", "SEKEUR=X"): [0.09, 0.09, 0.10],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"TEST.ST": 10}
        ticker_to_currency = {"TEST.ST": "SEK"}
        totals, changes = fetch_history(holdings, "EUR", ticker_to_currency)
        assert len(totals) == 3
        assert totals[0] == pytest.approx(100.0 * 10 * 0.09)
        assert totals[-1] == pytest.approx(120.0 * 10 * 0.10)

    def test_single_ticker_series(self, mocker):
        dates = pd.date_range("2024-01-01", periods=2)
        # When only one ticker, yf.download may return a Series for Close
        df = pd.DataFrame({"Close": [50.0, 55.0]}, index=dates)
        mocker.patch("yfinance.download", return_value=df)

        holdings = {"AAPL": 5}
        ticker_to_currency = {"AAPL": "USD"}
        totals, changes = fetch_history(holdings, "USD", ticker_to_currency)
        assert len(totals) == 2


# ---------------------------------------------------------------------------
# build_display_group
# ---------------------------------------------------------------------------

class TestBuildDisplayGroup:
    def _summary(self, symbol="AAPL", qty=10, val_now=1500.0, val_prev=1450.0,
                 chg_pct=3.45, daily_chg=50.0, currency="USD"):
        return {
            "symbol": symbol,
            "qty": qty,
            "val_now": val_now,
            "val_prev": val_prev,
            "chg_pct": chg_pct,
            "daily_chg_val": daily_chg,
            "source_currency": currency,
        }

    def test_empty(self):
        group = build_display_group([], [], "USD")
        output = _render(group)
        assert "Portfolio Summary" in output

    def test_with_summary(self):
        group = build_display_group([self._summary()], [], "USD")
        output = _render(group)
        assert "AAPL" in output
        assert "1,500.00" in output
        assert "TOTAL" in output

    def test_totals_row(self):
        results = [
            self._summary("AAPL", val_now=1000.0, val_prev=900.0, daily_chg=100.0),
            self._summary("MSFT", val_now=2000.0, val_prev=1950.0, daily_chg=50.0),
        ]
        group = build_display_group(results, [], "USD")
        output = _render(group)
        assert "TOTAL" in output
        assert "3,000.00" in output

    def test_with_dividends(self):
        divs = [{
            "symbol": "AAPL",
            "ex_date": date(2099, 6, 15),
            "amt": 1.5,
            "total_p": 15.0,
            "cur_label": "$",
        }]
        group = build_display_group([self._summary()], divs, "USD")
        output = _render(group)
        assert "Upcoming Dividends" in output
        assert "2099-06-15" in output

    def test_with_sparkline(self):
        history = [1400.0, 1420.0, 1450.0, 1500.0]
        group = build_display_group([self._summary()], [], "USD", history_points=history)
        output = _render(group)
        assert "30D TREND" in output

    def test_no_sparkline_without_history(self):
        group = build_display_group([self._summary()], [], "USD")
        output = _render(group)
        assert "30D TREND" not in output

    def test_with_monthly_changes(self):
        monthly = {"AAPL": 5.25}
        group = build_display_group(
            [self._summary()], [], "USD", monthly_changes=monthly
        )
        output = _render(group)
        assert "+5.25%" in output

    def test_footer_text(self):
        group = build_display_group([], [], "USD", footer_text="Last update: 12:00")
        output = _render(group)
        assert "Last update: 12:00" in output

    def test_negative_change_styling(self):
        s = self._summary(val_now=900.0, val_prev=1000.0, chg_pct=-10.0, daily_chg=-100.0)
        group = build_display_group([s], [], "USD")
        output = _render(group)
        assert "-100.00" in output
        assert "-10.00%" in output

    def test_currency_symbol_mapping(self):
        group = build_display_group([self._summary(currency="SEK")], [], "SEK")
        output = _render(group)
        assert "kr" in output

    def test_unknown_currency_uses_code(self):
        group = build_display_group([self._summary(currency="XYZ")], [], "XYZ")
        output = _render(group)
        assert "XYZ" in output

    def test_nan_values_handled(self):
        s = self._summary(val_now=float("nan"), val_prev=float("nan"),
                          chg_pct=float("nan"), daily_chg=float("nan"))
        group = build_display_group([s], [], "USD")
        output = _render(group)
        assert "AAPL" in output  # should not crash


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    def test_known_currencies_contains_common(self):
        for cur in ["EUR", "USD", "GBP", "SEK", "JPY", "CHF"]:
            assert cur in KNOWN_CURRENCIES

    def test_currency_symbols_has_entries(self):
        assert len(CURRENCY_SYMBOLS) > 0
        assert CURRENCY_SYMBOLS["EUR"] == "€"
        assert CURRENCY_SYMBOLS["USD"] == "$"

    def test_default_holdings_not_empty(self):
        assert len(DEFAULT_HOLDINGS) > 0
