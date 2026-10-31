# Telegram bot: inventory updates (buy / sell / remove)

**Date:** 2026-10-31 · **Type:** new feature · **Files:** `stock_bot.py`, `tests/test_bot.py`, `README.md`, `deploy/nomad/stock-bot.nomad.hcl`, pack `iac_jonathan/packs/stock-bot`

## Ask

When the user buys or sells something, they should be able to record it from
the phone instead of editing a YAML file. The bot stays the allowlisted chat's
bookkeeper: it records positions the user already owns, and it never places a
trade.

## Design

### Commands

| Command | Meaning | Calls |
| --- | --- | --- |
| `/buy TICKER QUANTITY [COST]` | Bought more (or a new position). COST is the price paid per share on this purchase, in the ticker's own currency; it is blended into the average cost as a weighted average. | `stock.add_shares(ticker, +qty, cost=cost)` |
| `/sell TICKER QUANTITY` | Sold some. The recorded cost basis is left untouched (average-cost accounting); selling down to zero removes the position. | `stock.add_shares(ticker, -qty)` |
| `/remove TICKER` | Sold out of a position, dropped it. | `stock.remove_holding(ticker)` |

All three go through the same write API the MCP server already uses
(`stock.add_shares` / `stock.remove_holding` → `_apply_holding_change` →
`write_config_document`), so the bot gains no new write path: ruamel
round-trip when installed, atomic write with a `.bak` otherwise, a `.bak` of
the previous file on every change.

### Validation

- The ticker is checked with `stock.resolve_symbol` before anything is
  written: a typo'd or unknown ticker is **refused** (a wrong ticker would sit
  in the config silently breaking the portfolio total), while a ticker the
  price service could not reach (rate limit, offline) is still recorded, with
  an "unchecked" note in the reply.
- Quantities and costs must parse as positive numbers; anything else gets the
  usage reply and writes nothing.
- Overselling (`/sell` more than held) and removing an absent ticker are
  refusals with the error in the reply; the file is untouched.
- Only allowlisted chats reach the handlers — strangers get silence, as with
  every other command. The write commands add no new exposure: same allowlist,
  same single-user chat.

### Reply shape

A successful change replies with a confirmation line plus the **updated
portfolio with the menu keyboard**, so the change is visible in place — the
same loop as a menu tap. Before rendering, the bot's quote cache is cleared:
it is keyed to the holdings last fetched, and those just changed (the
background refresher repopulates it within its cycle).

### Persistence

Today the config renders to `/local/stock.yaml` (alloc-local: wiped on every
deploy) and `HOME` points at a container-local `/data`, so a recorded buy
would vanish on the next deploy and the 30-day history reset every time.

The pack now mounts a **Docker named volume** (`stock-bot-data`) at `/data` —
the proven pattern on this cluster (postgres/immich; there are no Nomad
volume objects, and the `volumes = ["name:path"]` shorthand is alloc-local and
is how the original postgres data was lost). With it:

- `STOCK_PRICE_CONFIG=/data/stock.yaml` — the holdings file survives deploys.
- `start.sh` copies the rendered seed (`/local/stock.yaml`, from the
  gitignored vars file) into `/data/stock.yaml` **only if the live file is
  missing**. After first boot the volume is authoritative; the vars file is
  a first-run seed only.
- Side benefit: `HISTORY_PATH` and `CACHE_PATH` are `Path.home() / ...`, and
  `HOME=/data` — the 30-day portfolio history and the price cache stop
  resetting on every deploy.

### Docs

The "read-only / never edits holdings" claims in the module docstring,
`HELP`, `WELCOME`, README and the reference Nomad spec are reworded to:
reports, records holdings changes on command, never places a trade.

## Follow-up

(Shipped as v0.13.0.)