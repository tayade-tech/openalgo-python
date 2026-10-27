# Scalping Terminal (`/scalping`) — Product Requirements Document

**Status:** Implemented · **Branch:** `scalping` · **Last updated:** 2026-06-14

A keyboard-driven, low-latency options/futures/equity scalping terminal built into
OpenAlgo (Flask + React 19): a one-click scaling tool for fast intraday execution. It
reuses the existing broker session, unified WebSocket market-data feed, ZeroMQ bus,
SocketIO event bus, and service layer — no new broker plumbing.

---

## 1. Goals & non-goals

**Goals**
- Fire **MARKET** orders with a single keystroke (zero lookups on the hot path).
- Trade options (dual-leg CE/PE), futures, and equity across all supported segments.
- Enforce risk: predefined Auto SL/Target, per-position SL/TP/Trailing-SL,
  freeze-quantity-safe split orders (entries and exits).
- Keep the engine working **after the user leaves the page or closes the browser**.
- Be **fully event-driven (zero polling)** for prices, books, MTM, and risk triggers.
- **Segregate sandbox (Analyze) and live orders/state** completely.

**Non-goals**
- Multi-user. OpenAlgo is single-user-per-deployment.
- LIMIT/SL order types on the hot path (entries/exits are MARKET).
- Replacing the broker's master-contract data (lot sizes come from `SymToken`).

---

## 2. Order constants (authoritative — OpenAlgo format)

- **Exchange:** `NSE`, `BSE` (equity); `NFO`, `BFO`, `MCX`, `CDS` (derivatives);
  `NSE_INDEX`/`BSE_INDEX` (underlying quotes).
- **Product:** `MIS`, `NRML`, `CNC` — shown as raw codes (not Intraday/Margin/Delivery).
- **Price type:** `MARKET` (entries and exits).
- **Action:** `BUY`, `SELL`.

Defaults: Exchange `NFO`; Product `NRML` for F&O, `MIS` for equity; per-exchange default
underlying — NFO→NIFTY, BFO→SENSEX, MCX→CRUDEOIL, CDS→USDINR.

---

## 3. Functional requirements

### 3.1 Instrument selection
- **Exchange** selector: NSE, BSE, NFO, BFO, MCX, CDS.
- **Segment**: Equity (NSE/BSE only) · Options / Futures (F&O exchanges).
- **Underlying**: full dropdown of every F&O underlying for the exchange
  (`/scalping/api/all_underlyings`), indices first then stocks, type-to-filter — mirrors
  `/search/token`. Equity uses search-as-you-type.
- **Options**: nearest expiry auto-selected; CE strike + PE strike default to **ATM**.
  For MCX/CDS, ATM is derived from the current-month **future** LTP.
- **Futures**: nearest expiry auto-selected; framed `…FUT` symbol.

### 3.2 Order entry (keyboard)
- One-Click **ARM** toggle (persisted across reloads); arming gates only new
  risk-increasing entries.
