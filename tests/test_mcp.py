"""Tests for the MCP server.

The tools are exercised through a real MCP client speaking to the server
in-process, so the schemas, annotations and error envelopes are all covered —
not just the Python functions underneath them.
"""

import pytest

anyio = pytest.importorskip("anyio")
pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

import stock  # noqa: E402
import stock_mcp  # noqa: E402

CONFIG = """\
# My portfolio
holdings:
  AAPL: 10          # bought 2024
  SVOL-B.ST: 8367   # Swedish investment company
currency: EUR
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "stock.yaml"
    path.write_text(CONFIG)
    monkeypatch.setenv("STOCK_PRICE_CONFIG", str(path))
    return path


@pytest.fixture
def known_ticker(mocker):
    """Make every symbol resolve, so tests never touch the network."""
    return mocker.patch(
        "stock.resolve_symbol",
        side_effect=lambda s: {
            "symbol": str(s).strip().upper(),
            "status": "ok",
            "name": "Test Corp",
            "price": 100.0,
            "currency": "USD",
        },
    )


def call(tool, arguments=None):
    """Call one tool through a real client and return the CallToolResult."""

    async def _run():
        async with Client(stock_mcp.server) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(_run)


def tools():
    async def _run():
        async with Client(stock_mcp.server) as client:
            return (await client.list_tools()).tools

    return {t.name: t for t in anyio.run(_run)}


class TestToolRegistration:
    def test_expected_tools_exposed(self):
        assert set(tools()) == {
            "get_portfolio",
            "get_holding",
            "get_analyst_view",
            "list_holdings",
            "set_holding",
            "add_shares",
            "remove_holding",
        }

    @pytest.mark.parametrize(
        "name", ["get_portfolio", "get_holding", "get_analyst_view", "list_holdings"]
    )
    def test_read_tools_are_marked_read_only(self, name):
        assert tools()[name].annotations.read_only_hint is True

    @pytest.mark.parametrize("name", ["set_holding", "add_shares", "remove_holding"])
    def test_write_tools_are_not_marked_read_only(self, name):
        assert tools()[name].annotations.read_only_hint is False

    def test_only_removal_is_marked_destructive(self):
        registered = tools()
        assert registered["remove_holding"].annotations.destructive_hint is True
        assert registered["set_holding"].annotations.destructive_hint is False
        assert registered["add_shares"].annotations.destructive_hint is False

    def test_tools_declare_an_output_schema(self):
        # Without one the client gets no structured content back, only text.
        for name, tool in tools().items():
            assert tool.output_schema is not None, name

    def test_descriptions_say_no_trade_is_placed(self):
        # The write tools are bookkeeping; a model reading these must not come
        # away thinking it can trade.
        registered = tools()
        for name in ("set_holding", "add_shares", "remove_holding"):
            description = " ".join(registered[name].description.split())
            assert "does NOT place an order or execute a trade" in description


class TestListHoldings:
    def test_reads_the_config(self, config_file):
        result = call("list_holdings")
        assert result.structured_content["holdings"] == {
            "AAPL": 10,
            "SVOL-B.ST": 8367,
        }
        assert result.structured_content["currency"] == "EUR"
        assert result.structured_content["config_path"] == str(config_file)


class TestSetHolding:
    def test_updates_an_existing_position(self, config_file, known_ticker):
        result = call("set_holding", {"symbol": "AAPL", "quantity": 25})
        assert result.is_error is False
        assert result.structured_content["action"] == "updated"
        assert result.structured_content["quantity"] == 25
        assert stock.load_config(str(config_file))["holdings"]["AAPL"] == 25

    def test_adds_a_new_position(self, config_file, known_ticker):
        result = call("set_holding", {"symbol": "mc.pa", "quantity": 45})
        assert result.structured_content["symbol"] == "MC.PA"
        assert result.structured_content["action"] == "added"
        assert stock.load_config(str(config_file))["holdings"]["MC.PA"] == 45

    def test_reports_the_resolved_instrument(self, config_file, known_ticker):
        result = call("set_holding", {"symbol": "AAPL", "quantity": 5})
        assert result.structured_content["instrument"]["name"] == "Test Corp"
        assert "No trade was placed" in result.structured_content["note"]

    def test_unknown_ticker_is_refused_and_nothing_written(self, config_file, mocker):
        mocker.patch(
            "stock.resolve_symbol",
            return_value={"symbol": "NOPE", "status": "unknown", "name": None,
                          "price": None, "currency": None},
        )
        result = call("set_holding", {"symbol": "NOPE", "quantity": 5})
        assert result.is_error is True
        assert "does not resolve" in result.content[0].text
        assert config_file.read_text() == CONFIG

    def test_unverified_ticker_is_written_but_flagged(self, config_file, mocker):
        # A rate limit must not block a legitimate edit — but say so.
        mocker.patch(
            "stock.resolve_symbol",
            return_value={"symbol": "MC.PA", "status": "unverified", "name": None,
                          "price": None, "currency": None},
        )
        result = call("set_holding", {"symbol": "MC.PA", "quantity": 45})
        assert result.is_error is False
        assert "Could not reach" in result.structured_content["warning"]
        assert stock.load_config(str(config_file))["holdings"]["MC.PA"] == 45

    def test_negative_quantity_is_refused(self, config_file, known_ticker):
        result = call("set_holding", {"symbol": "AAPL", "quantity": -3})
        assert result.is_error is True
        assert config_file.read_text() == CONFIG


class TestAddShares:
    def test_bought_more(self, config_file, known_ticker):
        result = call("add_shares", {"symbol": "AAPL", "quantity": 5})
        assert result.structured_content["previous_quantity"] == 10
        assert result.structured_content["quantity"] == 15
        assert stock.load_config(str(config_file))["holdings"]["AAPL"] == 15

    def test_partial_sale(self, config_file, known_ticker):
        result = call("add_shares", {"symbol": "AAPL", "quantity": -4})
        assert result.structured_content["quantity"] == 6

    def test_selling_out_removes_the_holding(self, config_file, known_ticker):
        result = call("add_shares", {"symbol": "AAPL", "quantity": -10})
        assert result.structured_content["action"] == "removed"
        assert "AAPL" not in stock.load_config(str(config_file))["holdings"]

    def test_overselling_is_refused(self, config_file, known_ticker):
        result = call("add_shares", {"symbol": "AAPL", "quantity": -11})
        assert result.is_error is True
        assert config_file.read_text() == CONFIG


class TestRemoveHolding:
    def test_removes(self, config_file):
        result = call("remove_holding", {"symbol": "AAPL"})
        assert result.structured_content["previous_quantity"] == 10
        assert "No trade was placed" in result.structured_content["note"]
        assert "AAPL" not in stock.load_config(str(config_file))["holdings"]

    def test_unknown_holding_is_refused(self, config_file):
        result = call("remove_holding", {"symbol": "MSFT"})
        assert result.is_error is True
        assert config_file.read_text() == CONFIG


@pytest.mark.skipif(
    not stock.PRESERVES_COMMENTS, reason="ruamel.yaml not installed"
)
def test_edits_preserve_config_comments(config_file, known_ticker):
    call("add_shares", {"symbol": "AAPL", "quantity": 5})
    text = config_file.read_text()
    assert "# My portfolio" in text
    assert "# bought 2024" in text
    assert "# Swedish investment company" in text


class TestReadTools:
    @pytest.fixture
    def snapshot(self, mocker):
        return mocker.patch(
            "stock.collect_portfolio",
            return_value={
                "currency": "EUR",
                "generated_at": "2026-08-02T12:00:00+02:00",
                "positions": [
                    {"symbol": "AAPL", "status": "ok", "value": 1500.0,
                     "analysts": {"consensus": "Buy"}},
                    {"symbol": "SVOL-B.ST", "status": "ok", "value": 900.0,
                     "analysts": None},
                ],
            },
        )

    def test_get_portfolio(self, config_file, snapshot):
        result = call("get_portfolio")
        assert len(result.structured_content["positions"]) == 2
        assert snapshot.call_args.kwargs["cache_ttl"] == 60

    def test_refresh_bypasses_the_cache(self, config_file, snapshot):
        call("get_portfolio", {"refresh": True})
        assert snapshot.call_args.kwargs["cache_ttl"] == 0

    def test_currency_override(self, config_file, snapshot):
        call("get_portfolio", {"currency": "usd"})
        assert snapshot.call_args[0][1] == "USD"

    def test_get_holding(self, config_file, snapshot):
        result = call("get_holding", {"symbol": "aapl"})
        assert result.structured_content["symbol"] == "AAPL"
        assert result.structured_content["value"] == 1500.0
        assert result.structured_content["currency"] == "EUR"

    def test_get_holding_unknown_lists_what_is_held(self, config_file, snapshot):
        result = call("get_holding", {"symbol": "MSFT"})
        assert result.is_error is True
        assert "AAPL" in result.content[0].text

    def test_get_analyst_view(self, config_file, mocker):
        mocker.patch(
            "stock.analyst_view",
            return_value={
                "instrument": {"symbol": "AAPL", "name": "Apple Inc."},
                "analysts": {"consensus": "Buy", "analyst_count": 46},
            },
        )
        result = call("get_analyst_view", {"symbol": "AAPL"})
        assert result.structured_content["analysts"]["consensus"] == "Buy"

    def test_get_analyst_view_unknown_symbol(self, config_file, mocker):
        mocker.patch("stock.analyst_view", return_value=None)
        result = call("get_analyst_view", {"symbol": "NOPE"})
        assert result.is_error is True
        assert "does not resolve" in result.content[0].text
