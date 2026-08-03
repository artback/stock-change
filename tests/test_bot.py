"""Tests for the Telegram bot.

Telegram itself is never contacted: ``_request`` is the single seam through
which every API call passes, so patching it covers the whole surface.
"""

import html
import re
import time

import pytest

import stock_bot


def _plain(text):
    """Strip the HTML the bot sends so assertions read naturally."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


@pytest.fixture(autouse=True)
def _clear_cache():
    """The bot caches between commands; tests must not inherit each other's."""
    stock_bot._cache.clear()
    yield
    stock_bot._cache.clear()


@pytest.fixture
def sent(mocker):
    """Capture outgoing messages instead of calling Telegram."""
    calls = []

    def fake(token, method, params=None, timeout=None):
        calls.append((method, params or {}))
        return []

    mocker.patch("stock_bot._request", side_effect=fake)
    return calls


@pytest.fixture
def portfolio(mocker):
    """A small deterministic portfolio, so no network and no live prices."""
    mocker.patch(
        "stock.load_config",
        return_value={
            "holdings": {"MC.PA": 45, "IUSA.DE": 720},
            "currency": "EUR",
            "cost_basis": {"MC.PA": 400.0},
            "benchmark": None,
        },
    )
    summaries = {
        "MC.PA": {
            "symbol": "MC.PA", "qty": 45, "val_now": 21381.75, "val_prev": 21000.0,
            "chg_pct": 1.82, "daily_chg_val": 381.75, "source_currency": "EUR",
            "conv": 1.0, "price": 475.15, "cost": 400.0, "cost_value": 18000.0,
            "unrealized": 3381.75, "return_pct": 18.79,
        },
        "IUSA.DE": {
            "symbol": "IUSA.DE", "qty": 720, "val_now": 46340.64, "val_prev": 46000.0,
            "chg_pct": 0.74, "daily_chg_val": 340.64, "source_currency": "EUR",
            "conv": 1.0, "price": 64.36,
        },
    }
    mocker.patch(
        "stock.fetch_summaries",
        return_value=(summaries, {"MC.PA": "EUR", "IUSA.DE": "EUR"}, []),
    )
    mocker.patch(
        "stock.fetch_auxiliary",
        return_value={
            "history": [100.0, 110.0], "monthly": {"MC.PA": -2.23}, "traded": None,
            "dividends": {
                "MC.PA": {
                    "symbol": "MC.PA", "ex_date": "2026-12-01", "amt": 5.5,
                    "total_p": 247.5, "cur_label": "€", "ttm_per_share": 13.0,
                    "ttm_total": 585.0, "yield_pct": 2.74,
                    "yield_on_cost_pct": 3.25,
                }
            },
            "news": [
                {"symbol": "MC.PA", "title": "LVMH does something",
                 "link": "https://example.com/a", "provider": "Reuters",
                 "pub_date": "2026-08-03 10:00", "summary": "A summary."}
            ],
            "analysts": {
                "MC.PA": {
                    "consensus": "Buy", "trend": "steady", "analyst_count": 25,
                    "price_target": {"upside_pct": 19.8},
                }
            },
        },
    )
    mocker.patch("stock.record_portfolio_value", return_value=None)
    mocker.patch("stock.load_portfolio_history", return_value=[])


class TestAccessControl:
    """The bot answers a private portfolio; the allowlist is the whole defence."""

    def _update(self, chat_id, text="/portfolio"):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_allowed_chat_is_answered(self, sent, portfolio):
        assert (
            stock_bot.process_update(self._update(42), "tok", {"42"}) is True
        )
        assert sent[0][0] == "sendMessage"

    def test_unknown_chat_gets_nothing(self, sent, portfolio):
        assert stock_bot.process_update(self._update(999), "tok", {"42"}) is False
        assert sent == []

    def test_no_reply_leaks_data_to_a_stranger(self, sent, portfolio):
        stock_bot.process_update(self._update(999), "tok", {"42"})
        assert not any("MC.PA" in str(params) for _, params in sent)

    def test_missing_token_refuses_to_start(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
        with pytest.raises(SystemExit, match="TELEGRAM_TOKEN"):
            stock_bot.main()

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("42", {"42"}),
            ("42,-100", {"42", "-100"}),
            (" 42 , -100 ", {"42", "-100"}),
            ("", set()),
            (None, set()),
        ],
    )
    def test_allowlist_parsing(self, raw, expected):
        assert stock_bot._allowed_chat_ids(raw) == expected


