from datetime import date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from rich.console import Console

from stock import (
    CURRENCY_SYMBOLS,
    DEFAULT_HOLDINGS,
    DEFAULT_SCHEDULE,
    EXCHANGE_SCHEDULES,
    KNOWN_CURRENCIES,
    _get_exchange_suffix,
    _has_market_activity,
    build_display_group,
    fetch_history,
    get_dividend_data,
    get_news_data,
    get_rate,
    get_ticker_summary,
    is_any_market_open,
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
        totals, changes, traded = fetch_history(holdings, "USD", ticker_to_currency)

        assert len(totals) == 3
        assert totals[0] == pytest.approx(1000.0)
        assert totals[-1] == pytest.approx(1100.0)
        assert "AAPL" in changes
        assert changes["AAPL"] == pytest.approx(10.0)

    def test_empty_download(self, mocker):
        mocker.patch("yfinance.download", return_value=pd.DataFrame())
        totals, changes, traded = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
        assert totals == []
        assert changes == {}

    def test_exception_returns_empty(self, mocker):
        mocker.patch("yfinance.download", side_effect=Exception("network"))
        totals, changes, traded = fetch_history({"AAPL": 10}, "USD", {"AAPL": "USD"})
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
        totals, changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
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
        totals, changes, traded = fetch_history(holdings, "USD", ticker_to_currency)
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
        totals, changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
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
        totals, changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
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
        totals, changes, traded = fetch_history(holdings, "USD", ticker_to_currency)
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
        totals, changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
        assert "7203.T" in traded
        assert "SVOL-B.ST" not in traded

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
        totals, changes, traded = fetch_history(holdings, "EUR", ticker_to_currency)
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
        assert DEFAULT_SCHEDULE == ("America/New_York", 9, 16)


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
