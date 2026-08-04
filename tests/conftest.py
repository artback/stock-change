"""Keep the suite out of the real home directory.

The portfolio history and price cache live under ``$HOME``. A test that wrote
there once destroyed a real recorded history — and because the currency
differed, ``record_portfolio_value`` reset the series rather than merging it.
Redirecting both paths for every test makes that structurally impossible.
"""

import pytest

import stock


@pytest.fixture(autouse=True)
def _isolate_user_files(tmp_path, monkeypatch):
    monkeypatch.setattr(stock, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(stock, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(stock, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    return tmp_path