class TestUpdateHandling:
    def test_non_message_updates_ignored(self, sent):
        assert stock_bot.process_update({"update_id": 1}, "tok", {"42"}) is False

    def test_plain_text_is_not_a_command(self, sent, portfolio):
        update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "hello"}}
        assert stock_bot.process_update(update, "tok", {"42"}) is False

    def test_edited_messages_are_handled(self, sent, portfolio):
        update = {
            "update_id": 1,
            "edited_message": {"chat": {"id": 42}, "text": "/help"},
        }
        assert stock_bot.process_update(update, "tok", {"42"}) is True

    def test_a_failing_command_replies_instead_of_crashing(self, sent, mocker):
        mocker.patch("stock_bot.handle_command", side_effect=RuntimeError("boom"))
        update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/portfolio"}}
        assert stock_bot.process_update(update, "tok", {"42"}) is True
        assert "went wrong" in sent[0][1]["text"]

    def test_unknown_command_is_silent(self, sent, portfolio):
        update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/nope"}}
        assert stock_bot.process_update(update, "tok", {"42"}) is False


class TestCommands:
    def test_help(self, portfolio):
        assert "/portfolio" in stock_bot.handle_command("/help")

    def test_start_is_help(self, portfolio):
        assert stock_bot.handle_command("/start") == stock_bot.handle_command("/help")

    def test_group_chat_suffix_is_stripped(self, portfolio):
        assert stock_bot.handle_command("/help@my_bot") is not None

    def test_portfolio(self, portfolio):
        out = _plain(stock_bot.handle_command("/portfolio"))
        assert "MC.PA" in out
        assert "Portfolio" in out

    def test_portfolio_reads_as_a_message_not_a_terminal(self, portfolio):
        # Box drawing and monospace blocks look like a terminal dump on a
        # phone; this is what the format was rewritten to avoid.
        out = stock_bot.handle_command("/portfolio")
        assert not set(out) & set("┏┓┗┛━┃│├┤┼╭╮╰╯"), "box drawing in a chat message"
        assert "<pre>" not in out
        assert "…" not in out

    def test_portfolio_is_ordered_by_size(self, mocker, portfolio):
        # The top of the list is what gets read on a phone. Names are chosen so
        # size order and alphabetical order disagree — otherwise the assertion
        # passes without the sort doing anything.
        summaries = {
            "AAA.ST": {
                "symbol": "AAA.ST", "qty": 1, "val_now": 100.0, "val_prev": 100.0,
                "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "EUR",
                "conv": 1.0, "price": 100.0,
            },
            "ZZZ.ST": {
                "symbol": "ZZZ.ST", "qty": 1, "val_now": 9000.0, "val_prev": 9000.0,
                "chg_pct": 0.0, "daily_chg_val": 0.0, "source_currency": "EUR",
                "conv": 1.0, "price": 9000.0,
            },
        }
        mocker.patch("stock.fetch_summaries", return_value=(summaries, {}, []))
        out = _plain(stock_bot.handle_command("/portfolio"))
        assert out.index("ZZZ.ST") < out.index("AAA.ST")

    def test_direction_markers(self, portfolio):
        out = _plain(stock_bot.handle_command("/portfolio"))
        assert stock_bot.UP in out

    def test_portfolio_records_history(self, portfolio, mocker):
        record = mocker.patch("stock.record_portfolio_value")
        stock_bot.handle_command("/portfolio")
        record.assert_called_once()

    def test_holding(self, portfolio):
        out = _plain(stock_bot.handle_command("/holding MC.PA"))
        assert "21,382" in out
        assert "+18.79%" in out
        assert "Buy" in out

    def test_holding_is_case_insensitive(self, portfolio):
        assert "21,382" in _plain(stock_bot.handle_command("/holding mc.pa"))

    def test_holding_unknown_lists_what_is_held(self, portfolio):
        out = _plain(stock_bot.handle_command("/holding NOPE"))
        assert "not in the portfolio" in out
        assert "MC.PA" in out

    def test_holding_without_an_argument(self, portfolio):
        assert "Usage" in _plain(stock_bot.handle_command("/holding"))

    def test_dividends(self, portfolio):
        out = _plain(stock_bot.handle_command("/dividends"))
        assert "Dividends" in out
        assert "585" in out

    def test_allocation(self, portfolio):
        out = _plain(stock_bot.handle_command("/allocation"))
        assert "Allocation" in out
        assert "MC.PA" in out
        assert "%" in out

    def test_news(self, portfolio):
        out = stock_bot.handle_command("/news")
        assert "LVMH does something" in out
        assert "https://example.com/a" in out

    def test_short_aliases(self, portfolio):
        for alias in ("/p", "/d", "/n", "/a"):
            assert stock_bot.handle_command(alias) is not None

    def test_empty_message(self, portfolio):
        assert stock_bot.handle_command("   ") is None


