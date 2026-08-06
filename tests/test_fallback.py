"""Tests for the backup price provider.

No network: ``_get_json`` is the single seam every provider call goes through,
so patching it covers the whole module.
"""

import pytest

import stock_fallback


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    """Never let a developer's real key change what these tests exercise."""
    monkeypatch.delenv(stock_fallback.API_KEY_ENV, raising=False)


QUOTE = {
    "symbol": "MC", "close": "485.60", "previous_close": "486.90",
    "currency": "EUR", "is_market_open": True,
}


class TestSymbolMapping:
    @pytest.mark.parametrize(
        "yahoo,expected",
        [
            ("MC.PA", ("MC", "XPAR")),
            ("INVE-B.ST", ("INVE-B", "XSTO")),
            ("IUSA.DE", ("IUSA", "XETR")),
            ("ASML.AS", ("ASML", "XAMS")),
            ("AAPL", ("AAPL", None)),
            ("mc.pa", ("MC", "XPAR")),
        ],
    )
    def test_venue_suffix_becomes_a_mic(self, yahoo, expected):
        assert stock_fallback.to_provider_symbol(yahoo) == expected

    def test_unknown_suffix_is_passed_through_untouched(self):
        # Guessing a venue would fail silently and wrongly; passing it through
        # at least fails visibly.
        assert stock_fallback.to_provider_symbol("FOO.ZZ") == ("FOO.ZZ", None)


class TestFetchQuote:
    def test_parses_a_quote(self, mocker):
        mocker.patch("stock_fallback._get_json", return_value=QUOTE)
        quote = stock_fallback.fetch_quote("MC.PA", key="k")
        assert quote["symbol"] == "MC.PA"
        assert quote["price"] == pytest.approx(485.60)
        assert quote["previous_close"] == pytest.approx(486.90)
        assert quote["currency"] == "EUR"
        assert quote["provider"] == "twelvedata"

    def test_sends_the_mapped_symbol_and_venue(self, mocker):
        get = mocker.patch("stock_fallback._get_json", return_value=QUOTE)
        stock_fallback.fetch_quote("MC.PA", key="k")
        params = get.call_args[0][1]
        assert params["symbol"] == "MC"
        assert params["mic_code"] == "XPAR"
        assert params["apikey"] == "k"

    def test_no_key_means_no_request(self, mocker):
        get = mocker.patch("stock_fallback._get_json")
        assert stock_fallback.fetch_quote("MC.PA") is None
        get.assert_not_called()

    def test_provider_error_payload(self, mocker):
        # Twelve Data reports errors as a 200 with a status field.
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"code": 401, "status": "error", "message": "bad key"},
        )
        assert stock_fallback.fetch_quote("MC.PA", key="k") is None

    def test_error_payload_carrying_a_price_is_still_refused(self, mocker):
        # A rate-limit reply with a stale price attached must not be mistaken
        # for a quote; the missing-price check alone would let this through.
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"status": "error", "code": 429, "close": "1.00",
                          "currency": "EUR"},
        )
        assert stock_fallback.fetch_quote("MC.PA", key="k") is None

    def test_network_failure_is_not_fatal(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=OSError("down"))
        assert stock_fallback.fetch_quote("MC.PA", key="k") is None

    def test_missing_price(self, mocker):
        mocker.patch("stock_fallback._get_json", return_value={"currency": "EUR"})
        assert stock_fallback.fetch_quote("MC.PA", key="k") is None

    def test_missing_previous_close_is_tolerated(self, mocker):
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"close": "10.0", "currency": "EUR"},
        )
        quote = stock_fallback.fetch_quote("MC.PA", key="k")
        assert quote["price"] == pytest.approx(10.0)
        assert quote["previous_close"] is None


class TestFetchQuotes:
    def test_one_request_per_venue_not_per_holding(self, mocker):
        # Eight concurrent requests exhaust a free tier's whole minute budget
        # and self-429 before any of them lands.
        get = mocker.patch(
            "stock_fallback._get_json",
            return_value={"MC": QUOTE, "OR": dict(QUOTE, symbol="OR")},
        )
        stock_fallback.fetch_quotes(
            ["MC.PA", "OR.PA", "INVE-B.ST", "IUSA.DE"], key="k"
        )
        assert get.call_count == 3  # XPAR, XSTO, XETR

    def test_batched_symbols_share_one_request(self, mocker):
        get = mocker.patch(
            "stock_fallback._get_json",
            return_value={"MC": QUOTE, "OR": dict(QUOTE, symbol="OR")},
        )
        stock_fallback.fetch_quotes(["MC.PA", "OR.PA"], key="k")
        assert get.call_args[0][1]["symbol"] == "MC,OR"

    def test_keyed_by_the_yahoo_symbol(self, mocker):
        mocker.patch("stock_fallback._get_json", return_value=QUOTE)
        quotes = stock_fallback.fetch_quotes(["MC.PA"], key="k")
        assert set(quotes) == {"MC.PA"}

    def test_unservable_symbols_are_simply_absent(self, mocker):
        # The caller keeps the primary feed's value for those.
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"MC": QUOTE, "NOPE": {"status": "error"}},
        )
        quotes = stock_fallback.fetch_quotes(["MC.PA", "NOPE.PA"], key="k")
        assert set(quotes) == {"MC.PA"}

    def test_no_key_short_circuits(self, mocker):
        get = mocker.patch("stock_fallback._get_json")
        assert stock_fallback.fetch_quotes(["MC.PA"]) == {}
        get.assert_not_called()

    def test_empty_input(self):
        assert stock_fallback.fetch_quotes([], key="k") == {}

    def test_one_venue_failing_does_not_lose_another(self, mocker):
        def fake(url, params, timeout=None):
            if params["mic_code"] == "XSTO":
                raise OSError("down")
            return QUOTE

        mocker.patch("stock_fallback._get_json", side_effect=fake)
        quotes = stock_fallback.fetch_quotes(["MC.PA", "INVE-B.ST"], key="k")
        assert set(quotes) == {"MC.PA"}


