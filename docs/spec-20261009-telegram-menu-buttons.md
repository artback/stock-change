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

(Shipped as v0.11.0. The field report that followed — a duplicate message per
re-tap, and the missing header button other bots have — is resolved in
v0.11.1; see **Follow-up** below.)

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

## Follow-up (v0.11.1): sticky no-op + bar menu button

Field report after the v0.11.0 homelab deploy: (1) re-tapping the section the
sticky menu already shows stacked a *new* message per tap, and (2) the chat
header had no menu button of its own, unlike other bots.

### Bug: "message is not modified" became a duplicate message

Telegram rejects an `editMessageText` whose text *and* markup are unchanged
with `400 Bad Request: message is not modified` (reproduced live against the
running bot on 2026-10-09). Re-tapping the section the sticky menu already
shows is exactly that — normal use, not a failure. The v0.11.0 fallback
treated *every* edit exception as "edit failed, send a fresh message", so
each no-op tap stacked a duplicate; the warning was also logged without the
API error, which made the cause invisible.

Fix in `process_callback`: an edit rejected as `message is not modified` is a
no-op — the requested reply is already on screen, so nothing is sent and only
the ack stops the spinner. Any *other* edit failure still falls back to a
fresh message, now logged **with** the API error.

### Feature: the bar menu button

The per-chat Menu Button and the command dropdown are opt-in on
Telegram's side; v0.11.0 registered neither, so the header showed only the
bare default ("Start"). Two calls change that:

- `setMyCommands` — once at startup (`main`). Registers
  `start / portfolio / holding / dividends / allocation / news / help` with
  descriptions mirroring `HELP`, so the dropdown the header button opens
  lists exactly what `handle_command` handles and can't drift from it.
- `setChatMenuButton(chat_id, {"type": "commands"})` — once per chat per
  process (`_menu_button_chats`), from the command path for allowlisted
  chats, after the reply has been sent. Turns the header's default button
  into the command-menu dropdown.

Both are cosmetic: a `setMyCommands` failure at startup logs a warning and
the menu stays at Telegram's default; a `setChatMenuButton` failure logs and
retries on the next command — neither ever breaks a reply. Setup mode and
strangers never get the button. (The button only appears in private chats —
a Telegram limitation, which is this bot's use case.)

### Contract changes (v0.11.1)

- `process_callback` — `message is not modified` edits are no-ops (ack
  only); other edit failures log the API error.
- New: `set_my_commands`, `set_chat_menu_button`, `_ensure_menu_button`,
  module-level `_menu_button_chats`.
- `main()` — registers the command list once before polling.
- `process_update` — installs the chat's header button once, after a
  successful command reply.
- `tests/test_bot.py` — no-op re-tap, command-list registration, once-per-
  chat install, stranger/setup silence, install-failure safety.