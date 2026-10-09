# Telegram bot: inline menu buttons

**Date:** 2026-10-09 · **Type:** new feature · **File:** `stock_bot.py` (+ `tests/test_bot.py`, `README.md`)

## Ask

The bot currently answers only typed slash commands; a button press is silently
ignored. Add an inline-keyboard menu so the portfolio can be read by tapping,
not typing — the phone is where this bot is actually used.

## Design

### What "menu button" means here

Telegram's per-chat "Menu Button" API (`setChatMenuButton`) replaces the bot's
command menu in the chat header — a different UX. What the bot needs is an
**inline keyboard**: buttons attached to messages, pressing which sends a
`callback_query` update back to the bot. All five endpoints used
(`sendMessage.reply_markup`, `callback_query`, `answerCallbackQuery`,
`editMessageText`) are stable since the original Bot API and are received via
the existing `getUpdates` long-poll with no extra registration.

### The menu

`menu_keyboard()` returns the standard markup — one button per section,
`callback_data` values:

| Button | callback_data | renders |
| --- | --- | --- |
| 📊 Portfolio | `portfolio` | `cmd_portfolio` |
| 📈 Holding | `holding` | ticker picker (below) |
| 💰 Dividends | `dividends` | `cmd_dividends` |
| ⚖️ Allocation | `allocation` | `cmd_allocation` |
| 📰 News | `news` | `cmd_news` |

**Holding is two levels**, because a button cannot carry a ticker argument:
the `holding` button renders a short message plus a keyboard with one button
per configured ticker (`callback_data` = `holding:<TICKER>`) and the main
menu rows below it; tapping a ticker renders `cmd_holding` for that symbol.
Ticker buttons are capped at 20 (the picker text still lists all holdings,
and the JSON of hundreds of buttons is wasted payload on a phone).

### Where the menu is attached

- `/start` — new `WELCOME` text (short; points at the buttons) + menu.
  `/help` keeps the full command list and also gets the menu.
- Every section reply (text command *or* button) gets the menu attached, so
  the chat never dead-ends into typing.
- Setup-mode replies (`SETUP_REPLY`) get **no** menu — unconfigured means no
  data, buttons included.

### Callback flow (`process_callback`, new)

`process_update` routes `update["callback_query"]` to `process_callback`:

1. Extract `chat_id` from `callback.message.chat`, `message_id` from
   `callback.message`, `data` from `callback.data`.
2. **Setup mode** (empty allowlist): acknowledge the callback, serve nothing.
3. **Stranger** (chat not allowlisted — possible in group chats where buttons
   are visible to all members): acknowledge, serve nothing, log as the text
   path does.
4. Otherwise route `data` through `_route_callback` onto the *existing*
   `cmd_*` functions — no rendering logic duplicated. Unknown `data`:
   acknowledge, no reply.
5. Reply **in place**: `editMessageText` on the menu message (the menu stays
   in one chat position instead of stacking a message per tap). If the edit
   raises (too old, API hiccup), fall back to a new `sendMessage` with the
   same text and markup — a button press must always produce a reply.
6. `answerCallbackQuery` after the reply (Telegram keeps the button's
   spinner spinning until acknowledged, even on ignored presses). An ack
   failure is cosmetic: log and continue.

### Contract changes

- `send_message(token, chat_id, text, reply_markup=None)` — optional markup
  pass-through; truncation extracted to `_trim()` so `editMessageText` shares
  it.
- `/start` no longer equals `/help` (welcome vs. full command list) — the
  existing `test_start_is_help` assertion is updated deliberately.
- `handle_command` and all `cmd_*` functions keep their text-only return —
  the markup is composed in the reply layer, not the renderers.
- No new dependencies; `main()`'s poll loop is untouched (`callback_query`
  updates arrive through the existing `getUpdates` call).

## Edge cases

- Pressed button message too old / edited away → edit fails → new message.
- Group chat: buttons visible to non-allowlisted members → they get a silent
  ack, never data (same silence rule as the text path).
- 20+ holdings → picker caps buttons, text lists the rest.
- Failing section (network/rate limit) → same "went wrong" reply the text
  path gives, with the menu still attached.
- `callback.data` empty/unknown → ack, no reply, no crash.
- Long replies → `_trim` applied to edits exactly as to sends.

## Trade-offs

- **Edit-in-place over new message per tap**: one living menu instead of a
  growing chat log; costs a fallback path for the edit-failure case.
- **Ticker picker from the config file, not live summaries**: instant (no
  price fetch), and the detail view already degrades gracefully when a
  ticker failed to load.
- **Buttons attached to every reply**: small JSON per message; keeps every
  screen navigable. Revisit only if Telegram messages start failing because
  of it.

## Files changed

- `stock_bot.py` — `_trim`, `send_message(..., reply_markup=None)`,
  `WELCOME`, `menu_keyboard`, `holding_keyboard`, `_route_callback`,
  `process_callback`, `process_update` routing, text-path replies carry the
  menu, docstring.
- `tests/test_bot.py` — menu markup on replies, callback routing, two-level
  holding, in-place edit + fallback, stranger/setup silence, ack behaviour,
  unknown data, failing-section reply.
- `README.md` — Telegram bot section: the menu, and that commands still work.