class TestFormatting:
    def test_code_block_escapes_html(self):
        assert "&lt;b&gt;" in stock_bot.as_code_block("<b>")

    def test_render_has_no_ansi_escapes(self):
        # ANSI would appear as literal noise in a Telegram message.
        from rich.text import Text

        assert "\x1b" not in stock_bot.render(Text("hi", style="bold red"))

    def test_long_messages_are_trimmed_to_the_limit(self, sent):
        stock_bot.send_message("tok", 42, "x\n" * 5000)
        assert len(sent[0][1]["text"]) <= stock_bot.MAX_MESSAGE
        assert "truncated" in sent[0][1]["text"]

    def test_short_messages_pass_through(self, sent):
        stock_bot.send_message("tok", 42, "hello")
        assert sent[0][1]["text"] == "hello"

    def test_messages_are_sent_as_html(self, sent):
        stock_bot.send_message("tok", 42, "hi")
        assert sent[0][1]["parse_mode"] == "HTML"


class TestSetupMode:
    """With no allowlist the bot must still be configurable.

    The allowlist can't be filled in until you know your own chat ID, and the
    bot is the obvious place to ask — but it must not become an open door to
    the portfolio in the meantime.
    """

    def _update(self, chat_id=4242, text="/start"):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_reports_the_sender_chat_id(self, sent, portfolio):
        assert stock_bot.process_update(self._update(), "tok", set()) is True
        assert "4242" in sent[0][1]["text"]

    def test_says_it_is_unconfigured(self, sent, portfolio):
        stock_bot.process_update(self._update(), "tok", set())
        assert "Not configured" in sent[0][1]["text"]

    def test_serves_no_portfolio_data(self, sent, portfolio):
        stock_bot.process_update(self._update(text="/portfolio"), "tok", set())
        body = sent[0][1]["text"]
        assert "MC.PA" not in body
        assert "21,381.75" not in body

    def test_every_command_gets_the_same_setup_reply(self, sent, portfolio):
        for command in ("/start", "/portfolio", "/dividends", "/news"):
            sent.clear()
            stock_bot.process_update(self._update(text=command), "tok", set())
            assert "Not configured" in sent[0][1]["text"], command

    def test_starts_without_an_allowlist(self, mocker, monkeypatch):
        # It used to exit here, which made the chat ID impossible to discover.
        monkeypatch.setenv("TELEGRAM_TOKEN", "tok")
        monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
        mocker.patch("stock_bot._request", side_effect=KeyboardInterrupt)
        stock_bot.main()  # returns cleanly rather than raising SystemExit

    def test_still_refuses_without_a_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
        with pytest.raises(SystemExit, match="TELEGRAM_TOKEN"):
            stock_bot.main()

    def test_configured_bot_does_not_leak_ids_to_strangers(self, sent, portfolio):
        # Once configured, an unknown chat gets silence — not its own id back.
        assert stock_bot.process_update(self._update(999), "tok", {"42"}) is False
        assert sent == []


class TestPartialCostBasis:
    """P/L can only cover holdings that have a recorded cost basis.

    Presenting it as a whole-portfolio figure would overstate it, exactly as
    the dividends yield-on-cost total once did.
    """

    def test_coverage_is_named_when_partial(self, portfolio):
        # The fixture prices MC.PA but not IUSA.DE.
        out = _plain(stock_bot.handle_command("/portfolio"))
        assert "1 of 2 holdings" in out

    def test_no_qualifier_when_everything_is_priced(self, mocker, portfolio):
        summaries = {
            "MC.PA": {
                "symbol": "MC.PA", "qty": 45, "val_now": 21381.75,
                "val_prev": 21000.0, "chg_pct": 1.82, "daily_chg_val": 381.75,
                "source_currency": "EUR", "conv": 1.0, "price": 475.15,
                "cost": 400.0, "cost_value": 18000.0, "unrealized": 3381.75,
                "return_pct": 18.79,
            }
        }
        mocker.patch("stock.fetch_summaries", return_value=(summaries, {}, []))
        out = _plain(stock_bot.handle_command("/portfolio"))
        assert "all time" in out
        assert "of 1 holdings" not in out

    def test_percentage_uses_only_priced_holdings(self, portfolio):
        # 3,381.75 on 18,000 is +18.79%; spreading it over the unpriced
        # position too would report a much smaller number.
        assert "+18.79%" in _plain(stock_bot.handle_command("/portfolio"))

    def test_no_pl_line_without_any_cost_basis(self, mocker, portfolio):
        summaries = {
            "IUSA.DE": {
                "symbol": "IUSA.DE", "qty": 720, "val_now": 46340.64,
                "val_prev": 46000.0, "chg_pct": 0.74, "daily_chg_val": 340.64,
                "source_currency": "EUR", "conv": 1.0, "price": 64.36,
            }
        }
        mocker.patch("stock.fetch_summaries", return_value=(summaries, {}, []))
        assert "all time" not in _plain(stock_bot.handle_command("/portfolio"))


