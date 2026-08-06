"""Second price source, for when the primary feed stops.

Yahoo's finance endpoints are undocumented and unsupported, and they do stall:
on 2026-08-06 every European exchange froze at ~09:25 CEST for hours while
other providers kept publishing. One source with no alternative means the tool
silently shows hours-old prices whenever that happens.

Two providers, deliberately different in kind:

* **Twelve Data** for quotes. Needs a free API key, supplied through the
  environment — never the config file, so a config can be shared or committed
  without leaking it.
* **Frankfurter** for exchange rates. Published by the European Central Bank,
  no key at all, so FX cover works out of the box.

Everything here returns empty rather than raising. A fallback that breaks the
run is worse than the outage it exists to paper over.
"""

import concurrent.futures
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TWELVEDATA_QUOTE = "https://api.twelvedata.com/quote"
FRANKFURTER_LATEST = "https://api.frankfurter.dev/v1/latest"

API_KEY_ENV = "TWELVEDATA_API_KEY"

# Yahoo writes the venue as a ticker suffix; Twelve Data wants the MIC code.
# Only exchanges the tool already models are listed — an unmapped suffix is
# passed through untouched rather than guessed at.
YAHOO_SUFFIX_TO_MIC = {
    ".PA": "XPAR",
    ".ST": "XSTO",
    ".DE": "XETR",
    ".AS": "XAMS",
    ".BR": "XBRU",
    ".LS": "XLIS",
    ".MI": "XMIL",
    ".MC": "XMAD",
    ".SW": "XSWX",
    ".VI": "XWBO",
    ".L": "XLON",
    ".CO": "XCSE",
    ".HE": "XHEL",
    ".OL": "XOSL",
    ".TO": "XTSE",
    ".T": "XTKS",
    ".HK": "XHKG",
    ".AX": "XASX",
}

# The free tier allows eight requests a minute, which a portfolio of eight
# holdings would consume exactly. Quotes are only fetched when the primary
# feed has already stalled, so a small pool keeps a burst inside the limit.
MAX_CONCURRENCY = 4
TIMEOUT = 10


def api_key():
    """The Twelve Data key, or None if the user hasn't configured one."""
    return (os.environ.get(API_KEY_ENV) or "").strip() or None


def to_provider_symbol(yahoo_symbol):
    """Split a Yahoo ticker into ``(symbol, mic_code)``.

    ``MC.PA`` becomes ``("MC", "XPAR")``. A US ticker has no suffix and needs
    no MIC. An unrecognised suffix is left alone: passing it through may fail,
    but inventing a venue would fail *silently and wrongly*.
    """
    symbol = str(yahoo_symbol).strip().upper()
    dot = symbol.rfind(".")
    if dot <= 0:
        return symbol, None
    suffix = symbol[dot:]
    mic = YAHOO_SUFFIX_TO_MIC.get(suffix)
    if mic is None:
        return symbol, None
    return symbol[:dot], mic


def _get_json(url, params, timeout=TIMEOUT):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as response:
        return json.loads(response.read().decode())


def fetch_quote(yahoo_symbol, key=None, timeout=TIMEOUT):
    """One quote from Twelve Data, or None.

    Returns the same shape the primary path produces: last price, the previous
    session's close and the currency the instrument trades in.
    """
    key = key or api_key()
    if not key:
        return None
    symbol, mic = to_provider_symbol(yahoo_symbol)
    try:
        payload = _get_json(
            TWELVEDATA_QUOTE,
            {"symbol": symbol, "mic_code": mic, "apikey": key},
            timeout=timeout,
        )
    except Exception:
        return None

    # Errors arrive as a normal 200 with a status field.
    if not isinstance(payload, dict) or payload.get("status") == "error":
        return None
    try:
        price = float(payload["close"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        previous_close = float(payload.get("previous_close"))
    except (TypeError, ValueError):
        previous_close = None

    return {
        "symbol": str(yahoo_symbol).strip().upper(),
        "price": price,
        "previous_close": previous_close,
        "currency": payload.get("currency"),
        "provider": "twelvedata",
    }


def fetch_quotes(yahoo_symbols, key=None, timeout=TIMEOUT):
    """Quotes for several tickers, keyed by their Yahoo symbol.

    Symbols the provider cannot serve are simply absent, so the caller keeps
    whatever the primary feed gave it for those.
    """
    symbols = list(yahoo_symbols)
    key = key or api_key()
    if not symbols or not key:
        return {}
    quotes = {}
    workers = min(MAX_CONCURRENCY, len(symbols))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_quote, s, key, timeout): s for s in symbols
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                quote = future.result()
            except Exception:
                quote = None
            if quote:
                quotes[futures[future]] = quote
    return quotes


def fetch_rate(source, target, timeout=TIMEOUT):
    """Exchange rate from Frankfurter (ECB reference rates), or None.

    Keyless, so this half of the fallback works without any setup. Note the
    ECB publishes once per working day: good enough to keep a portfolio total
    honest during an outage, not a live rate.
    """
    source = str(source).upper()
    target = str(target).upper()
    if source == target:
        return 1.0
    try:
        payload = _get_json(
            FRANKFURTER_LATEST, {"base": source, "symbols": target}, timeout=timeout
        )
        return float(payload["rates"][target])
    except Exception:
        return None
