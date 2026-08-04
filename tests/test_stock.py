import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from rich.console import Console
from rich.table import Table

import stock as stock_module
from stock import (
    CURRENCY_SYMBOLS,
    DEFAULT_CONFIG_PATH,
    DEFAULT_HOLDINGS,
    DEFAULT_SCHEDULE,
    EXCHANGE_SCHEDULES,
    JSON_SCHEMA_VERSION,
    KNOWN_CURRENCIES,
    MIN_RECORDED_HISTORY_POINTS,
    PRESERVES_COMMENTS,
    _cached_previous_close,
    _consensus_trend,
    _fit_columns,
    _get_exchange_suffix,
    _has_market_activity,
    _json_safe,
    _previous_close_from_history,
    _price_service_reachable,
    _retry,
    _score_ratings,
    _table_width,
    add_shares,
    analyst_view,
    apply_holiday_zeroing,
    blend_cost,
    build_display_group,
    collect_portfolio,
    consensus_label,
    fetch_all_analysts,
    fetch_all_dividends,
    fetch_auxiliary,
    fetch_benchmark,
    fetch_history,
    fetch_summaries,
    get_analyst_data,
    get_dividend_data,
    get_news_data,
    get_previous_rate,
    get_rate,
    get_ticker_summary,
    holding_cost,
    holding_quantity,
    is_any_market_open,
    load_cached_portfolio,
    load_config,
    load_portfolio_history,
    parse_holdings,
    portfolio_payload,
    read_config_document,
    record_portfolio_value,
    remove_holding,
    render_sparkline,
    resolve_config_path,
    resolve_symbol,
    save_cached_portfolio,
    set_holding,
    validate_currency,
    write_config_document,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fixed_console_width(monkeypatch):
    """Pin the console size so shedding is deterministic.

    Under pytest the module console is 80x25, but the helpers below render at
    120 — without this the two disagree, columns vanish unexpectedly, and the
    short default height silently drops panels.
    """
    monkeypatch.setattr(stock_module, "console", Console(width=120, height=100))


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

    def test_api_exception_is_tolerated_offline(self, mocker):
        # Can't verify an unknown code without network — be lenient and allow
        # a plausibly valid (alphabetic, 3-char) code rather than refuse to run.
        mocker.patch("yfinance.Ticker", side_effect=Exception("network"))
        assert validate_currency("XYZ") is True

    def test_non_alpha_code_is_invalid(self):
        assert validate_currency("12X") is False
        assert validate_currency("E1R") is False


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

    def test_nan_prev_close_falls_back_to_history(self, mocker):
        """fast_info often returns NaN for regularMarketPreviousClose; the
        day change must come from daily history instead of collapsing to 0."""
        dates = pd.DatetimeIndex(
            [pd.Timestamp.now().normalize() - pd.Timedelta(days=1)]
        )
        hist = pd.DataFrame({"Close": [54.3]}, index=dates)
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": 55.5,
            "regularMarketPreviousClose": float("nan"),
            "previousClose": float("nan"),
            "currency": "SEK",
        }
        mock.history.return_value = hist
        mocker.patch("yfinance.Ticker", return_value=mock)
        result = get_ticker_summary("SVOL-B.ST", 100, "SEK", {})
        assert result is not None
        assert result["chg_pct"] == pytest.approx((55.5 - 54.3) / 54.3 * 100)

    def test_history_preferred_over_bogus_previous_close(self, mocker):
        """When regularMarketPreviousClose is missing, fast_info.previousClose
        sometimes mirrors lastPrice (which would zero the day change). Daily
        history must win so the real prior-session close is used."""
        dates = pd.DatetimeIndex(
            [pd.Timestamp.now().normalize() - pd.Timedelta(days=1)]
        )
        hist = pd.DataFrame({"Close": [54.3]}, index=dates)
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": 55.25,
            "regularMarketPreviousClose": float("nan"),
            "previousClose": 55.25,  # bogus — equals lastPrice
            "currency": "SEK",
        }
        mock.history.return_value = hist
        mocker.patch("yfinance.Ticker", return_value=mock)
        result = get_ticker_summary("SVOL-B.ST", 100, "SEK", {})
        assert result is not None
        assert result["chg_pct"] == pytest.approx((55.25 - 54.3) / 54.3 * 100)

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
        mocker.patch("stock.time.sleep")  # don't actually back off in tests
        mocker.patch("yfinance.Ticker", side_effect=Exception("network"))
        assert get_ticker_summary("FAIL", 1, "USD", {}) is None

    def test_retries_then_succeeds(self, mocker):
        mocker.patch("stock.time.sleep")
        good = MagicMock()
        good.fast_info = {"lastPrice": 150.0, "regularMarketPreviousClose": 145.0}
        # First two calls fail, third returns a working ticker.
        mocker.patch(
            "yfinance.Ticker",
            side_effect=[Exception("429"), Exception("429"), good],
        )
        result = get_ticker_summary("AAPL", 10, "USD", {})
        assert result is not None
        assert result["chg_pct"] == pytest.approx((150 - 145) / 145 * 100)


# ---------------------------------------------------------------------------
# _retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_returns_first_success(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        assert _retry(fn) == "ok"
        assert calls["n"] == 1

    def test_retries_then_raises(self, mocker):
        mocker.patch("stock.time.sleep")
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError("boom")

        with pytest.raises(ValueError):
            _retry(fn, attempts=3)
        assert calls["n"] == 3


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
        totals, changes, _traded = fetch_history(holdings, "USD", ticker_to_currency)

        assert len(totals) == 3
        assert totals[0] == pytest.approx(1000.0)
        assert totals[-1] == pytest.approx(1100.0)
        assert "AAPL" in changes
        assert changes["AAPL"] == pytest.approx(10.0)

    def test_empty_download(self, mocker):
        mocker.patch("yfinance.download", return_value=pd.DataFrame())
        totals, changes, _traded = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
        assert totals == []
        assert changes == {}

    def test_exception_returns_empty(self, mocker):
        mocker.patch("yfinance.download", side_effect=Exception("network"))
        totals, changes, _traded = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
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
        totals, _changes, _traded = fetch_history(holdings, "EUR", ticker_to_currency)
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
        totals, _changes, _traded = fetch_history(holdings, "USD", ticker_to_currency)
        assert len(totals) == 2

    def test_holiday_row_with_only_forex_excluded(self, mocker):
        """On bank holidays, forex trades but stocks don't. The holiday row
        (stock=NaN, forex=fresh rate) should be excluded so stale prices
        aren't paired with fresh exchange rates."""
        dates = pd.date_range("2024-01-01", periods=4)
        data = {
            # Stock has data on days 1-3 but NaN on day 4 (holiday)
            ("Close", "TEST.ST"): [100.0, 105.0, 110.0, float("nan")],
            # Forex trades every day, rate drops on the holiday
            ("Close", "SEKEUR=X"): [0.09, 0.09, 0.09, 0.08],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"TEST.ST": 10}
        ticker_to_currency = {"TEST.ST": "SEK"}
        totals, _changes, _traded = fetch_history(holdings, "EUR", ticker_to_currency)
        # Holiday row should be excluded — only 3 trading days
        assert len(totals) == 3
        # Last total uses day 3 stock price (110) with day 3 rate (0.09)
        assert totals[-1] == pytest.approx(110.0 * 10 * 0.09)

    def test_traded_today_empty_on_holiday(self, mocker):
        """traded_today should be empty when today's date has no stock data."""
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        dates = pd.DatetimeIndex([yesterday, today])
        data = {
            ("Close", "AAPL"): [150.0, float("nan")],
            ("Close", "USDEUR=X"): [0.87, 0.88],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"AAPL": 10}
        ticker_to_currency = {"AAPL": "USD"}
        _totals, _changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
        assert traded == set()

    def test_traded_today_contains_tickers_with_data(self, mocker):
        """traded_today should contain only tickers that had data today."""
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        dates = pd.DatetimeIndex([yesterday, today])
        data = {
            ("Close", "AAPL"): [150.0, 152.0],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"AAPL": 10}
        ticker_to_currency = {"AAPL": "USD"}
        _totals, _changes, traded = fetch_history(holdings, "USD", ticker_to_currency)
        assert traded == {"AAPL"}

    def test_partial_holiday_mixed_exchanges(self, mocker):
        """On partial holidays (e.g. Good Friday), only tickers on open
        exchanges should appear in traded_today."""
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        dates = pd.DatetimeIndex([yesterday, today])
        data = {
            # Tokyo open on Good Friday
            ("Close", "7203.T"): [2500.0, 2520.0],
            # Stockholm closed
            ("Close", "SVOL-B.ST"): [50.0, float("nan")],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"7203.T": 100, "SVOL-B.ST": 500}
        ticker_to_currency = {"7203.T": "JPY", "SVOL-B.ST": "SEK"}
        _totals, _changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
        assert "7203.T" in traded
        assert "SVOL-B.ST" not in traded

    def test_traded_today_per_exchange_not_per_ticker(self, mocker):
        """A ticker whose today bar lags in yfinance's daily download should
        still count as traded if a sibling on the same exchange has data.
        Otherwise its live fast_info day change gets wrongly zeroed."""
        today = pd.Timestamp.now().normalize()
        yesterday = today - pd.Timedelta(days=1)
        dates = pd.DatetimeIndex([yesterday, today])
        data = {
            # Same exchange (.ST): one has today's bar, one lags (NaN today)
            ("Close", "LIFCO-B.ST"): [100.0, 102.0],
            ("Close", "INVE-B.ST"): [200.0, float("nan")],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"LIFCO-B.ST": 5, "INVE-B.ST": 10}
        ticker_to_currency = {"LIFCO-B.ST": "SEK", "INVE-B.ST": "SEK"}
        _totals, _changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
        assert traded == {"LIFCO-B.ST", "INVE-B.ST"}

    def test_bfill_fixes_missing_rate_on_first_day(self, mocker):
        """Exchange rate NaN on the first row should be backfilled, not
        fall back to 1.0 which would massively inflate the portfolio."""
        dates = pd.date_range("2024-01-01", periods=3)
        data = {
            ("Close", "TEST.ST"): [100.0, 105.0, 110.0],
            ("Close", "SEKEUR=X"): [float("nan"), 0.09, 0.09],
        }
        mi_df = pd.DataFrame(data, index=dates)
        mi_df.columns = pd.MultiIndex.from_tuples(mi_df.columns)
        mocker.patch("yfinance.download", return_value=mi_df)

        holdings = {"TEST.ST": 10}
        ticker_to_currency = {"TEST.ST": "SEK"}
        totals, _changes, _traded = fetch_history(holdings, "EUR", ticker_to_currency)
        assert len(totals) == 3
        # First day should use backfilled rate 0.09, not 1.0
        assert totals[0] == pytest.approx(100.0 * 10 * 0.09)


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
        assert "Dividends" in output
        assert "2099-06-15" in output

    def test_with_sparkline(self):
        history = [1400.0, 1420.0, 1450.0, 1500.0]
        group = build_display_group([self._summary()], [], "USD", history_points=history)
        output = _render(group)
        assert "30D BASKET" in output

    def test_no_sparkline_without_history(self):
        group = build_display_group([self._summary()], [], "USD")
        output = _render(group)
        assert "30D BASKET" not in output

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
# News
# ---------------------------------------------------------------------------