- Options: `↑` Buy Call · `↓` Sell Call · `→` Buy Put · `←` Sell Put.
- Single-instrument (equity/futures): `↑/→` Buy · `↓/←` Sell.
- `F6` Close-All (scalping-scoped), `F7` Cancel-All. These safety actions fire even from
  inputs and even when disarmed. *(macOS treats F1–F12 as hardware keys by default — the
  on-screen Close-All / Cancel-All buttons always work; F6/F7 need Fn or the "use F-keys
  as standard function keys" setting.)*
- Lots are **unlimited**: an entry above the instrument's exchange freeze limit is
  auto-split server-side into whole-lot, freeze-sized orders (see 3.7). The UI
  shows the split preview (e.g. NIFTY 28 lots → 27 + 1). Whole-lot validation
  server-side; the only input ceiling is the 100,000-qty sanity limit.
- Order-entry cooldown debounces accidental key-repeat.

### 3.3 Live tickers
- CE, PE, and underlying panels show LTP, change %, and an **OHLC range bar**
  (L→H with Open `○` and LTP `▲` markers). Underlying subscribes on its index/spot
  exchange (e.g. NIFTY→`NSE_INDEX`) or the current-month future for MCX/CDS.

### 3.4 Position book
- Per-(symbol,exchange,product) rows: Side, Net Qty, LTP, **SL · TP · TSL**, Realized/
  Unrealized/Total P&L, Avg/Buy/Sell price & qty.
- Per-row **Close** (freeze-safe) and **Risk** (edit SL/Target/Trailing) buttons.
- Scoped Net Qty + MTM summary across scalping rows only.

### 3.5 Risk: SL / Target / Trailing-SL
- **Predefined Auto SL & Target** (global; Points or Percent, default Points) auto-attached
  to every new entry.
- Per-position SL, optional Target (TP), optional Trailing-SL (step) editable on demand.
- Direction-validated (a long stop must be below price, a short stop above).

### 3.6 Books scoping
- Position/Order/Trade books and Close-All are scoped to **instruments this terminal has
  traded** (the scalping list / `scalping_tracked_symbol`), since broker positions carry no
  strategy tag. Order/Trade books additionally show **today only**.

### 3.7 Freeze-quantity handling
- Freeze limits are **dynamic, never hardcoded**: read from the admin-managed
  `qty_freeze` table (`database/qty_freeze_db.py`, editable at /admin/freeze) and
  the instrument's lot size from `SymToken`. The per-order cap is
  `floor(freeze_qty / lotsize)` lots — e.g. NIFTY 1800/65 → 27 lots = 1755 qty,
  BANKNIFTY 600/30 → 20 lots = 600 qty.
- **Entries and exits both** auto-split into whole-lot, freeze-sized chunks, so
  a single order of any size never exceeds the exchange freeze (28 NIFTY lots →
  27 + 1 orders). Splits route through `services/split_order_service.py`
  (sequential, rate-limited, sandbox-aware).

---

## 4. Server-side risk engine (event-driven, browser-independent)

`services/scalping_risk_monitor_service.py` — a singleton background monitor:

- **Event-driven, zero polling.** Consumes live ticks from the WebSocket proxy
  (`services/websocket_client.py`, LTP mode); each tick drives one evaluation. The watched
  symbol set changes only when an SL is saved/deleted (`/sl` endpoints call `sync()`);
  trailing updates and auto-clears are pushed to the browser via a SocketIO
  `scalping_sl_update` event.
- **Browser-independent.** Works after the user leaves `/scalping` or closes the browser.
  The React `useTrailingSL` hook is now **config + display only** (no executor) — exactly
  one engine, no double-exit.
- **Freeze-safe exits** sized to the *current* live position via
  `blueprints.scalping._reducing_exit`; SL cleared only on a confirmed exit.
- **Reconnect-safe** (re-subscribes on auth), **FD-safe** (one shared WS connection, per-
  iteration scoped-session cleanup, `atexit` teardown).

Trailing rule: long stops only rise, short stops only fall; trailing starts only once the
position is ≥1 in profit.

---

## 5. Analyze (sandbox) vs Live segregation

- The books route per-mode server-side (services honour `get_analyze_mode()`).
- `scalping_tracked_symbol` and `scalping_sl_state` carry a **`mode`** column
  (`analyze`|`live`). The scalping list, SL states, Close-All, and books are filtered to the
  **current mode**, so sandbox state never appears in live (and vice-versa).
- The risk monitor only watches/acts on SLs matching the current mode; a mode mismatch is
  skipped, and the exit worker's live-position check is the backstop.
- The React books re-fetch when the Analyze/Live toggle changes (query keys include
  `appMode`), so each mode shows its corresponding positions.

---

## 6. Non-functional requirements

- **Latency:** keypress → order with no blocking lookups on the hot path.
- **Event-driven:** no polling for prices, MTM, books, or risk triggers (WebSocket ticks +
  SocketIO order/SL events; `refetchOnWindowFocus` and mode-toggle are events, not timers).
- **FD hygiene:** shared singletons (WS client, scoped sessions removed in background
  threads), no per-call sockets/engines; audited per CLAUDE.md.
- **Security:** session + CSRF on all `/scalping` endpoints; server-side validation of
  action/exchange/product/quantity/lot-multiple plus the 100k sanity ceiling and
  freeze-split routing; sandbox routing via api_key only.

---

## 7. Key APIs (`blueprints/scalping.py`)

| Endpoint | Purpose |
|---|---|
| `GET /scalping/api/all_underlyings` | All F&O underlyings for an exchange (indices first) |
| `GET /scalping/api/expiry` | Expiries for an underlying (options/futures) |
| `GET /scalping/api/strikes` | Option chain + ATM (NFO/BFO; MCX/CDS via future) |
| `GET /scalping/api/futures` | Futures contracts (nearest-first) |
| `GET /scalping/api/search` | Equity/instrument search |
| `POST /scalping/api/order` | Place MARKET order (validated, tracked w/ mode) |
| `POST /scalping/api/close_leg` | Freeze-safe single-leg exit |
| `POST /scalping/api/close_all` | Close scalping-scoped positions (current mode) |
| `POST /scalping/api/cancel_all` | Cancel all open orders |
| `GET/POST/DELETE /scalping/api/sl` | SL/TP/TSL state (mode-scoped; notifies monitor) |
| `GET/DELETE /scalping/api/tracked` | Scalping list (mode-scoped) |

---

## 8. Data model (`database/scalping_db.py`)

- **`ScalpingSLState`** — one row per leg's SL config: side, entry, qty, initial/current SL,
  trailing on/step, highest/lowest, target, **mode**, is_active.
- **`ScalpingTrackedSymbol`** — (symbol, exchange, product, **mode**) the terminal has traded.

Both migrate idempotently (added `lowest_price`, `target`, `mode` columns).

---

## 9. Testing & validation

- **Unit:** `test/test_scalping_risk_monitor.py` (23 tests) — SL/TP/TSL triggers for long &
  short, trailing edge cases, event-driven `_on_tick`, and **mode segregation**.
- **Sandbox lifecycle:** `docs/scalping/ORDER_LIFECYCLE_TESTS.md` (20/20) — entry/scale/
  partial+full exit, short cycle, freeze-split (28→27+1), futures, guards, cancel-all,
  tracking; plus a live end-to-end of the server engine (entry → breach tick → auto-exit →
  flat → state cleared) which surfaced & fixed the sandbox-auth routing bug.
- **Static:** `npm run build` + biome (frontend), `ruff` (backend) — clean.

---

## 10. Known limitations / dependencies

- **MCX/CDS lot size = 1** in the current broker master contract (`SymToken`) — CRUDEOIL
  should be 100, USDINR 1000. This is a **master-contract data issue (platform-wide)**, not
  a scalping defect; MCX/CDS orders will be mis-sized until the master contract is
  refreshed/fixed in the broker layer. NSE/BSE/NFO/BFO lot sizes are correct.
- **macOS function keys:** F6/F7 may not reach the browser; on-screen buttons always work.
- The `scalping_sl_update` SocketIO push is best-effort (wrapped in try/except); the books
  still reconcile via order events regardless.
