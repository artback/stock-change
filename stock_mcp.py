"""MCP server exposing the portfolio to an AI assistant.

Read tools answer questions about what you hold and how it is doing; write
tools maintain the holdings in your ``~/.stock_price.yaml``. The write tools
are *bookkeeping only* — they record positions you already own. Nothing here
places an order, moves money, or talks to a broker.

Run it with ``stock-price-mcp`` (installed by the ``mcp`` extra).
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import stock

server = MCPServer(
    name="stock-price",
    version=stock.__version__,
    instructions=(
        "Tracks the user's stock portfolio: live values, daily and monthly "
        "performance, analyst consensus, dividends and news.\n\n"
        "The write tools only edit the user's local holdings file — they "
        "record what the user already owns. They do not place trades, and "
        "nothing here should be presented to the user as buying or selling. "
        "Confirm the ticker and share count with the user before writing.\n\n"
        "Analyst ratings are third-party opinions reported as-is; do not "
        "present them as a recommendation to act."
    ),
)

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_DELETE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


def _snapshot(currency: str | None = None, cache_ttl: int = 60) -> dict[str, Any]:
    config = stock.load_config()
    target = (currency or config["currency"]).upper()
    return stock.collect_portfolio(
        config["holdings"],
        target,
        cache_ttl=cache_ttl,
        cost_basis=config.get("cost_basis"),
        benchmark=config.get("benchmark"),
    )


@server.tool(annotations=_READ)
def get_portfolio(currency: str | None = None, refresh: bool = False) -> dict[str, Any]:
    """Get the user's whole portfolio: per-position value, daily and monthly
    change, analyst consensus, upcoming dividends, recent news and the 30-day
    history.

    Args:
        currency: Report in this currency (e.g. "USD"). Defaults to the
            currency configured by the user.
        refresh: Force a live fetch instead of reusing a snapshot from the
            last minute.
    """
    return _snapshot(currency, cache_ttl=0 if refresh else 60)


@server.tool(annotations=_READ)
def get_holding(symbol: str, currency: str | None = None) -> dict[str, Any]:
    """Get one position: its value, daily and monthly change, and analyst view.

    Args:
        symbol: Ticker as it appears in the portfolio, e.g. "AAPL" or "MC.PA".
        currency: Report in this currency. Defaults to the user's configured one.
    """
    payload = _snapshot(currency)
    wanted = symbol.strip().upper()
    for position in payload["positions"]:
        if position["symbol"].upper() == wanted:
            return {
                "currency": payload["currency"],
                "generated_at": payload["generated_at"],
                **position,
            }
    held = [p["symbol"] for p in payload["positions"]]
    raise ValueError(f"{wanted} is not in the portfolio. Holdings: {', '.join(held)}")


@server.tool(annotations=_READ)
def get_analyst_view(symbol: str) -> dict[str, Any]:
    """Get analyst ratings and price targets for any ticker, held or not.

    Returns the consensus rating, the spread of analysts across strong
    buy/buy/hold/sell/strong sell, how the consensus moved over the last month,
    and the low/mean/median/high price targets.

    These are third-party opinions reported as-is, not a recommendation.
    Coverage is often absent for index funds and small caps, in which case
    "analysts" is null.

    Args:
        symbol: Ticker to look up, e.g. "AAPL".
    """
    view = stock.analyst_view(symbol)
    if view is None:
        raise ValueError(f"'{symbol}' does not resolve to a known ticker")
    return view


@server.tool(annotations=_READ)
def list_holdings() -> dict[str, Any]:
    """List the tickers, share counts and recorded cost basis currently
    configured, and where that configuration lives on disk. Reads the file
    only — no prices are fetched."""
    document, path = stock.read_config_document()
    quantities, cost_basis = stock.parse_holdings(document["holdings"])
    return {
        "holdings": {str(k): v for k, v in quantities.items()},
        "cost_basis": {str(k): v for k, v in cost_basis.items()},
        "currency": document.get("currency", "EUR"),
        "benchmark": document.get("benchmark"),
        "config_path": str(path),
    }


@server.tool(annotations=_WRITE)
def set_holding(
    symbol: str, quantity: float, cost: float | None = None
) -> dict[str, Any]:
    """Record that the user holds exactly this many shares of a ticker, adding
    it to the portfolio if it is new.

    This is bookkeeping — it edits the user's local holdings file and does NOT
    place an order or execute a trade. Confirm the ticker and share count with
    the user first.

    Use this for "I hold 15 in total". For "I bought 5 more", use add_shares.

    Args:
        symbol: Ticker, e.g. "AAPL" or "MC.PA" (Yahoo Finance format, so
            non-US listings carry an exchange suffix).
        quantity: Total shares now held. Must be positive; fractional is fine.
        cost: Average price paid per share, in the ticker's own currency.
            Recording it is what lets the portfolio report profit and loss.
            Omit to leave any existing cost basis untouched.
    """
    return _write_holding(symbol, lambda s: stock.set_holding(s, quantity, cost=cost))


@server.tool(annotations=_WRITE)
def add_shares(
    symbol: str, quantity: float, cost: float | None = None
) -> dict[str, Any]:
    """Add shares to a holding — the user bought more — or subtract with a
    negative quantity after a partial sale. Adds the ticker if it is new.

    This is bookkeeping — it edits the user's local holdings file and does NOT
    place an order or execute a trade. Confirm the ticker and share count with
    the user first.

    The result reports both the previous and the new quantity, so read it back
    to the user to confirm the change landed as they meant it.

    Args:
        symbol: Ticker, e.g. "AAPL".
        quantity: Shares to add; negative to subtract. Reaching exactly zero
            removes the holding.
        cost: Price paid per share on this purchase, in the ticker's own
            currency. Recording it is what lets the portfolio report profit
            and loss. Omit to leave any existing cost basis untouched.
    """
    return _write_holding(symbol, lambda s: stock.add_shares(s, quantity, cost=cost))


@server.tool(annotations=_DELETE)
def remove_holding(symbol: str) -> dict[str, Any]:
    """Remove a ticker from the portfolio entirely — the user sold out of it.

    This is bookkeeping — it edits the user's local holdings file and does NOT
    place an order or execute a trade. The previous share count is returned,
    and the file's prior contents are kept alongside it as a .bak.

    Args:
        symbol: Ticker to drop, e.g. "AAPL".
    """
    result = stock.remove_holding(symbol)
    result["note"] = "Removed from the tracked portfolio. No trade was placed."
    return result


def _write_holding(symbol: str, apply) -> dict[str, Any]:
    """Validate the ticker, apply the edit, and describe what changed.

    An unrecognised ticker is refused rather than written: a typo would sit in
    the config silently breaking the portfolio total. A ticker we merely could
    not *reach* (rate limit, no network) is still written, but flagged.
    """
    instrument = stock.resolve_symbol(symbol)
    if instrument["status"] == "unknown":
        raise ValueError(
            f"'{symbol}' does not resolve to a known ticker. Non-US listings "
            f"need their Yahoo Finance exchange suffix — e.g. MC.PA, SVOL-B.ST."
        )

    result = apply(instrument["symbol"])
    result["instrument"] = instrument
    result["note"] = "Holdings file updated. No trade was placed."
    if instrument["status"] == "unverified":
        result["warning"] = (
            "Could not reach the price service to verify this ticker, so it was "
            "saved unchecked. Confirm it appears correctly in the portfolio."
        )
    if not result.get("comments_preserved", True):
        result["warning_comments"] = (
            "Comments and key order in the config file were not preserved "
            "(install ruamel.yaml to keep them)."
        )
    return result


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