class TestFailureReasons:
    """A bare "fail" sent me chasing a venue-mapping bug that did not exist;
    the real cause was the plan not covering the exchange."""

    def _error(self, code):
        import urllib.error

        return urllib.error.HTTPError("u", code, "m", {}, None)

    def test_rate_limiting_is_named(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=self._error(429))
        errors = {}
        stock_fallback.fetch_quotes(["MC.PA"], key="k", errors=errors)
        assert "rate limited" in errors["MC.PA"]

    def test_missing_coverage_is_named(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=self._error(404))
        errors = {}
        stock_fallback.fetch_quotes(["MC.PA"], key="k", errors=errors)
        assert "not covered by this plan" in errors["MC.PA"]

    def test_bad_key_is_named(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=self._error(401))
        errors = {}
        stock_fallback.fetch_quotes(["MC.PA"], key="k", errors=errors)
        assert "API key" in errors["MC.PA"]

    def test_provider_message_is_surfaced(self, mocker):
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"status": "error", "message": "symbol not found"},
        )
        errors = {}
        stock_fallback.fetch_quotes(["MC.PA"], key="k", errors=errors)
        assert "symbol not found" in errors["MC.PA"]

    def test_errors_are_optional(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=self._error(429))
        assert stock_fallback.fetch_quotes(["MC.PA"], key="k") == {}


class TestFetchRate:
    def test_parses_a_rate(self, mocker):
        mocker.patch(
            "stock_fallback._get_json",
            return_value={"base": "SEK", "rates": {"EUR": 0.09121}},
        )
        assert stock_fallback.fetch_rate("SEK", "EUR") == pytest.approx(0.09121)

    def test_same_currency_needs_no_request(self, mocker):
        get = mocker.patch("stock_fallback._get_json")
        assert stock_fallback.fetch_rate("EUR", "EUR") == 1.0
        get.assert_not_called()

    def test_needs_no_api_key(self, mocker):
        # Frankfurter is keyless, so FX cover works with no setup at all.
        mocker.patch(
            "stock_fallback._get_json", return_value={"rates": {"EUR": 0.09}}
        )
        assert stock_fallback.fetch_rate("SEK", "EUR") is not None

    def test_failure_is_not_fatal(self, mocker):
        mocker.patch("stock_fallback._get_json", side_effect=OSError("down"))
        assert stock_fallback.fetch_rate("SEK", "EUR") is None

    def test_unexpected_payload(self, mocker):
        mocker.patch("stock_fallback._get_json", return_value={"rates": {}})
        assert stock_fallback.fetch_rate("SEK", "EUR") is None


class TestApiKey:
    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv(stock_fallback.API_KEY_ENV, "  abc  ")
        assert stock_fallback.api_key() == "abc"

    def test_blank_is_treated_as_absent(self, monkeypatch):
        monkeypatch.setenv(stock_fallback.API_KEY_ENV, "   ")
        assert stock_fallback.api_key() is None

    def test_unset(self):
        assert stock_fallback.api_key() is None


class TestRequestHeaders:
    """Frankfurter answers urllib's default agent with a 403, which only
    showed up once the code ran somewhere other than a laptop."""

    def test_requests_identify_the_client(self, mocker):
        opened = {}

        class FakeResponse:
            def read(self):
                return b'{"rates": {"EUR": 0.09}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            opened["headers"] = request.headers
            return FakeResponse()

        mocker.patch("stock_fallback.urllib.request.urlopen", fake_urlopen)
        stock_fallback.fetch_rate("SEK", "EUR")
        headers = {k.lower(): v for k, v in opened["headers"].items()}
        assert "stock-price" in headers["User-agent".lower()]

    def test_query_omits_unset_parameters(self, mocker):
        # A None mic_code must not become the literal string "None".
        captured = {}

        class FakeResponse:
            def read(self):
                return b'{"close": "1.0", "currency": "USD"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return FakeResponse()

        mocker.patch("stock_fallback.urllib.request.urlopen", fake_urlopen)
        stock_fallback.fetch_quote("AAPL", key="k")
        assert "mic_code" not in captured["url"]