class TestGetNewsData:
    @staticmethod
    def _recent_iso(days_ago=1):
        """Return an ISO 8601 UTC timestamp for *days_ago* days before now."""
        dt = datetime.now(ZoneInfo("UTC")) - timedelta(days=days_ago)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _make_article(self, title, pub_date, provider="Test", summary="Some summary"):
        return {
            "content": {
                "title": title,
                "pubDate": pub_date,
                "summary": summary,
                "provider": {"displayName": provider},
                "clickThroughUrl": {"url": f"https://example.com/{title.replace(' ', '-')}"},
                "canonicalUrl": {"url": f"https://example.com/canonical/{title.replace(' ', '-')}"},
            }
        }

    def test_basic_fetch(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [
            self._make_article("Breaking News", self._recent_iso(1)),
        ]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL"])
        assert len(result) == 1
        assert result[0]["title"] == "Breaking News"
        assert result[0]["symbol"] == "AAPL"
        assert result[0]["link"] == "https://example.com/Breaking-News"
        assert result[0]["summary"] == "Some summary"

    def test_filters_old_articles(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [
            self._make_article("Recent", self._recent_iso(1)),
            self._make_article("Old", "2020-01-01T10:00:00Z"),
        ]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL"], max_age_days=14)
        titles = [r["title"] for r in result]
        assert "Recent" in titles
        assert "Old" not in titles

    def test_deduplicates_titles(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [
            self._make_article("Same Title", self._recent_iso(1)),
        ]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL", "MSFT"])
        assert len(result) == 1

    def test_empty_news(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = []
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL"])
        assert result == []

    def test_limits_to_15(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [
            self._make_article(f"Article {i}", self._recent_iso(1 + i % 5))
            for i in range(20)
        ]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA"])
        assert len(result) <= 15

    def test_fallback_to_canonical_url(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [{
            "content": {
                "title": "Test",
                "pubDate": self._recent_iso(1),
                "summary": "",
                "provider": {"displayName": "Source"},
                "clickThroughUrl": None,
                "canonicalUrl": {"url": "https://canonical.example.com/test"},
            }
        }]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL"])
        assert result[0]["link"] == "https://canonical.example.com/test"

    def test_handles_ticker_exception(self, mocker):
        mocker.patch("stock.yf.Ticker", side_effect=Exception("API error"))
        result = get_news_data(["AAPL"])
        assert result == []

    def test_sorted_newest_first(self, mocker):
        mock_ticker = MagicMock()
        mock_ticker.news = [
            self._make_article("Older", self._recent_iso(7)),
            self._make_article("Newer", self._recent_iso(1)),
            self._make_article("Middle", self._recent_iso(3)),
        ]
        mocker.patch("stock.yf.Ticker", return_value=mock_ticker)
        result = get_news_data(["AAPL"])
        assert result[0]["title"] == "Newer"
        assert result[1]["title"] == "Middle"
        assert result[2]["title"] == "Older"


class TestBuildDisplayGroupNews:
    def _summary(self, symbol="AAPL"):
        return {
            "symbol": symbol, "qty": 10, "val_now": 1500.0,
            "val_prev": 1450.0, "chg_pct": 3.45, "daily_chg_val": 50.0,
            "source_currency": "USD",
        }

    def test_news_panel_rendered(self):
        news = [{
            "symbol": "AAPL", "title": "Big News", "link": "https://example.com",
            "provider": "Reuters", "pub_date": "2026-03-05 10:00", "summary": "A summary.",
        }]
        group = build_display_group([self._summary()], [], "USD", news_items=news)
        output = _render(group)
        assert "Related News" in output
        assert "Big News" in output
        assert "Reuters" in output
        assert "A summary." in output

    def test_no_news_panel_when_empty(self):
        group = build_display_group([self._summary()], [], "USD", news_items=[])
        output = _render(group)
        assert "Related News" not in output

    def test_long_summary_truncated(self):
        news = [{
            "symbol": "AAPL", "title": "Test", "link": "",
            "provider": "Source", "pub_date": "2026-03-05 10:00",
            "summary": "A " * 200,
        }]
        group = build_display_group([self._summary()], [], "USD", news_items=news)
        output = _render(group)
        assert "..." in output


# ---------------------------------------------------------------------------
# Market status detection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Holiday / market activity detection
# ---------------------------------------------------------------------------

class TestHasMarketActivity:
    def test_no_data_assumes_active(self):
        assert _has_market_activity([]) is True
        assert _has_market_activity(None) is True

    def test_all_zero_change_means_closed(self):
        results = [
            {"symbol": "AAPL", "chg_pct": 0},
            {"symbol": "MSFT", "chg_pct": 0},
        ]
        assert _has_market_activity(results) is False

    def test_any_nonzero_change_means_open(self):
        results = [
            {"symbol": "AAPL", "chg_pct": 0},
            {"symbol": "MSFT", "chg_pct": 0.5},
        ]
        assert _has_market_activity(results) is True

    def test_negative_change_means_open(self):
        results = [{"symbol": "AAPL", "chg_pct": -1.2}]
        assert _has_market_activity(results) is True

    def test_none_entries_skipped(self):
        results = [None, {"symbol": "AAPL", "chg_pct": 0}]
        assert _has_market_activity(results) is False


class TestGetExchangeSuffix:
    def test_stockholm(self):
        assert _get_exchange_suffix("SVOL-B.ST") == ".ST"

    def test_paris(self):
        assert _get_exchange_suffix("MC.PA") == ".PA"

    def test_no_suffix(self):
        assert _get_exchange_suffix("AAPL") is None

    def test_german(self):
        assert _get_exchange_suffix("IUSA.DE") == ".DE"


class TestIsAnyMarketOpen:
    def test_weekday_during_us_hours(self):
        # Wednesday 10:00 AM ET
        now = datetime(2026, 3, 11, 15, 0, 0, tzinfo=ZoneInfo("UTC"))  # 10 AM ET
        assert is_any_market_open({"AAPL": 10}, now=now) is True

    def test_weekday_outside_us_hours(self):
        # Wednesday 10:00 PM ET (after close)
        now = datetime(2026, 3, 12, 3, 0, 0, tzinfo=ZoneInfo("UTC"))  # 10 PM ET
        assert is_any_market_open({"AAPL": 10}, now=now) is False

    def test_saturday(self):
        # Saturday 12:00 UTC
        now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_any_market_open({"AAPL": 10}, now=now) is False

    def test_sunday(self):
        # Sunday 12:00 UTC
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_any_market_open({"SVOL-B.ST": 100}, now=now) is False

    def test_stockholm_open(self):
        # Wednesday 10:00 Stockholm time = 09:00 UTC
        now = datetime(2026, 3, 11, 9, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_any_market_open({"SVOL-B.ST": 100}, now=now) is True

    def test_stockholm_closed_evening(self):
        # Wednesday 19:00 Stockholm time = 18:00 UTC
        now = datetime(2026, 3, 11, 18, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert is_any_market_open({"SVOL-B.ST": 100}, now=now) is False

    def test_mixed_exchanges_one_open(self):
        # Wednesday 14:30 UTC — US market open (9:30 ET), Stockholm closed (15:30 CET > 17)
        # Actually 14:30 UTC = 15:30 CET (still open until 17 CET) and 9:30 ET (open)
        now = datetime(2026, 3, 11, 14, 30, 0, tzinfo=ZoneInfo("UTC"))
        holdings = {"AAPL": 10, "SVOL-B.ST": 100}
        assert is_any_market_open(holdings, now=now) is True

    def test_mixed_exchanges_all_closed(self):
        # Wednesday 23:00 UTC — all closed
        now = datetime(2026, 3, 11, 23, 0, 0, tzinfo=ZoneInfo("UTC"))
        holdings = {"AAPL": 10, "SVOL-B.ST": 100}
        assert is_any_market_open(holdings, now=now) is False

    def test_exchange_schedules_has_common_exchanges(self):
        for suffix in [".ST", ".PA", ".DE", ".L", ".T", ".HK"]:
            assert suffix in EXCHANGE_SCHEDULES

    def test_default_schedule_is_nyse(self):
        # NYSE/Nasdaq regular session is 09:30-16:00 Eastern.
        assert DEFAULT_SCHEDULE == ("America/New_York", 9.5, 16)


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


# ---------------------------------------------------------------------------
# apply_holiday_zeroing
# ---------------------------------------------------------------------------


class TestApplyHolidayZeroing:
    def _entry(self):
        return {"val_now": 100.0, "val_prev": 90.0, "chg_pct": 11.1, "daily_chg_val": 10.0}

    def test_none_traded_leaves_values_untouched(self):
        cache = {"AAPL": self._entry()}
        apply_holiday_zeroing(cache, None)
        assert cache["AAPL"]["chg_pct"] == 11.1
        assert cache["AAPL"]["daily_chg_val"] == 10.0

    def test_untraded_ticker_is_zeroed(self):
        cache = {"AAPL": self._entry(), "SVOL-B.ST": self._entry()}
        apply_holiday_zeroing(cache, {"AAPL"})
        assert cache["AAPL"]["chg_pct"] == 11.1
        assert cache["SVOL-B.ST"]["chg_pct"] == 0
        assert cache["SVOL-B.ST"]["daily_chg_val"] == 0
        assert cache["SVOL-B.ST"]["val_prev"] == cache["SVOL-B.ST"]["val_now"]

    def test_idempotent(self):
        cache = {"SVOL-B.ST": self._entry()}
        apply_holiday_zeroing(cache, set())
        apply_holiday_zeroing(cache, set())
        assert cache["SVOL-B.ST"]["chg_pct"] == 0
        assert cache["SVOL-B.ST"]["val_prev"] == cache["SVOL-B.ST"]["val_now"]


# ---------------------------------------------------------------------------
# fetch_all_dividends
# ---------------------------------------------------------------------------


class TestFetchAllDividends:
    def test_empty_returns_empty(self):
        assert fetch_all_dividends([]) == {}

    def test_collects_only_tickers_with_dividends(self, mocker):
        def fake(summary):
            if summary["symbol"] == "INVE-B.ST":
                return {"symbol": "INVE-B.ST", "amt": 1.6}
            return None

        mocker.patch("stock.get_dividend_data", side_effect=fake)
        summary_values = [
            {"symbol": "INVE-B.ST"},
            {"symbol": "MC.PA"},
        ]
        result = fetch_all_dividends(summary_values)
        assert set(result) == {"INVE-B.ST"}
        assert result["INVE-B.ST"]["amt"] == 1.6


# ---------------------------------------------------------------------------
# fetch_summaries
# ---------------------------------------------------------------------------


class TestFetchSummaries:
    def test_collects_and_reports_failures(self, mocker):
        def fake(symbol, qty, currency, rate_cache, prev_close_cache=None, cost=None):
            if symbol == "BAD":
                return None
            return {"symbol": symbol, "source_currency": "USD", "val_now": qty}

        mocker.patch("stock.get_ticker_summary", side_effect=fake)
        summaries, ttc, failed = fetch_summaries(
            {"AAPL": 1, "BAD": 2, "MSFT": 3}, "USD", {}
        )
        assert set(summaries) == {"AAPL", "MSFT"}
        assert ttc == {"AAPL": "USD", "MSFT": "USD"}
        assert failed == ["BAD"]

    def test_empty_holdings(self):
        summaries, ttc, failed = fetch_summaries({}, "USD", {})
        assert summaries == {} and ttc == {} and failed == []

    def test_progress_callback_invoked(self, mocker):
        mocker.patch(
            "stock.get_ticker_summary",
            side_effect=lambda s, q, c, rc, pc=None: {
                "symbol": s,
                "source_currency": "USD",
            },
        )
        seen = []
        fetch_summaries(
            {"AAPL": 1, "MSFT": 1}, "USD", {}, on_progress=lambda c, t, p: seen.append((c, t))
        )
        assert seen[-1] == (2, 2)

    def test_future_exception_counts_as_failed(self, mocker):
        mocker.patch("stock.get_ticker_summary", side_effect=Exception("boom"))
        summaries, _ttc, failed = fetch_summaries({"AAPL": 1}, "USD", {})
        assert summaries == {}
        assert failed == ["AAPL"]


# ---------------------------------------------------------------------------
# fetch_auxiliary
# ---------------------------------------------------------------------------


class TestFetchAuxiliary:
    def test_only_requested_sections_run(self, mocker):
        hist = mocker.patch("stock.fetch_history", return_value=([1.0], {"A": 2.0}, {"A"}))
        divs = mocker.patch("stock.fetch_all_dividends", return_value={"A": {"x": 1}})
        news = mocker.patch("stock.get_news_data", return_value=[{"t": 1}])

        result = fetch_auxiliary(
            {"A": 1}, "USD", {"A": {"symbol": "A"}}, {"A": "USD"},
            want_history=True, want_dividends=False, want_news=False,
        )
        assert result["history"] == [1.0]
        assert result["monthly"] == {"A": 2.0}
        assert result["traded"] == {"A"}
        assert "dividends" not in result and "news" not in result
        hist.assert_called_once()
        divs.assert_not_called()
        news.assert_not_called()

    def test_all_sections(self, mocker):
        mocker.patch("stock.fetch_history", return_value=([1.0], {}, set()))
        mocker.patch("stock.fetch_all_dividends", return_value={"A": 1})
        mocker.patch("stock.get_news_data", return_value=["n"])
        result = fetch_auxiliary(
            {"A": 1}, "USD", {}, {},
            want_history=True, want_dividends=True, want_news=True,
        )
        assert set(result) == {"history", "monthly", "traded", "dividends", "news"}


# ---------------------------------------------------------------------------
# portfolio cache
# ---------------------------------------------------------------------------


class TestPortfolioCache:
    def test_roundtrip_within_ttl(self, tmp_path):
        path = tmp_path / "cache.json"
        holdings = {"AAPL": 10}
        summaries = [
            {
                "symbol": "AAPL",
                "qty": 10,
                "val_now": 1500.0,
                "val_prev": 1450.0,
                "chg_pct": 3.4,
                "daily_chg_val": 50.0,
                "source_currency": "USD",
                "conv": 1.0,
                "ticker_obj": object(),  # non-serialisable, must be dropped
            }
        ]
        save_cached_portfolio(holdings, "USD", summaries, [], [], [1.0, 2.0], {"AAPL": 5.0}, path=path)
        cached = load_cached_portfolio(holdings, "USD", ttl=60, path=path)
        assert cached is not None
        assert cached["summaries"][0]["symbol"] == "AAPL"
        assert "ticker_obj" not in cached["summaries"][0]
        assert cached["history_points"] == [1.0, 2.0]

    def test_disabled_when_ttl_zero(self, tmp_path):
        path = tmp_path / "cache.json"
        save_cached_portfolio({"AAPL": 1}, "USD", [], [], [], [], {}, path=path)
        assert load_cached_portfolio({"AAPL": 1}, "USD", ttl=0, path=path) is None

    def test_miss_on_different_holdings(self, tmp_path):
        path = tmp_path / "cache.json"
        save_cached_portfolio({"AAPL": 1}, "USD", [], [], [], [], {}, path=path)
        assert load_cached_portfolio({"MSFT": 1}, "USD", ttl=60, path=path) is None

    def test_expired_returns_none(self, tmp_path, mocker):
        path = tmp_path / "cache.json"
        save_cached_portfolio({"AAPL": 1}, "USD", [], [], [], [], {}, path=path)
        # Pretend the snapshot is two minutes old.
        real = time.time()
        mocker.patch("stock.time.time", return_value=real + 120)
        assert load_cached_portfolio({"AAPL": 1}, "USD", ttl=60, path=path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert load_cached_portfolio({"AAPL": 1}, "USD", ttl=60, path=tmp_path / "nope.json") is None


# ---------------------------------------------------------------------------
# build_display_group error indicators
# ---------------------------------------------------------------------------


class TestBuildDisplayErrors:
    def _summary(self, symbol="AAPL"):
        return {
            "symbol": symbol,
            "qty": 10,
            "val_now": 1500.0,
            "val_prev": 1450.0,
            "chg_pct": 3.4,
            "daily_chg_val": 50.0,
        }

    def _render(self, group):
        console = Console(width=120, record=True)
        with console.capture() as cap:
            console.print(group)
        return cap.get()

    def test_never_loaded_symbol_shows_error_row(self):
        group = build_display_group(
            [], [], "USD", error_symbols={"FAIL"}, holdings={"FAIL": 7}
        )
        out = self._render(group)
        assert "FAIL" in out
        assert "error" in out

    def test_stale_symbol_is_marked(self):
        group = build_display_group(
            [self._summary("AAPL")], [], "USD",
            error_symbols={"AAPL"}, holdings={"AAPL": 10},
        )
        out = self._render(group)
        assert "AAPL" in out
        assert "⚠" in out

    def test_no_errors_no_marker(self):
        group = build_display_group([self._summary("AAPL")], [], "USD")
        out = self._render(group)
        assert "⚠" not in out


# ---------------------------------------------------------------------------
# analyst consensus scoring
# ---------------------------------------------------------------------------


def _recs_frame(rows):
    return pd.DataFrame(rows)


_FULL_BUY_ROW = {
    "period": "0m",
    "strongBuy": 6,
    "buy": 22,
    "hold": 14,
    "sell": 2,
    "strongSell": 2,
}


class TestConsensusLabel:
    @pytest.mark.parametrize(
        "score,label",
        [
            (1.0, "Strong Buy"),
            (1.49, "Strong Buy"),
            (1.5, "Buy"),
            (2.49, "Buy"),
            (2.5, "Hold"),
            (3.49, "Hold"),
            (3.5, "Sell"),
            (4.49, "Sell"),
            (4.5, "Strong Sell"),
            (5.0, "Strong Sell"),
        ],
    )
    def test_thresholds(self, score, label):
        assert consensus_label(score) == label

    def test_none_and_nan(self):
        assert consensus_label(None) is None
        assert consensus_label(float("nan")) is None


class TestScoreRatings:
    def test_weighted_mean(self):
        # 6*1 + 22*2 + 14*3 + 2*4 + 2*5 = 110 over 46 analysts.
        score, counts, total = _score_ratings(_FULL_BUY_ROW)
        assert total == 46
        assert score == pytest.approx(110 / 46)
        assert counts == {
            "strong_buy": 6,
            "buy": 22,
            "hold": 14,
            "sell": 2,
            "strong_sell": 2,
        }

    def test_all_strong_buy_scores_one(self):
        score, _, total = _score_ratings({"strongBuy": 3})
        assert score == 1.0
        assert total == 3

    def test_no_coverage(self):
        score, counts, total = _score_ratings({})
        assert score is None
        assert total == 0
        assert counts == {
            "strong_buy": 0,
            "buy": 0,
            "hold": 0,
            "sell": 0,
            "strong_sell": 0,
        }

    def test_nan_counts_treated_as_zero(self):
        score, counts, total = _score_ratings({"buy": float("nan"), "hold": 2})
        assert counts["buy"] == 0
        assert total == 2
        assert score == 3.0


class TestConsensusTrend:
    def test_falling_score_is_an_upgrade(self):
        # The scale is inverted: 2.0 is more bullish than 2.5.
        assert _consensus_trend(2.0, 2.5) == "up"

    def test_rising_score_is_a_downgrade(self):
        assert _consensus_trend(2.5, 2.0) == "down"

    def test_small_drift_is_steady(self):
        assert _consensus_trend(2.40, 2.42) == "steady"

    def test_missing_history(self):
        assert _consensus_trend(2.0, None) is None
        assert _consensus_trend(None, 2.0) is None
        assert _consensus_trend(2.0, float("nan")) is None


# ---------------------------------------------------------------------------
# get_analyst_data
# ---------------------------------------------------------------------------


class TestGetAnalystData:
    def _summary(self, symbol="AAPL", currency="USD"):
        return {"symbol": symbol, "ticker_obj": MagicMock(), "source_currency": currency}

    def test_full_coverage(self):
        summary = self._summary()
        summary["ticker_obj"].recommendations = _recs_frame(
            [
                _FULL_BUY_ROW,
                {**_FULL_BUY_ROW, "period": "-1m", "hold": 16, "sell": 1},
            ]
        )
        summary["ticker_obj"].analyst_price_targets = {
            "current": 100.0,
            "low": 80.0,
            "mean": 120.0,
            "median": 118.0,
            "high": 150.0,
        }

        result = get_analyst_data(summary)
        assert result["symbol"] == "AAPL"
        assert result["consensus"] == "Buy"
        assert result["analyst_count"] == 46
        assert result["counts"]["strong_buy"] == 6
        assert result["price_target"]["upside_pct"] == pytest.approx(20.0)
        assert result["price_target"]["currency"] == "USD"

    def test_upgrade_trend_detected(self):
        summary = self._summary()
        summary["ticker_obj"].recommendations = _recs_frame(
            [
                {"period": "0m", "strongBuy": 10, "buy": 0, "hold": 0},
                {"period": "-1m", "strongBuy": 0, "buy": 0, "hold": 10},
            ]
        )
        summary["ticker_obj"].analyst_price_targets = {}
        assert get_analyst_data(summary)["trend"] == "up"

    def test_targets_only_without_ratings(self):
        summary = self._summary()
        summary["ticker_obj"].recommendations = pd.DataFrame()
        summary["ticker_obj"].analyst_price_targets = {"current": 50.0, "mean": 55.0}

        result = get_analyst_data(summary)
        assert result["consensus"] is None
        assert result["analyst_count"] == 0
        assert result["price_target"]["upside_pct"] == pytest.approx(10.0)

    def test_no_coverage_returns_none(self):
        # An index fund: empty ratings, and a target dict holding only the
        # current price.
        summary = self._summary("IUSA.DE", "EUR")
        summary["ticker_obj"].recommendations = pd.DataFrame()
        summary["ticker_obj"].analyst_price_targets = {"current": 56.45}
        assert get_analyst_data(summary) is None

    def test_empty_targets_dict(self):
        summary = self._summary()
        summary["ticker_obj"].recommendations = pd.DataFrame()
        summary["ticker_obj"].analyst_price_targets = {}
        assert get_analyst_data(summary) is None

    def test_ratings_without_targets(self):
        summary = self._summary()
        summary["ticker_obj"].recommendations = _recs_frame([_FULL_BUY_ROW])
        summary["ticker_obj"].analyst_price_targets = {"current": 100.0}

        result = get_analyst_data(summary)
        assert result["consensus"] == "Buy"
        assert result["price_target"] is None

    def test_network_error_returns_none(self):
        summary = self._summary()
        type(summary["ticker_obj"]).recommendations = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("429"))
        )
        assert get_analyst_data(summary) is None


class TestFetchAllAnalysts:
    def test_keyed_by_symbol_skipping_uncovered(self, mocker):
        mocker.patch(
            "stock.get_analyst_data",
            side_effect=lambda s: (
                {"symbol": s["symbol"], "consensus": "Buy"}
                if s["symbol"] == "AAPL"
                else None
            ),
        )
        result = fetch_all_analysts(
            [{"symbol": "AAPL"}, {"symbol": "IUSA.DE"}]
        )
        assert set(result) == {"AAPL"}

    def test_empty_input(self):
        assert fetch_all_analysts([]) == {}


class TestFetchAuxiliaryAnalysts:
    def test_analysts_requested(self, mocker):
        analysts = mocker.patch(
            "stock.fetch_all_analysts", return_value={"A": {"consensus": "Buy"}}
        )
        result = fetch_auxiliary(
            {"A": 1}, "USD", {"A": {"symbol": "A"}}, {"A": "USD"},
            want_analysts=True,
        )
        assert result["analysts"] == {"A": {"consensus": "Buy"}}
        analysts.assert_called_once()

    def test_analysts_skipped_by_default(self, mocker):
        analysts = mocker.patch("stock.fetch_all_analysts", return_value={})
        result = fetch_auxiliary({"A": 1}, "USD", {"A": {"symbol": "A"}}, {"A": "USD"})
        assert "analysts" not in result
        analysts.assert_not_called()


# ---------------------------------------------------------------------------
# analyst columns in the summary table
# ---------------------------------------------------------------------------


class TestBuildDisplayGroupAnalysts:
    def _summary(self, symbol="AAPL"):
        return {
            "symbol": symbol,
            "qty": 10,
            "val_now": 1500.0,
            "val_prev": 1450.0,
            "chg_pct": 3.4,
            "daily_chg_val": 50.0,
            "source_currency": "USD",
        }

    def _analysts(self, **overrides):
        info = {
            "symbol": "AAPL",
            "consensus": "Buy",
            "trend": "steady",
            "analyst_count": 46,
            "price_target": {"upside_pct": 4.65},
        }
        info.update(overrides)
        return {"AAPL": info}

    def test_columns_hidden_without_data(self):
        out = _render(build_display_group([self._summary()], [], "USD"))
        assert "Analysts" not in out
        assert "Target" not in out

    def test_consensus_and_target_rendered(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD", analyst_results=self._analysts()
            )
        )
        assert "Analysts" in out
        assert "Buy 46" in out
        assert "+4.7%" in out

    def test_upgrade_arrow(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                analyst_results=self._analysts(trend="up"),
            )
        )
        assert "↑" in out

    def test_downgrade_arrow(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                analyst_results=self._analysts(trend="down"),
            )
        )
        assert "↓" in out

    def test_uncovered_holding_shows_dash(self):
        # MSFT has no entry, so its own row falls back to "-" while AAPL's fills.
        out = _render(
            build_display_group(
                [self._summary("AAPL"), self._summary("MSFT")], [], "USD",
                analyst_results=self._analysts(),
            )
        )
        table_out = out.split("Allocation")[0]
        rows = {
            line.split()[1]: line for line in table_out.splitlines() if "│" in line and (
                "AAPL" in line or "MSFT" in line
            )
        }
        assert "Buy 46" in rows["AAPL"]
        assert "+4.7%" in rows["AAPL"]
        assert "Buy" not in rows["MSFT"]
        assert rows["MSFT"].rstrip().rstrip("│").rstrip().endswith("-")

    def test_totals_row_survives_extra_columns(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD", analyst_results=self._analysts()
            )
        )
        assert "TOTAL" in out
        assert "1,500.00" in out

    def test_error_row_survives_extra_columns(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                error_symbols={"FAIL"}, holdings={"FAIL": 7},
                analyst_results=self._analysts(),
            )
        )
        assert "FAIL" in out
        assert "error" in out

    def test_missing_price_target(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                analyst_results=self._analysts(price_target=None),
            )
        )
        assert "Buy 46" in out


# ---------------------------------------------------------------------------
# JSON payload
# ---------------------------------------------------------------------------


class TestJsonSafe:
    def test_nan_becomes_null(self):
        assert _json_safe({"a": float("nan")}) == {"a": None}

    def test_nested_containers(self):
        assert _json_safe({"a": [1, (2, float("nan"))]}) == {"a": [1, [2, None]]}

    def test_numpy_scalars_unwrapped(self):
        value = _json_safe(pd.Series([3]).iloc[0])
        assert value == 3
        assert isinstance(value, int)

    def test_dates_stringified(self):
        assert _json_safe(date(2026, 1, 2)) == "2026-01-02"

    def test_nat_becomes_null(self):
        assert _json_safe(pd.NaT) is None

    def test_bool_preserved(self):
        assert _json_safe(True) is True


class TestPortfolioPayload:
    def _summary(self, symbol="AAPL", val_now=1500.0, val_prev=1450.0):
        return {
            "symbol": symbol,
            "qty": 10,
            "val_now": val_now,
            "val_prev": val_prev,
            "chg_pct": 3.45,
            "daily_chg_val": val_now - val_prev,
            "source_currency": "USD",
            "conv": 1.0,
        }

    def test_shape_and_totals(self):
        payload = portfolio_payload(
            "EUR",
            [self._summary("AAPL"), self._summary("MSFT", 2000.0, 1950.0)],
            [], [], [], {},
        )
        assert payload["schema_version"] == JSON_SCHEMA_VERSION
        assert payload["currency"] == "EUR"
        assert payload["cached"] is False
        assert payload["totals"]["value"] == pytest.approx(3500.0)
        assert payload["totals"]["previous_value"] == pytest.approx(3400.0)
        assert payload["totals"]["daily_change"] == pytest.approx(100.0)
        assert payload["totals"]["daily_change_pct"] == pytest.approx(100 / 34)
        assert [p["symbol"] for p in payload["positions"]] == ["AAPL", "MSFT"]

    def test_is_strict_json(self):
        # NaN would serialise to a bare NaN token and break strict parsers.
        payload = portfolio_payload(
            "USD", [self._summary(val_prev=float("nan"))], [], [], [], {},
        )
        assert json.loads(json.dumps(payload, allow_nan=False))
        assert payload["positions"][0]["previous_value"] is None

    def test_position_fields(self):
        payload = portfolio_payload(
            "USD", [self._summary()], [], [], [], {"AAPL": 5.25},
            analysts={"AAPL": {"consensus": "Buy"}},
        )
        position = payload["positions"][0]
        assert position["status"] == "ok"
        assert position["quantity"] == 10
        assert position["month_change_pct"] == pytest.approx(5.25)
        assert position["fx_rate"] == 1.0
        assert position["analysts"] == {"consensus": "Buy"}

    def test_stale_and_errored_positions(self):
        payload = portfolio_payload(
            "USD", [self._summary("AAPL")], [], [], [], {},
            failed=["AAPL", "GONE"], holdings={"AAPL": 10, "GONE": 3},
        )
        by_symbol = {p["symbol"]: p for p in payload["positions"]}
        assert by_symbol["AAPL"]["status"] == "stale"
        assert by_symbol["AAPL"]["value"] == 1500.0
        assert by_symbol["GONE"]["status"] == "error"
        assert by_symbol["GONE"]["value"] is None
        assert by_symbol["GONE"]["quantity"] == 3

    def test_month_change_from_history(self):
        payload = portfolio_payload(
            "USD", [self._summary()], [], [], [100.0, 110.0], {},
        )
        assert payload["totals"]["month_change_pct"] == pytest.approx(10.0)

    def test_month_change_none_without_history(self):
        payload = portfolio_payload("USD", [self._summary()], [], [], [], {})
        assert payload["totals"]["month_change_pct"] is None

    def test_dividends_serialised_and_sorted(self):
        divs = [
            {"symbol": "B", "ex_date": date(2099, 6, 15), "amt": 1.0, "total_p": 10.0},
            {"symbol": "A", "ex_date": date(2099, 1, 5), "amt": 2.0, "total_p": 20.0},
        ]
        payload = portfolio_payload("USD", [], divs, [], [], {})
        assert [d["ex_date"] for d in payload["dividends"]] == [
            "2099-01-05", "2099-06-15",
        ]

    def test_cached_metadata(self):
        payload = portfolio_payload(
            "USD", [], [], [], [], {}, cached=True, cache_age=42,
        )
        assert payload["cached"] is True
        assert payload["cache_age_seconds"] == 42

    def test_generated_at_injectable(self):
        stamp = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
        payload = portfolio_payload("USD", [], [], [], [], {}, generated_at=stamp)
        assert payload["generated_at"].startswith("2026-08-02T12:00:00")

    def test_empty_portfolio(self):
        payload = portfolio_payload("USD", [], [], [], [], {})
        assert payload["positions"] == []
        assert payload["totals"]["value"] == 0.0
        assert payload["totals"]["daily_change_pct"] == 0.0


class TestCollectPortfolio:
    def _patch_fetches(self, mocker, summaries=None, aux=None):
        summaries = summaries if summaries is not None else {
            "AAPL": {
                "symbol": "AAPL", "qty": 10, "val_now": 1500.0, "val_prev": 1450.0,
                "chg_pct": 3.4, "daily_chg_val": 50.0, "source_currency": "USD",
                "conv": 1.0, "ticker_obj": object(),
            }
        }
        mocker.patch(
            "stock.fetch_summaries",
            return_value=(summaries, {"AAPL": "USD"}, []),
        )
        mocker.patch("stock.fetch_auxiliary", return_value=aux if aux is not None else {
            "history": [100.0, 110.0],
            "monthly": {"AAPL": 5.0},
            "traded": {"AAPL"},
            "dividends": {},
            "news": [],
            "analysts": {"AAPL": {"consensus": "Buy", "analyst_count": 46}},
        })

    def test_fresh_fetch(self, mocker, tmp_path):
        self._patch_fetches(mocker)
        payload = collect_portfolio(
            {"AAPL": 10}, "USD", cache_path=tmp_path / "c.json"
        )
        assert payload["cached"] is False
        assert payload["positions"][0]["analysts"]["consensus"] == "Buy"
        assert payload["totals"]["month_change_pct"] == pytest.approx(10.0)

    def test_result_is_cached_for_next_run(self, mocker, tmp_path):
        path = tmp_path / "c.json"
        self._patch_fetches(mocker)
        collect_portfolio({"AAPL": 10}, "USD", cache_path=path)

        cached = load_cached_portfolio({"AAPL": 10}, "USD", ttl=60, path=path)
        assert cached is not None
        assert cached["analysts"]["AAPL"]["consensus"] == "Buy"

    def test_cache_hit_skips_network(self, mocker, tmp_path):
        path = tmp_path / "c.json"
        save_cached_portfolio(
            {"AAPL": 10}, "USD",
            [{"symbol": "AAPL", "qty": 10, "val_now": 1.0, "val_prev": 1.0,
              "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "USD",
              "conv": 1.0}],
            [], [], [], {}, path=path, analysts={"AAPL": {"consensus": "Hold"}},
        )
        summaries = mocker.patch("stock.fetch_summaries")

        payload = collect_portfolio(
            {"AAPL": 10}, "USD", cache_ttl=600, cache_path=path
        )
        assert payload["cached"] is True
        assert payload["positions"][0]["analysts"]["consensus"] == "Hold"
        summaries.assert_not_called()

    def test_analysts_can_be_disabled(self, mocker, tmp_path):
        self._patch_fetches(mocker, aux={"dividends": {}, "news": []})
        payload = collect_portfolio(
            {"AAPL": 10}, "USD", want_analysts=False, cache_path=tmp_path / "c.json"
        )
        assert payload["positions"][0]["analysts"] is None
        from stock import fetch_auxiliary as patched
        assert patched.call_args.kwargs["want_analysts"] is False

    def test_empty_portfolio_does_not_write_cache(self, mocker, tmp_path):
        path = tmp_path / "c.json"
        self._patch_fetches(mocker, summaries={}, aux={})
        payload = collect_portfolio({}, "USD", cache_path=path)
        assert payload["positions"] == []
        assert not path.exists()


# ---------------------------------------------------------------------------
# config path resolution
# ---------------------------------------------------------------------------


class TestResolveConfigPath:
    def test_explicit_arg_wins(self, monkeypatch):
        monkeypatch.setenv("STOCK_PRICE_CONFIG", "/from/env.yaml")
        assert resolve_config_path("/explicit.yaml") == Path("/explicit.yaml")

    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("STOCK_PRICE_CONFIG", "/from/env.yaml")
        assert resolve_config_path() == Path("/from/env.yaml")

    def test_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("STOCK_PRICE_CONFIG", raising=False)
        assert resolve_config_path() == DEFAULT_CONFIG_PATH


# ---------------------------------------------------------------------------
# reading and writing the config document
# ---------------------------------------------------------------------------


COMMENTED_CONFIG = """\
# My portfolio
holdings:
  SVOL-B.ST: 8367   # Swedish investment company
  AAPL: 10          # bought 2024
currency: EUR
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "stock.yaml"
    path.write_text(COMMENTED_CONFIG)
    monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
    return path


class TestReadConfigDocument:
    def test_reads_holdings(self, config_file):
        document, path = read_config_document()
        assert path == config_file
        assert dict(document["holdings"]) == {"SVOL-B.ST": 8367, "AAPL": 10}
        assert document["currency"] == "EUR"

    def test_missing_file_is_empty_not_defaults(self, tmp_path, monkeypatch):
        # load_config() falls back to the demo holdings; writing those out
        # would add six positions the user never owned.
        missing = tmp_path / "nope.yaml"
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(missing))
        document, _ = read_config_document()
        assert dict(document["holdings"]) == {}
        assert set(DEFAULT_HOLDINGS) - set(document["holdings"]) == set(DEFAULT_HOLDINGS)

    def test_empty_file(self, tmp_path, monkeypatch):
        path = tmp_path / "empty.yaml"
        path.write_text("\n  \n")
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
        document, _ = read_config_document()
        assert dict(document["holdings"]) == {}

    def test_null_holdings_key(self, tmp_path, monkeypatch):
        path = tmp_path / "null.yaml"
        path.write_text("holdings:\ncurrency: USD\n")
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
        document, _ = read_config_document()
        assert dict(document["holdings"]) == {}

    def test_non_mapping_rejected(self, tmp_path, monkeypatch):
        path = tmp_path / "list.yaml"
        path.write_text("- one\n- two\n")
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
        with pytest.raises(ValueError, match="YAML mapping"):
            read_config_document()


class TestWriteConfigDocument:
    def test_creates_backup(self, config_file):
        document, path = read_config_document()
        document["holdings"]["AAPL"] = 99
        write_config_document(document, path)
        backup = path.with_name(path.name + ".bak")
        assert backup.exists()
        assert backup.read_text() == COMMENTED_CONFIG

    def test_no_temp_file_left_behind(self, config_file):
        document, path = read_config_document()
        write_config_document(document, path)
        assert not path.with_name(path.name + ".tmp").exists()

    def test_output_reloads(self, config_file):
        document, path = read_config_document()
        document["holdings"]["MC.PA"] = 45
        write_config_document(document, path)
        assert load_config(str(path))["holdings"]["MC.PA"] == 45

    def test_refuses_to_write_unloadable_config(self, config_file):
        document, path = read_config_document()
        del document["holdings"]
        with pytest.raises(ValueError, match="would not load back"):
            write_config_document(document, path)
        # The real file is untouched.
        assert path.read_text() == COMMENTED_CONFIG

    def test_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "stock.yaml"
        write_config_document({"holdings": {"AAPL": 1}, "currency": "USD"}, path)
        assert load_config(str(path))["holdings"] == {"AAPL": 1}


# ---------------------------------------------------------------------------
# editing holdings
# ---------------------------------------------------------------------------


class TestSetHolding:
    def test_updates_existing(self, config_file):
        result = set_holding("AAPL", 25)
        assert result["action"] == "updated"
        assert result["previous_quantity"] == 10
        assert result["quantity"] == 25
        assert load_config(str(config_file))["holdings"]["AAPL"] == 25

    def test_adds_new(self, config_file):
        result = set_holding("MC.PA", 45)
        assert result["action"] == "added"
        assert result["previous_quantity"] is None
        assert load_config(str(config_file))["holdings"]["MC.PA"] == 45

    def test_matches_existing_key_case_insensitively(self, config_file):
        result = set_holding("aapl", 25)
        assert result["symbol"] == "AAPL"
        holdings = load_config(str(config_file))["holdings"]
        assert holdings["AAPL"] == 25
        assert "aapl" not in holdings

    def test_whole_numbers_stored_as_int(self, config_file):
        # 25.0 would otherwise land in the YAML as "25.0", which reads oddly
        # for a share count.
        set_holding("AAPL", 25.0)
        assert "AAPL: 25" in config_file.read_text()
        assert "25.0" not in config_file.read_text()
        assert isinstance(load_config(str(config_file))["holdings"]["AAPL"], int)

    def test_fractional_shares_allowed(self, config_file):
        set_holding("AAPL", 2.5)
        assert load_config(str(config_file))["holdings"]["AAPL"] == 2.5

    @pytest.mark.parametrize("quantity", [0, -5])
    def test_non_positive_rejected(self, config_file, quantity):
        with pytest.raises(ValueError, match="must be positive"):
            set_holding("AAPL", quantity)
        assert config_file.read_text() == COMMENTED_CONFIG

    def test_empty_symbol_rejected(self, config_file):
        with pytest.raises(ValueError):
            set_holding("   ", 5)


class TestAddShares:
    def test_adds_to_existing(self, config_file):
        result = add_shares("AAPL", 5)
        assert result["previous_quantity"] == 10
        assert result["quantity"] == 15
        assert result["delta"] == 5
        assert result["action"] == "updated"

    def test_creates_when_new(self, config_file):
        result = add_shares("MC.PA", 45)
        assert result["action"] == "added"
        assert result["quantity"] == 45

    def test_subtracts_after_partial_sale(self, config_file):
        result = add_shares("AAPL", -4)
        assert result["quantity"] == 6
        assert load_config(str(config_file))["holdings"]["AAPL"] == 6

    def test_selling_everything_removes_the_holding(self, config_file):
        result = add_shares("AAPL", -10)
        assert result["action"] == "removed"
        assert "AAPL" not in load_config(str(config_file))["holdings"]

    def test_oversell_rejected(self, config_file):
        with pytest.raises(ValueError, match="cannot subtract"):
            add_shares("AAPL", -11)
        assert config_file.read_text() == COMMENTED_CONFIG

    def test_zero_rejected(self, config_file):
        with pytest.raises(ValueError, match="must not be zero"):
            add_shares("AAPL", 0)


class TestRemoveHolding:
    def test_removes(self, config_file):
        result = remove_holding("AAPL")
        assert result["action"] == "removed"
        assert result["previous_quantity"] == 10
        assert "AAPL" not in load_config(str(config_file))["holdings"]

    def test_unknown_symbol_rejected(self, config_file):
        with pytest.raises(KeyError):
            remove_holding("MSFT")
        assert config_file.read_text() == COMMENTED_CONFIG


@pytest.mark.skipif(not PRESERVES_COMMENTS, reason="ruamel.yaml not installed")
class TestCommentPreservation:
    def test_comments_and_order_survive_an_edit(self, config_file):
        add_shares("AAPL", 5)
        text = config_file.read_text()
        assert "# My portfolio" in text
        assert "# Swedish investment company" in text
        assert "# bought 2024" in text
        # Key order is unchanged: SVOL-B.ST still precedes AAPL.
        assert text.index("SVOL-B.ST") < text.index("AAPL")

    def test_new_holding_appended_without_disturbing_others(self, config_file):
        set_holding("MC.PA", 45)
        text = config_file.read_text()
        assert "SVOL-B.ST: 8367   # Swedish investment company" in text
        assert "MC.PA: 45" in text


# ---------------------------------------------------------------------------
# resolve_symbol
# ---------------------------------------------------------------------------


class TestResolveSymbol:
    def test_known_symbol(self, mocker):
        ticker = MagicMock()
        ticker.info = {"shortName": "Apple Inc."}
        mocker.patch(
            "stock._fast_quote",
            return_value=(ticker, {"currency": "USD"}, 200.0),
        )
        result = resolve_symbol("aapl")
        assert result == {
            "symbol": "AAPL", "status": "ok", "name": "Apple Inc.",
            "price": 200.0, "currency": "USD",
        }

    def test_priceless_symbol_is_unknown(self, mocker):
        mocker.patch("stock._fast_quote", return_value=(MagicMock(), {}, None))
        assert resolve_symbol("AAPL")["status"] == "unknown"

    def test_lookup_error_with_service_up_is_unknown(self, mocker):
        # A typo raises out of fast_info exactly like an outage does, so the
        # sentinel probe is what tells the two apart.
        mocker.patch("stock._fast_quote", side_effect=KeyError("exchangeTimezoneName"))
        mocker.patch("stock._price_service_reachable", return_value=True)
        assert resolve_symbol("NOPE")["status"] == "unknown"

    def test_lookup_error_with_service_down_is_unverified(self, mocker):
        mocker.patch("stock._fast_quote", side_effect=OSError("network down"))
        mocker.patch("stock._price_service_reachable", return_value=False)
        assert resolve_symbol("AAPL")["status"] == "unverified"

    def test_name_lookup_failure_is_not_fatal(self, mocker):
        ticker = MagicMock()
        type(ticker).info = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("429"))
        )
        mocker.patch(
            "stock._fast_quote", return_value=(ticker, {"currency": "USD"}, 5.0)
        )
        result = resolve_symbol("AAPL")
        assert result["status"] == "ok"
        assert result["name"] is None

    def test_empty_symbol_rejected(self):
        with pytest.raises(ValueError):
            resolve_symbol("  ")


class TestPriceServiceReachable:
    def test_true_when_sentinel_prices(self, mocker):
        mocker.patch("stock._fast_quote", return_value=(MagicMock(), {}, 100.0))
        assert _price_service_reachable() is True

    def test_false_when_sentinel_raises(self, mocker):
        mocker.patch("stock._fast_quote", side_effect=OSError("down"))
        assert _price_service_reachable() is False

    def test_false_when_sentinel_has_no_price(self, mocker):
        mocker.patch("stock._fast_quote", return_value=(MagicMock(), {}, None))
        assert _price_service_reachable() is False


class TestAnalystView:
    def test_returns_none_for_unknown_symbol(self, mocker):
        mocker.patch(
            "stock.resolve_symbol",
            return_value={"symbol": "NOPE", "status": "unknown", "currency": None},
        )
        assert analyst_view("NOPE") is None

    def test_wraps_instrument_and_analysts(self, mocker):
        mocker.patch(
            "stock.resolve_symbol",
            return_value={"symbol": "AAPL", "status": "ok", "currency": "USD"},
        )
        mocker.patch("stock.yf.Ticker", return_value=MagicMock())
        mocker.patch("stock.get_analyst_data", return_value={"consensus": "Buy"})
        view = analyst_view("AAPL")
        assert view["instrument"]["symbol"] == "AAPL"
        assert view["analysts"] == {"consensus": "Buy"}

    def test_unverified_symbol_still_attempted(self, mocker):
        mocker.patch(
            "stock.resolve_symbol",
            return_value={"symbol": "AAPL", "status": "unverified", "currency": None},
        )
        mocker.patch("stock.yf.Ticker", return_value=MagicMock())
        mocker.patch("stock.get_analyst_data", return_value=None)
        assert analyst_view("AAPL")["analysts"] is None


# ---------------------------------------------------------------------------
# cost basis in the config
# ---------------------------------------------------------------------------


class TestParseHoldings:
    def test_bare_quantities(self):
        quantities, cost = parse_holdings({"AAPL": 10, "MC.PA": 45})
        assert quantities == {"AAPL": 10, "MC.PA": 45}
        assert cost == {}

    def test_extended_form(self):
        quantities, cost = parse_holdings({"AAPL": {"qty": 10, "cost": 185.2}})
        assert quantities == {"AAPL": 10}
        assert cost == {"AAPL": 185.2}

    def test_mixed_forms_coexist(self):
        quantities, cost = parse_holdings(
            {"AAPL": {"qty": 10, "cost": 185.2}, "IUSA.DE": 720}
        )
        assert quantities == {"AAPL": 10, "IUSA.DE": 720}
        assert cost == {"AAPL": 185.2}

    def test_quantity_alias(self):
        quantities, _ = parse_holdings({"AAPL": {"quantity": 7}})
        assert quantities == {"AAPL": 7}

    def test_entry_without_quantity_is_skipped(self):
        quantities, cost = parse_holdings({"AAPL": {"cost": 100.0}})
        assert quantities == {}
        assert cost == {}

    @pytest.mark.parametrize("bad", [0, -5, "abc", None])
    def test_unusable_cost_ignored_but_quantity_kept(self, bad):
        quantities, cost = parse_holdings({"AAPL": {"qty": 10, "cost": bad}})
        assert quantities == {"AAPL": 10}
        assert cost == {}

    def test_empty(self):
        assert parse_holdings(None) == ({}, {})


class TestHoldingQuantity:
    def test_bare(self):
        assert holding_quantity(10) == 10

    def test_mapping(self):
        assert holding_quantity({"qty": 10, "cost": 5}) == 10

    def test_missing(self):
        assert holding_quantity({"cost": 5}) is None


class TestLoadConfigCostBasis:
    def _write(self, tmp_path, text):
        path = tmp_path / "c.yaml"
        path.write_text(text)
        return path

    def test_cost_basis_and_benchmark_loaded(self, tmp_path):
        path = self._write(tmp_path, """
holdings:
  AAPL: {qty: 10, cost: 185.20}
  IUSA.DE: 720
currency: EUR
benchmark: ^gspc
""")
        config = load_config(str(path))
        assert config["holdings"] == {"AAPL": 10, "IUSA.DE": 720}
        assert config["cost_basis"] == {"AAPL": 185.2}
        assert config["benchmark"] == "^GSPC"

    def test_defaults_when_absent(self, tmp_path):
        path = self._write(tmp_path, "holdings:\n  AAPL: 10\n")
        config = load_config(str(path))
        assert config["cost_basis"] == {}
        assert config["benchmark"] is None


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------


class TestGetTickerSummaryReturns:
    def _patch(self, mocker, price=200.0, currency="USD"):
        ticker = MagicMock()
        mocker.patch(
            "stock._retry",
            return_value=(
                ticker,
                {"currency": currency, "regularMarketPreviousClose": 190.0},
                price,
            ),
        )

    def test_no_cost_leaves_return_fields_empty(self, mocker):
        self._patch(mocker)
        result = get_ticker_summary("AAPL", 10, "USD", {})
        assert result["cost"] is None
        assert result["unrealized"] is None
        assert result["return_pct"] is None

    def test_profit(self, mocker):
        self._patch(mocker, price=200.0)
        result = get_ticker_summary("AAPL", 10, "USD", {}, cost=100.0)
        assert result["cost_value"] == pytest.approx(1000.0)
        assert result["unrealized"] == pytest.approx(1000.0)
        assert result["return_pct"] == pytest.approx(100.0)

    def test_loss(self, mocker):
        self._patch(mocker, price=80.0)
        result = get_ticker_summary("AAPL", 10, "USD", {}, cost=100.0)
        assert result["unrealized"] == pytest.approx(-200.0)
        assert result["return_pct"] == pytest.approx(-20.0)

    def test_return_is_fx_neutral(self, mocker):
        # Cost is recorded in the ticker's own currency, so the percentage must
        # not move with the exchange rate — only the converted amount does.
        self._patch(mocker, price=200.0, currency="USD")
        result = get_ticker_summary("AAPL", 10, "EUR", {"USDEUR=X": 0.5}, cost=100.0)
        assert result["return_pct"] == pytest.approx(100.0)
        assert result["unrealized"] == pytest.approx(500.0)


class TestFetchSummariesCostBasis:
    def test_cost_passed_per_symbol(self, mocker):
        seen = {}

        def fake(symbol, qty, currency, rate_cache, prev_close_cache=None, cost=None):
            seen[symbol] = cost
            return {"symbol": symbol, "source_currency": "USD", "val_now": qty}

        mocker.patch("stock.get_ticker_summary", side_effect=fake)
        fetch_summaries(
            {"AAPL": 1, "MSFT": 2}, "USD", {}, cost_basis={"AAPL": 50.0}
        )
        assert seen == {"AAPL": 50.0, "MSFT": None}


# ---------------------------------------------------------------------------
# cost basis survives config edits
# ---------------------------------------------------------------------------


COST_CONFIG = """\
holdings:
  AAPL:
    qty: 10
    cost: 185.20
  IUSA.DE: 720
currency: EUR
"""


@pytest.fixture
def cost_config_file(tmp_path, monkeypatch):
    path = tmp_path / "cost.yaml"
    path.write_text(COST_CONFIG)
    monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
    return path


class TestCostBasisSurvivesEdits:
    def test_quantity_edit_keeps_cost(self, cost_config_file):
        set_holding("AAPL", 25)
        config = load_config(str(cost_config_file))
        assert config["holdings"]["AAPL"] == 25
        assert config["cost_basis"]["AAPL"] == 185.2

    def test_add_shares_keeps_cost(self, cost_config_file):
        add_shares("AAPL", 5)
        config = load_config(str(cost_config_file))
        assert config["holdings"]["AAPL"] == 15
        assert config["cost_basis"]["AAPL"] == 185.2

    def test_cost_can_be_updated(self, cost_config_file):
        set_holding("AAPL", 10, cost=200.0)
        assert load_config(str(cost_config_file))["cost_basis"]["AAPL"] == 200.0

    def test_cost_can_be_added_to_a_bare_holding(self, cost_config_file):
        add_shares("IUSA.DE", 10, cost=42.5)
        config = load_config(str(cost_config_file))
        assert config["holdings"]["IUSA.DE"] == 730
        assert config["cost_basis"]["IUSA.DE"] == 42.5

    def test_selling_out_removes_cost_too(self, cost_config_file):
        add_shares("AAPL", -10)
        config = load_config(str(cost_config_file))
        assert "AAPL" not in config["holdings"]
        assert "AAPL" not in config["cost_basis"]

    @pytest.mark.parametrize("bad", [0, -1])
    def test_invalid_cost_rejected_and_nothing_written(self, cost_config_file, bad):
        with pytest.raises(ValueError, match="cost must be a positive number"):
            set_holding("AAPL", 10, cost=bad)
        assert cost_config_file.read_text() == COST_CONFIG


# ---------------------------------------------------------------------------
# dividend income
# ---------------------------------------------------------------------------


def _dividend_series(values, days_ago):
    index = pd.to_datetime(
        [datetime.now(ZoneInfo("UTC")) - timedelta(days=d) for d in days_ago]
    )
    return pd.Series(values, index=index)


class TestDividendIncome:
    def _summary(self, **overrides):
        summary = {
            "symbol": "MC.PA",
            "ticker_obj": MagicMock(),
            "conv": 1.0,
            "qty": 10,
            "source_currency": "EUR",
            "price": 100.0,
        }
        summary.update(overrides)
        return summary

    def test_trailing_twelve_months_only(self):
        summary = self._summary()
        # 2.0 inside the window, 9.0 outside it.
        summary["ticker_obj"].dividends = _dividend_series([1.0, 1.0, 9.0], [10, 200, 400])
        summary["ticker_obj"].calendar = None
        result = get_dividend_data(summary)
        assert result["ttm_per_share"] == pytest.approx(2.0)
        assert result["ttm_total"] == pytest.approx(20.0)

    def test_yield_and_yield_on_cost(self):
        summary = self._summary(cost=50.0)
        summary["ticker_obj"].dividends = _dividend_series([5.0], [30])
        summary["ticker_obj"].calendar = None
        result = get_dividend_data(summary)
        assert result["yield_pct"] == pytest.approx(5.0)
        assert result["yield_on_cost_pct"] == pytest.approx(10.0)

    def test_income_reported_without_an_upcoming_payout(self):
        summary = self._summary()
        summary["ticker_obj"].dividends = _dividend_series([1.0], [30])
        summary["ticker_obj"].calendar = None
        result = get_dividend_data(summary)
        assert result["ex_date"] is None
        assert result["ttm_total"] == pytest.approx(10.0)

    def test_upcoming_payout_still_reported(self):
        summary = self._summary()
        summary["ticker_obj"].calendar = {"Ex-Dividend Date": date(2099, 6, 15)}
        summary["ticker_obj"].info = {"lastDividendValue": 2.5}
        summary["ticker_obj"].dividends = _dividend_series([1.0], [30])
        result = get_dividend_data(summary)
        assert result["ex_date"] == date(2099, 6, 15)
        assert result["total_p"] == pytest.approx(25.0)
        assert result["ttm_total"] == pytest.approx(10.0)

    def test_no_dividends_at_all(self):
        summary = self._summary()
        summary["ticker_obj"].calendar = None
        summary["ticker_obj"].dividends = pd.Series(dtype=float)
        assert get_dividend_data(summary) is None

    def test_non_series_dividends_are_tolerated(self):
        summary = self._summary()
        summary["ticker_obj"].calendar = None
        summary["ticker_obj"].dividends = MagicMock()
        assert get_dividend_data(summary) is None


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


class TestFetchBenchmark:
    def test_change_computed(self, mocker):
        frame = pd.DataFrame({"Close": [100.0, 105.0, 110.0]})
        mocker.patch("stock.yf.Ticker", return_value=MagicMock(history=lambda **k: frame))
        result = fetch_benchmark("^GSPC")
        assert result["symbol"] == "^GSPC"
        assert result["change_pct"] == pytest.approx(10.0)
        assert result["points"] == [100.0, 105.0, 110.0]

    def test_no_symbol(self):
        assert fetch_benchmark(None) is None
        assert fetch_benchmark("") is None

    def test_single_point_is_not_a_trend(self, mocker):
        frame = pd.DataFrame({"Close": [100.0]})
        mocker.patch("stock.yf.Ticker", return_value=MagicMock(history=lambda **k: frame))
        assert fetch_benchmark("^GSPC") is None

    def test_failure_is_not_fatal(self, mocker):
        mocker.patch("stock.yf.Ticker", side_effect=RuntimeError("boom"))
        assert fetch_benchmark("^GSPC") is None

    def test_requested_only_alongside_history(self, mocker):
        bench = mocker.patch("stock.fetch_benchmark", return_value={"symbol": "^GSPC"})
        mocker.patch("stock.fetch_history", return_value=([1.0], {}, set()))
        fetch_auxiliary({}, "USD", {}, {}, want_history=True, benchmark="^GSPC")
        bench.assert_called_once()

        bench.reset_mock()
        fetch_auxiliary({}, "USD", {}, {}, want_history=False, benchmark="^GSPC")
        bench.assert_not_called()


# ---------------------------------------------------------------------------
# recorded portfolio history
# ---------------------------------------------------------------------------


class TestPortfolioHistory:
    def test_records_one_point_per_day(self, tmp_path):
        path = tmp_path / "h.json"
        record_portfolio_value(100.0, "EUR", path=path, today=date(2026, 1, 1))
        record_portfolio_value(110.0, "EUR", path=path, today=date(2026, 1, 2))
        data = json.loads(path.read_text())
        assert data["points"] == {"2026-01-01": 100.0, "2026-01-02": 110.0}

    def test_same_day_overwrites(self, tmp_path):
        path = tmp_path / "h.json"
        record_portfolio_value(100.0, "EUR", path=path, today=date(2026, 1, 1))
        record_portfolio_value(150.0, "EUR", path=path, today=date(2026, 1, 1))
        assert json.loads(path.read_text())["points"] == {"2026-01-01": 150.0}

    def test_currency_change_resets_the_series(self, tmp_path):
        # Values in different currencies are not comparable; a mixed series
        # would render a trend that never happened.
        path = tmp_path / "h.json"
        record_portfolio_value(100.0, "EUR", path=path, today=date(2026, 1, 1))
        record_portfolio_value(120.0, "USD", path=path, today=date(2026, 1, 2))
        data = json.loads(path.read_text())
        assert data["currency"] == "USD"
        assert data["points"] == {"2026-01-02": 120.0}

    @pytest.mark.parametrize("bad", [None, 0, -5, float("nan")])
    def test_unusable_values_ignored(self, tmp_path, bad):
        path = tmp_path / "h.json"
        assert record_portfolio_value(bad, "EUR", path=path) is None
        assert not path.exists()

    def test_retention_prunes_oldest(self, tmp_path, mocker):
        mocker.patch("stock.HISTORY_RETENTION_DAYS", 3)
        path = tmp_path / "h.json"
        for day in range(1, 6):
            record_portfolio_value(
                float(day), "EUR", path=path, today=date(2026, 1, day)
            )
        assert sorted(json.loads(path.read_text())["points"]) == [
            "2026-01-03", "2026-01-04", "2026-01-05",
        ]

    def test_load_returns_values_oldest_first(self, tmp_path):
        path = tmp_path / "h.json"
        today = datetime.now().date()
        for i, value in enumerate([10.0, 20.0, 30.0]):
            record_portfolio_value(
                value, "EUR", path=path, today=today - timedelta(days=2 - i)
            )
        assert load_portfolio_history("EUR", path=path) == [10.0, 20.0, 30.0]

    def test_load_ignores_a_different_currency(self, tmp_path):
        path = tmp_path / "h.json"
        record_portfolio_value(10.0, "EUR", path=path)
        assert load_portfolio_history("USD", path=path) == []

    def test_load_drops_points_outside_the_window(self, tmp_path):
        path = tmp_path / "h.json"
        today = datetime.now().date()
        record_portfolio_value(1.0, "EUR", path=path, today=today - timedelta(days=90))
        record_portfolio_value(2.0, "EUR", path=path, today=today)
        assert load_portfolio_history("EUR", path=path, days=30) == [2.0]

    def test_missing_file(self, tmp_path):
        assert load_portfolio_history("EUR", path=tmp_path / "nope.json") == []


# ---------------------------------------------------------------------------
# the new panels and columns
# ---------------------------------------------------------------------------


class TestReturnColumns:
    def _summary(self, symbol="AAPL", cost=100.0, price=150.0, qty=10):
        return {
            "symbol": symbol,
            "qty": qty,
            "val_now": price * qty,
            "val_prev": price * qty,
            "chg_pct": 0.0,
            "daily_chg_val": 0.0,
            "source_currency": "USD",
            "conv": 1.0,
            "price": price,
            "cost": cost,
            "cost_value": (cost * qty) if cost else None,
            "unrealized": ((price - cost) * qty) if cost else None,
            "return_pct": (((price - cost) / cost) * 100) if cost else None,
        }

    def test_hidden_without_any_cost_basis(self):
        out = _render(build_display_group([self._summary(cost=None)], [], "USD"))
        assert "Return %" not in out

    def test_shown_when_cost_is_known(self):
        out = _render(build_display_group([self._summary()], [], "USD"))
        assert "Return %" in out
        assert "+50.00%" in out
        assert "+500.00" in out

    def test_loss_rendered(self):
        out = _render(build_display_group([self._summary(price=50.0)], [], "USD"))
        assert "-50.00%" in out

    def test_position_without_cost_shows_dash(self):
        out = _render(
            build_display_group(
                [self._summary("AAPL"), self._summary("MSFT", cost=None)], [], "USD"
            )
        )
        table_out = out.split("Allocation")[0]
        msft = next(ln for ln in table_out.splitlines() if "MSFT" in ln)
        assert "%" not in msft.split("MSFT")[1].replace("+0.00%", "")

    def test_total_return_uses_only_priced_positions(self):
        # MSFT has no cost, so it must not dilute the total return figure.
        out = _render(
            build_display_group(
                [self._summary("AAPL"), self._summary("MSFT", cost=None)], [], "USD"
            )
        )
        assert "+50.00%" in out


class TestAllocationPanel:
    def _summary(self, symbol, value, currency="USD"):
        return {
            "symbol": symbol, "qty": 1, "val_now": value, "val_prev": value,
            "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": currency,
            "conv": 1.0,
        }

    def test_weights_and_currency_exposure(self):
        out = _render(
            build_display_group(
                [self._summary("AAPL", 750.0, "USD"),
                 self._summary("MC.PA", 250.0, "EUR")],
                [], "USD",
            )
        )
        assert "Allocation" in out
        assert "75.0%" in out
        assert "25.0%" in out
        assert "By currency" in out
        assert "USD 75.0%" in out

    def test_hidden_for_a_single_holding(self):
        out = _render(build_display_group([self._summary("AAPL", 100.0)], [], "USD"))
        assert "Allocation" not in out


class TestTrendPanel:
    def _summary(self):
        return {
            "symbol": "AAPL", "qty": 1, "val_now": 100.0, "val_prev": 100.0,
            "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "USD",
        }

    def test_basket_label_without_recorded_history(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD", history_points=[90.0, 100.0]
            )
        )
        assert "30D BASKET" in out
        assert "today's holdings, priced back" in out

    def test_portfolio_label_once_enough_days_recorded(self):
        recorded = [float(90 + i) for i in range(MIN_RECORDED_HISTORY_POINTS)]
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                history_points=[10.0, 20.0], portfolio_history=recorded,
            )
        )
        assert "30D PORTFOLIO" in out
        assert "30D BASKET" not in out

    def test_too_few_recorded_points_falls_back(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                history_points=[90.0, 100.0], portfolio_history=[99.0, 100.0],
            )
        )
        assert "30D BASKET" in out

    def test_benchmark_comparison(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD",
                history_points=[100.0, 110.0],
                benchmark={"symbol": "^GSPC", "change_pct": 4.0},
            )
        )
        assert "^GSPC" in out
        assert "+4.00%" in out
        assert "+6.00 pts" in out

    def test_no_benchmark_no_comparison(self):
        out = _render(
            build_display_group(
                [self._summary()], [], "USD", history_points=[100.0, 110.0]
            )
        )
        assert "pts" not in out


class TestDividendIncomeTable:
    def _div(self, symbol="AAPL", ex_date=None, ttm_total=0.0, on_cost=None):
        return {
            "symbol": symbol, "ex_date": ex_date, "amt": 1.0 if ex_date else None,
            "total_p": 10.0 if ex_date else None, "cur_label": "$",
            "ttm_per_share": 1.0 if ttm_total else 0.0, "ttm_total": ttm_total,
            "yield_pct": 2.0 if ttm_total else None, "yield_on_cost_pct": on_cost,
        }

    def _summary(self, value=1000.0, cost_value=None):
        return {
            "symbol": "AAPL", "qty": 10, "val_now": value, "val_prev": value,
            "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "USD",
            "cost_value": cost_value,
            "unrealized": 0.0 if cost_value else None,
            "return_pct": 0.0 if cost_value else None,
        }

    def test_income_shown_without_an_upcoming_payout(self):
        out = _render(
            build_display_group([self._summary()], [self._div(ttm_total=40.0)], "USD")
        )
        assert "12M Income" in out
        assert "40.00" in out

    def test_totals_row(self):
        out = _render(
            build_display_group(
                [self._summary()],
                [self._div("AAPL", ttm_total=40.0), self._div("MSFT", ttm_total=60.0)],
                "USD",
            )
        )
        assert "100.00" in out

    def test_yield_on_cost_total_excludes_unpriced_income(self):
        # AAPL has a cost basis, MSFT does not. Dividing both incomes by only
        # AAPL's cost would report 20% instead of 10%.
        out = _render(
            build_display_group(
                [self._summary(cost_value=1000.0)],
                [
                    self._div("AAPL", ttm_total=100.0, on_cost=10.0),
                    self._div("MSFT", ttm_total=100.0),
                ],
                "USD",
            )
        )
        # The summary table has a TOTAL row too; this one is the dividends table's.
        dividends_out = out.split("12M Income")[1]
        total_row = next(ln for ln in dividends_out.splitlines() if "TOTAL" in ln)
        cells = [c.strip() for c in total_row.split("│") if c.strip()]
        # Yield is 200/1000 across every holding; on-cost is 100/1000 using
        # only the position that actually has a cost basis.
        assert cells[-2] == "20.00%"
        assert cells[-1] == "10.00%"


# ---------------------------------------------------------------------------
# responsive tables
# ---------------------------------------------------------------------------


def _render_at(group, width):
    console = Console(width=width)
    with console.capture() as cap:
        console.print(group)
    return cap.get()


def _headers(out, contains="Ticker"):
    """The header row of a table, so assertions don't match the footer's
    "hidden: ..." list, which names the very columns that were dropped."""
    return next(ln for ln in out.splitlines() if contains in ln and "┃" in ln)


class TestResponsiveTable:
    def _summary(self, symbol="AAPL", value=1500.0):
        return {
            "symbol": symbol, "qty": 10, "val_now": value, "val_prev": value,
            "chg_pct": 1.5, "daily_chg_val": 20.0, "source_currency": "USD",
            "conv": 1.0, "price": 150.0, "cost": 100.0, "cost_value": 1000.0,
            "unrealized": 500.0, "return_pct": 50.0,
        }

    def _analysts(self):
        return {
            "AAPL": {
                "consensus": "Strong Buy", "trend": "up", "analyst_count": 46,
                "price_target": {"upside_pct": 19.8},
            }
        }

    def _group(self, width):
        return build_display_group(
            [self._summary()], [], "USD",
            analyst_results=self._analysts(), max_width=width,
        )

    @pytest.mark.parametrize("width", [80, 100, 120, 140, 200])
    def test_never_truncates(self, width):
        # Rich squeezes every column at once when the table is too wide, which
        # turns figures into "+66.8…". Shedding columns must prevent that.
        out = _render_at(self._group(width), width)
        assert "…" not in out

    @pytest.mark.parametrize("width", [80, 100, 120, 140, 200])
    def test_essentials_always_survive(self, width):
        out = _render_at(self._group(width), width)
        assert "AAPL" in out
        assert "Value" in out
        assert "Day %" in out
        assert "1,500.00" in out

    def test_wide_terminal_keeps_everything(self):
        headers = _headers(_render_at(self._group(200), 200))
        for header in ("Quantity", "Daily", "Month %", "P/L", "Return %",
                       "Analysts", "Target"):
            assert header in headers, header

    def test_narrower_terminals_show_no_more_columns(self):
        counts = []
        for width in (80, 100, 120, 140, 200):
            out = _render_at(self._group(width), width)
            header = next(ln for ln in out.splitlines() if "Ticker" in ln)
            counts.append(header.count("┃") - 1)
        assert counts == sorted(counts), counts

    def test_returns_outlive_analysts(self):
        # Cost basis is opt-in, so if the user recorded it they want to see it
        # more than they want third-party opinions.
        out = _render_at(self._group(100), 100)
        headers = _headers(out)
        assert "Return %" in headers
        assert "Target" not in headers

    def test_dropped_columns_are_named_in_the_footer(self):
        # A couple of drops are listed by name...
        out = _render_at(self._group(120), 120)
        note = out.split("narrow terminal")[1]
        assert "Quantity" in note
        assert "columns hidden" not in note

    def test_many_dropped_columns_are_summarised(self):
        # ...but on a phone the list would be longer than the table itself.
        out = _render_at(self._group(100), 100)
        note = out.split("narrow terminal")[1]
        assert "columns hidden" in note
        assert "Quantity" not in note

    def test_no_footer_note_when_nothing_is_dropped(self):
        out = _render_at(self._group(200), 200)
        assert "narrow terminal" not in out

    def test_footer_note_appends_to_existing_text(self):
        group = build_display_group(
            [self._summary()], [], "USD", "Last update: 12:00:00",
            analyst_results=self._analysts(), max_width=100,
        )
        out = _render_at(group, 100)
        assert "Last update: 12:00:00" in out
        assert "narrow terminal" in out

    def test_dividends_table_also_sheds(self):
        dividends = [{
            "symbol": "AAPL", "ex_date": date(2099, 6, 15), "amt": 1.0,
            "total_p": 10.0, "cur_label": "$", "ttm_per_share": 4.0,
            "ttm_total": 40.0, "yield_pct": 2.0, "yield_on_cost_pct": 4.0,
        }]
        wide = _render_at(
            build_display_group([self._summary()], dividends, "USD", max_width=200),
            200,
        )
        narrow = _render_at(
            build_display_group([self._summary()], dividends, "USD", max_width=80),
            80,
        )
        assert "Amount" in _headers(wide, "Ex-Date")
        assert "Amount" not in _headers(narrow, "Ex-Date")
        assert "…" not in narrow
        # The income figure is the point of the table; it must never be shed.
        assert "12M Income" in _headers(narrow, "Ex-Date")
        assert "40.00" in narrow


class TestFitColumns:
    def _cols(self):
        return [
            {"key": "a", "header": "A", "width": 10},
            {"key": "b", "header": "B", "width": 10},
            {"key": "c", "header": "C", "width": 10},
        ]

    def test_keeps_everything_when_it_fits(self):
        visible, dropped = _fit_columns(self._cols(), 500, ("b", "c"))
        assert len(visible) == 3
        assert dropped == []

    def test_sheds_in_the_given_order(self):
        visible, dropped = _fit_columns(self._cols(), 30, ("c", "b"))
        assert dropped == ["C"]
        assert [c["key"] for c in visible] == ["a", "b"]

    def test_stops_once_it_fits(self):
        _, dropped = _fit_columns(self._cols(), 30, ("c", "b", "a"))
        assert len(dropped) == 1

    def test_never_drops_a_column_absent_from_the_order(self):
        visible, _ = _fit_columns(self._cols(), 1, ("b", "c"))
        assert [c["key"] for c in visible] == ["a"]

    def test_unknown_available_width_keeps_everything(self):
        visible, dropped = _fit_columns(self._cols(), None, ("b", "c"))
        assert len(visible) == 3
        assert dropped == []

    def test_width_matches_what_rich_renders(self):
        # The shedding decision is only as good as this estimate.
        columns = self._cols()
        table = Table()
        for column in columns:
            table.add_column(column["header"], width=column["width"], no_wrap=True)
        table.add_row("x", "y", "z")
        console = Console(width=500)
        with console.capture() as cap:
            console.print(table)
        rendered = max(len(ln.rstrip()) for ln in cap.get().splitlines())
        assert _table_width(columns) == rendered


# ---------------------------------------------------------------------------
# averaging the cost basis across purchases
# ---------------------------------------------------------------------------


class TestHoldingCost:
    def test_mapping(self):
        assert holding_cost({"qty": 10, "cost": 185.2}) == 185.2

    def test_alias(self):
        assert holding_cost({"qty": 10, "cost_basis": 12.5}) == 12.5

    def test_bare_entry_has_none(self):
        assert holding_cost(10) is None

    def test_missing_and_unparseable(self):
        assert holding_cost({"qty": 10}) is None
        assert holding_cost({"qty": 10, "cost": "abc"}) is None


class TestBlendCost:
    def test_weighted_average(self):
        assert blend_cost(10, 100.0, 10, 200.0) == 150.0

    def test_weights_by_size(self):
        # 30 at 100 plus 10 at 200 is 125, not 150.
        assert blend_cost(30, 100.0, 10, 200.0) == 125.0

    def test_first_purchase_uses_the_price_paid(self):
        assert blend_cost(0, None, 10, 185.2) == 185.2

    def test_existing_position_without_a_basis(self):
        assert blend_cost(10, None, 5, 50.0) == 50.0

    def test_no_price_leaves_the_basis_alone(self):
        assert blend_cost(10, 100.0, 10, None) is None

    def test_sales_do_not_reprice(self):
        assert blend_cost(10, 100.0, -5, 200.0) is None

    def test_rounded_to_avoid_float_noise(self):
        # (10*185.20 + 5*310) / 15 = 226.8 exactly; without rounding this
        # lands as 226.79999999999998 in the config file.
        assert blend_cost(10, 185.20, 5, 310.0) == 226.8


class TestAddSharesAveragesCost:
    """Regression cover for a bug shipped in 0.7.0.

    ``add_shares(..., cost=X)`` replaced the average cost instead of blending
    it, so buying more at a different price silently corrupted the reported
    profit and loss for the whole position.
    """

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        path = tmp_path / "avg.yaml"
        path.write_text(
            "holdings:\n  AAPL:\n    qty: 10\n    cost: 100.0\n  BARE: 5\ncurrency: EUR\n"
        )
        monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
        return path

    def _cost(self, config, symbol="AAPL"):
        return load_config(str(config))["cost_basis"].get(symbol)

    def test_buying_higher_raises_the_average(self, config):
        result = add_shares("AAPL", 10, cost=200.0)
        assert result["cost"] == 150.0
        assert self._cost(config) == 150.0

    def test_buying_lower_lowers_the_average(self, config):
        add_shares("AAPL", 10, cost=50.0)
        assert self._cost(config) == 75.0

    def test_buying_without_a_price_keeps_the_average(self, config):
        add_shares("AAPL", 10)
        assert self._cost(config) == 100.0

    def test_selling_does_not_change_the_average(self, config):
        add_shares("AAPL", -5)
        assert self._cost(config) == 100.0

    def test_a_price_passed_on_a_sale_is_ignored(self, config):
        add_shares("AAPL", -5, cost=999.0)
        assert self._cost(config) == 100.0

    def test_cost_can_be_introduced_to_a_bare_holding(self, config):
        add_shares("BARE", 5, cost=50.0)
        assert self._cost(config, "BARE") == 50.0

    def test_a_brand_new_holding_takes_the_price_paid(self, config):
        add_shares("MSFT", 3, cost=400.0)
        assert self._cost(config, "MSFT") == 400.0

    def test_successive_purchases_compound_correctly(self, config):
        add_shares("AAPL", 10, cost=200.0)   # 20 @ 150
        add_shares("AAPL", 20, cost=300.0)   # 40 @ 225
        assert self._cost(config) == 225.0

    def test_set_holding_still_replaces(self, config):
        # set_holding means "this is the position", so replacing is right.
        set_holding("AAPL", 20, cost=42.0)
        assert self._cost(config) == 42.0


# ---------------------------------------------------------------------------
# fitting the output to the terminal height
# ---------------------------------------------------------------------------


class TestVerticalFit:
    def _summaries(self, count=8):
        return [
            {
                "symbol": f"TICK{i}.ST", "qty": 100, "val_now": 1000.0,
                "val_prev": 1000.0, "chg_pct": 0.0, "daily_chg_val": 0.0,
                "source_currency": "SEK", "conv": 1.0,
            }
            for i in range(count)
        ]

    def _news(self, count=15):
        return [
            {
                "symbol": "TICK0.ST", "title": f"Headline number {i}",
                "link": "", "provider": "Reuters", "pub_date": "2026-08-02 10:00",
                "summary": "A reasonably long summary sentence for this story.",
            }
            for i in range(count)
        ]

    def _dividends(self):
        return [{
            "symbol": "TICK0.ST", "ex_date": date(2099, 1, 1), "amt": 1.0,
            "total_p": 10.0, "cur_label": "kr", "ttm_per_share": 1.0,
            "ttm_total": 100.0, "yield_pct": 2.0, "yield_on_cost_pct": None,
        }]

    def _group(self, height):
        return build_display_group(
            self._summaries(), self._dividends(), "EUR",
            history_points=[100.0, 105.0, 103.0, 110.0],
            news_items=self._news(), max_width=150, max_height=height,
        )

    @pytest.mark.parametrize("height", [24, 30, 45, 60, 200])
    def test_output_fits_the_terminal(self, height):
        out = _render_at(self._group(height), 150)
        assert len(out.splitlines()) <= height, out

    def test_tall_terminal_keeps_everything(self):
        out = _render_at(self._group(200), 150)
        assert "Related News" in out
        assert "Allocation" in out
        assert "30D" in out

    def test_the_summary_table_always_survives(self):
        # Shedding exists so this never scrolls away; it is the whole point.
        for height in (24, 30, 45, 60, 200):
            out = _render_at(self._group(height), 150)
            assert "Portfolio Summary" in out
            assert "TOTAL" in out

    def test_news_is_trimmed_before_it_is_dropped(self):
        out = _render_at(self._group(60), 150)
        assert "Related News" in out
        # The count in the title says how many survived.
        assert "Related News (15)" not in out

    def test_news_goes_before_allocation(self):
        out = _render_at(self._group(45), 150)
        assert "Related News" not in out

    def test_dropped_panels_are_named_in_the_footer(self):
        out = _render_at(self._group(45), 150)
        assert "hidden to fit the window" in out
        assert "news" in out.split("hidden to fit the window")[1]

    def test_no_footer_note_when_everything_fits(self):
        out = _render_at(self._group(200), 150)
        assert "hidden to fit the window" not in out


class TestOnCostColumnVisibility:
    def _summary(self):
        return {
            "symbol": "AAPL", "qty": 10, "val_now": 1000.0, "val_prev": 1000.0,
            "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "USD",
        }

    def _dividend(self, on_cost=None):
        return {
            "symbol": "AAPL", "ex_date": None, "amt": None, "total_p": None,
            "cur_label": "$", "ttm_per_share": 1.0, "ttm_total": 10.0,
            "yield_pct": 1.0, "yield_on_cost_pct": on_cost,
        }

    def test_hidden_when_no_holding_has_a_cost_basis(self):
        # A column of nothing but dashes is noise.
        out = _render(build_display_group([self._summary()], [self._dividend()], "USD"))
        assert "On Cost" not in out

    def test_shown_once_a_cost_basis_exists(self):
        out = _render(
            build_display_group([self._summary()], [self._dividend(on_cost=4.0)], "USD")
        )
        assert "On Cost" in out
        assert "4.00%" in out


# ---------------------------------------------------------------------------
# choosing a previous close
# ---------------------------------------------------------------------------


def _ticker_with_history(days_ago, close, fast_info, include_today=False):
    """A ticker whose daily series has its latest bar `days_ago` days back."""
    today = pd.Timestamp.now().normalize()
    rows = [(today - pd.Timedelta(days=days_ago), close)]
    if include_today:
        rows.append((today, fast_info["lastPrice"]))
    mock = MagicMock()
    mock.fast_info = fast_info
    mock.history.return_value = pd.DataFrame(
        {"Close": [c for _, c in rows]}, index=pd.DatetimeIndex([d for d, _ in rows])
    )
    return mock


class TestPreviousCloseSelection:
    """Three unreliable sources; picking wrong turns Day % into a multi-day
    change, which is exactly what made the reported percentages drift."""

    def _fast_info(self, **overrides):
        info = {
            "lastPrice": 66.59,
            "regularMarketPreviousClose": float("nan"),
            "previousClose": 65.62,
            "currency": "EUR",
        }
        info.update(overrides)
        return info

    def test_authoritative_value_wins(self, mocker):
        ticker = _ticker_with_history(
            1, 60.0, self._fast_info(regularMarketPreviousClose=65.0)
        )
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 65.0) / 65.0 * 100)

    def test_recent_history_bar_is_used(self, mocker):
        ticker = _ticker_with_history(1, 64.0, self._fast_info())
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 64.0) / 64.0 * 100)

    def test_friday_to_monday_gap_is_still_trusted(self, mocker):
        ticker = _ticker_with_history(3, 64.0, self._fast_info())
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 64.0) / 64.0 * 100)

    def test_gappy_history_falls_back_to_reported_close(self, mocker):
        # The real failure: IUSA.DE's series jumped 07-31 -> 08-04, so the
        # "previous" bar was four days old and Day % read +3.46% instead of
        # +1.47%.
        ticker = _ticker_with_history(4, 64.36, self._fast_info())
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 65.62) / 65.62 * 100)

    def test_gappy_history_still_used_when_reported_close_mirrors_price(self, mocker):
        # previousClose sometimes equals lastPrice, which would zero the change.
        # A stale close beats reporting no move at all.
        ticker = _ticker_with_history(4, 64.36, self._fast_info(previousClose=66.59))
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 64.36) / 64.36 * 100)

    def test_todays_bar_is_skipped_when_present(self, mocker):
        ticker = _ticker_with_history(1, 64.0, self._fast_info(), include_today=True)
        mocker.patch("yfinance.Ticker", return_value=ticker)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx((66.59 - 64.0) / 64.0 * 100)

    def test_no_usable_source_leaves_the_change_flat(self, mocker):
        mock = MagicMock()
        mock.fast_info = self._fast_info(previousClose=float("nan"))
        mock.history.return_value = pd.DataFrame({"Close": []})
        mocker.patch("yfinance.Ticker", return_value=mock)
        result = get_ticker_summary("IUSA.DE", 1, "EUR", {})
        assert result["chg_pct"] == 0


class TestPreviousCloseFromHistory:
    def test_reports_the_age_of_the_bar(self):
        ticker = _ticker_with_history(
            4, 64.36, {"lastPrice": 66.59}, include_today=False
        )
        close, age = _previous_close_from_history(ticker)
        assert close == pytest.approx(64.36)
        assert age == 4

    def test_age_measured_from_the_bar_before_today(self):
        ticker = _ticker_with_history(
            2, 64.0, {"lastPrice": 66.59}, include_today=True
        )
        close, age = _previous_close_from_history(ticker)
        assert close == pytest.approx(64.0)
        assert age == 2

    def test_empty_history(self):
        mock = MagicMock()
        mock.history.return_value = pd.DataFrame({"Close": []})
        assert _previous_close_from_history(mock) == (None, None)

    def test_failure_is_not_fatal(self):
        mock = MagicMock()
        mock.history.side_effect = RuntimeError("boom")
        assert _previous_close_from_history(mock) == (None, None)

    def test_cache_round_trips_the_pair(self):
        ticker = _ticker_with_history(1, 64.0, {"lastPrice": 66.59})
        cache = {}
        first = _cached_previous_close("IUSA.DE", ticker, cache)
        ticker.history.side_effect = AssertionError("should not refetch")
        assert _cached_previous_close("IUSA.DE", ticker, cache) == first


# ---------------------------------------------------------------------------
# FX in the daily change
# ---------------------------------------------------------------------------


class TestPreviousRate:
    def test_same_currency_needs_no_lookup(self, mocker):
        ticker = mocker.patch("stock.yf.Ticker")
        assert get_previous_rate("EUR", "EUR", {}) == 1.0
        ticker.assert_not_called()

    def test_direct_pair(self, mocker):
        mocker.patch(
            "stock._previous_close_from_history", return_value=(0.0911, 1)
        )
        assert get_previous_rate("SEK", "EUR", {}) == pytest.approx(0.0911)

    def test_inverse_pair_is_inverted(self, mocker):
        # SEKEUR=X missing, EURSEK=X present.
        results = iter([(None, None), (11.0, 1)])
        mocker.patch(
            "stock._previous_close_from_history", side_effect=lambda t: next(results)
        )
        assert get_previous_rate("SEK", "EUR", {}) == pytest.approx(1 / 11.0)

    def test_stale_fx_history_is_rejected(self, mocker):
        # Same reasoning as prices: a days-old rate is not yesterday's rate.
        mocker.patch("stock._previous_close_from_history", return_value=(0.0911, 9))
        assert get_previous_rate("SEK", "EUR", {}) is None

    def test_result_is_cached(self, mocker):
        history = mocker.patch(
            "stock._previous_close_from_history", return_value=(0.0911, 1)
        )
        cache = {}
        get_previous_rate("SEK", "EUR", cache)
        get_previous_rate("SEK", "EUR", cache)
        assert history.call_count == 1

    def test_failure_is_cached_too(self, mocker):
        # Otherwise every ticker in the currency retries the same dead lookup.
        history = mocker.patch(
            "stock._previous_close_from_history", return_value=(None, None)
        )
        cache = {}
        assert get_previous_rate("SEK", "EUR", cache) is None
        get_previous_rate("SEK", "EUR", cache)
        assert history.call_count == 2  # one per pair orientation, then cached

    def test_does_not_collide_with_current_rate_cache(self, mocker):
        mocker.patch("stock._previous_close_from_history", return_value=(0.0911, 1))
        cache = {"SEKEUR=X": 0.0906}
        assert get_previous_rate("SEK", "EUR", cache) == pytest.approx(0.0911)
        assert cache["SEKEUR=X"] == 0.0906


class TestDailyChangeIncludesFx:
    """A EUR holder of a SEK share gains or loses on the currency too; the
    daily change has to be the move they actually experienced."""

    def _ticker(self, mocker, price=110.0, prev=100.0):
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": price,
            "regularMarketPreviousClose": prev,
            "currency": "SEK",
        }
        mocker.patch("yfinance.Ticker", return_value=mock)
        return mock

    def test_currency_move_is_included(self, mocker):
        self._ticker(mocker)
        mocker.patch("stock.get_rate", return_value=0.10)
        mocker.patch("stock.get_previous_rate", return_value=0.11)
        result = get_ticker_summary("SVOL-B.ST", 1, "EUR", {})
        # 110 * 0.10 = 11.00 today against 100 * 0.11 = 11.00 yesterday: the
        # share rose 10% but the currency gave it all back.
        assert result["chg_pct"] == pytest.approx(0.0)

    def test_a_gain_can_become_a_loss(self, mocker):
        # The real case: LIFCO-B.ST rose 0.18% in SEK on a day SEK fell 0.55%
        # against EUR, so a EUR holder was down 0.39%.
        self._ticker(mocker, price=100.18, prev=100.0)
        mocker.patch("stock.get_rate", return_value=0.0906)
        mocker.patch("stock.get_previous_rate", return_value=0.0911)
        result = get_ticker_summary("LIFCO-B.ST", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx(-0.37, abs=0.05)

    def test_a_small_fx_move_does_not_flip_a_bigger_gain(self, mocker):
        self._ticker(mocker, price=101.0, prev=100.0)
        mocker.patch("stock.get_rate", return_value=0.0906)
        mocker.patch("stock.get_previous_rate", return_value=0.0911)
        result = get_ticker_summary("INVE-B.ST", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx(0.45, abs=0.05)

    def test_same_currency_is_unaffected(self, mocker):
        mock = MagicMock()
        mock.fast_info = {
            "lastPrice": 110.0,
            "regularMarketPreviousClose": 100.0,
            "currency": "EUR",
        }
        mocker.patch("yfinance.Ticker", return_value=mock)
        result = get_ticker_summary("MC.PA", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx(10.0)

    def test_missing_previous_rate_falls_back_to_today(self, mocker):
        # Degrades to the old FX-neutral behaviour rather than losing the row.
        self._ticker(mocker)
        mocker.patch("stock.get_rate", return_value=0.10)
        mocker.patch("stock.get_previous_rate", return_value=None)
        result = get_ticker_summary("SVOL-B.ST", 1, "EUR", {})
        assert result["chg_pct"] == pytest.approx(10.0)

    def test_previous_value_uses_the_previous_rate(self, mocker):
        self._ticker(mocker)
        mocker.patch("stock.get_rate", return_value=0.10)
        mocker.patch("stock.get_previous_rate", return_value=0.11)
        result = get_ticker_summary("SVOL-B.ST", 10, "EUR", {})
        assert result["val_prev"] == pytest.approx(100.0 * 0.11 * 10)
        assert result["val_now"] == pytest.approx(110.0 * 0.10 * 10)