class TestFetching:
    """Every command used to refetch everything, which is what made replies
    slow on a Pi. Each now asks only for what it renders, and reuses anything
    still fresh."""

    @pytest.fixture
    def aux(self, portfolio, mocker):
        return mocker.patch(
            "stock.fetch_auxiliary",
            return_value={"history": [1.0, 2.0], "monthly": {}, "traded": None,
                          "dividends": {}, "news": [], "analysts": {}},
        )

    def _wants(self, call):
        return {k: v for k, v in call.kwargs.items() if k.startswith("want_")}

    def test_portfolio_does_not_fetch_news_or_analysts(self, aux):
        stock_bot.handle_command("/portfolio")
        wants = self._wants(aux.call_args)
        assert wants["want_history"] is True
        assert wants["want_news"] is False
        assert wants["want_dividends"] is False
        assert wants["want_analysts"] is False

    def test_dividends_fetches_only_dividends(self, aux):
        stock_bot.handle_command("/dividends")
        wants = self._wants(aux.call_args)
        assert wants["want_dividends"] is True
        assert wants["want_news"] is False
        assert wants["want_analysts"] is False

    def test_news_fetches_only_news(self, aux):
        stock_bot.handle_command("/news")
        wants = self._wants(aux.call_args)
        assert wants["want_news"] is True
        assert wants["want_history"] is False

    def test_holding_fetches_analysts(self, aux):
        stock_bot.handle_command("/holding MC.PA")
        assert self._wants(aux.call_args)["want_analysts"] is True

    def test_allocation_needs_no_auxiliary_fetch_at_all(self, aux):
        stock_bot.handle_command("/allocation")
        aux.assert_not_called()


class TestCaching:
    @pytest.fixture
    def fetches(self, portfolio, mocker):
        mocker.patch(
            "stock.fetch_auxiliary",
            return_value={"history": [1.0, 2.0], "monthly": {}, "traded": None,
                          "dividends": {}, "news": [], "analysts": {}},
        )
        return mocker.patch(
            "stock.fetch_summaries",
            return_value=({"MC.PA": {
                "symbol": "MC.PA", "qty": 45, "val_now": 21381.75,
                "val_prev": 21000.0, "chg_pct": 1.82, "daily_chg_val": 381.75,
                "source_currency": "EUR", "conv": 1.0, "price": 475.15,
            }}, {"MC.PA": "EUR"}, []),
        )

    def test_repeat_commands_reuse_prices(self, fetches):
        stock_bot.handle_command("/portfolio")
        stock_bot.handle_command("/portfolio")
        assert fetches.call_count == 1

    def test_different_commands_share_the_price_fetch(self, fetches):
        stock_bot.handle_command("/portfolio")
        stock_bot.handle_command("/allocation")
        stock_bot.handle_command("/holding MC.PA")
        assert fetches.call_count == 1

    def test_prices_refresh_once_stale(self, fetches, mocker):
        stock_bot.handle_command("/portfolio")
        clock = [time.monotonic() + stock_bot.SUMMARY_TTL + 1]
        mocker.patch("stock_bot.time.monotonic", side_effect=lambda: clock[0])
        stock_bot.handle_command("/portfolio")
        assert fetches.call_count == 2

    def test_slow_sections_outlive_prices(self, fetches, mocker):
        # Analyst ratings change over days; refetching them whenever a price
        # goes stale would put the slowest calls back on every command.
        aux = mocker.patch(
            "stock.fetch_auxiliary",
            return_value={"analysts": {}, "history": [], "monthly": {},
                          "traded": None},
        )
        stock_bot.handle_command("/holding MC.PA")
        clock = [time.monotonic() + stock_bot.SUMMARY_TTL + 1]
        mocker.patch("stock_bot.time.monotonic", side_effect=lambda: clock[0])
        stock_bot.handle_command("/holding MC.PA")
        assert fetches.call_count == 2
        assert aux.call_count == 1
