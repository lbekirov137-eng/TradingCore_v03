# Bybit Demo Trading — Setup Guide

**Status: NOT CONNECTED.** No credentials are configured, and the system will not attempt any authenticated request until you supply your own Bybit **Demo Trading** keys. This is deliberate.

---

## 1. Safety model

| Guarantee | How it is enforced |
|---|---|
| Only the official Bybit Demo endpoint is used | `api/exchanges/bybit_demo/config.py::validate_endpoint` rejects `api.bybit.com`, `api.bytick.com`, and any non-demo host when `TRADING_ENVIRONMENT=DEMO` |
| Production endpoints cannot be reached in demo mode | Validated on **every** request, not just at startup |
| `LIVE` mode is impossible | `validate_demo_configuration()` raises `ConfigurationError` on `TRADING_ENVIRONMENT=LIVE`; there is no live order code path |
| Adapter refuses to construct outside DEMO | `BybitDemoAdapter.__init__` raises unless `TRADING_ENVIRONMENT=DEMO` |
| Secrets are never printed | Only boolean "set / not_set" flags are ever returned or logged; `X-BAPI-SIGN` is a derived HMAC, never the secret |
| Retries cannot duplicate an order | Every retry reuses the identical `orderLinkId` (= our deterministic `client_order_id`); Bybit rejects duplicates |
| Timeouts never trigger a blind resend | A timeout raises `TimeoutError`; the caller must reconcile via `get_order()` before any further action |

All of the above are covered by 23 mocked tests in `tests/unit/test_bybit_demo_adapter.py` — **none of which perform real network calls**.

---

## 2. Prerequisites

1. A Bybit account with **Demo Trading** enabled (this is Bybit's official demo environment — not testnet, not mainnet).
2. Demo API key + secret generated from the Demo Trading section of your Bybit account.
3. **Do not** enable withdrawal permission on the key. Demo keys should be read + trade only.

---

## 3. Configuration

```bash
cp .env.example .env
```

Then edit `.env`:

```
TRADING_ENVIRONMENT=DEMO
BYBIT_DEMO_API_KEY=<your demo key>
BYBIT_DEMO_API_SECRET=<your demo secret>
```

`.env` is in `.gitignore` and must never be committed.

> The repository ships **only** `.env.example`. No real credential values exist anywhere in this repo — verified by an automated test (`tests/unit/test_security_hygiene.py`).

---

## 4. Validate configuration (no connection made)

```bash
python -c "from api.exchanges.bybit_demo.adapter import BybitDemoAdapter; import json; print(json.dumps(BybitDemoAdapter.preflight(), indent=2))"
```

Or via the API:

```bash
curl http://localhost:8000/demo/preflight
```

Expected before credentials are set:

```json
{
  "environment": "PAPER",
  "ready": false,
  "reason": "Bybit Demo адаптер требует TRADING_ENVIRONMENT=DEMO (текущее: PAPER).",
  "credentials": {"BYBIT_DEMO_API_KEY": false, "BYBIT_DEMO_API_SECRET": false}
}
```

Expected once configured correctly:

```json
{
  "environment": "DEMO",
  "ready": true,
  "reason": "Конфигурация DEMO корректна.",
  "credentials": {"BYBIT_DEMO_API_KEY": true, "BYBIT_DEMO_API_SECRET": true},
  "rest_url": "https://api-demo.bybit.com"
}
```

Note the response reports only **whether** variables are set — never their values.

---

## 5. Implemented adapter surface

`api/exchanges/bybit_demo/adapter.py` implements the shared `ExchangeAdapter` interface, so the decision engine, reconciler, and exit monitor work against it unchanged:

| Capability | Endpoint | Status |
|---|---|---|
| Market data (klines) | `/v5/market/kline` | Implemented (unsigned) |
| Place order | `/v5/order/create` | Implemented, `orderLinkId` idempotency |
| Amend order | `/v5/order/amend` | Implemented |
| Cancel order | `/v5/order/cancel` | Implemented |
| Open orders | `/v5/order/realtime` | Implemented |
| Order status / reconciliation | `/v5/order/realtime` + `/v5/order/history` fallback | Implemented |
| Executions | `/v5/execution/list` | Implemented |
| Position | `/v5/position/list` (spot via balance) | Implemented |
| Balance | `/v5/account/wallet-balance` | Implemented |
| Rate-limit handling | HTTP 429 → exponential backoff, same `orderLinkId` | Implemented |
| Retry with idempotency | Same `orderLinkId` on every attempt | Implemented |

### Not implemented (honest gaps)

- **WebSocket streaming and reconnect logic are NOT implemented.** The `websockets` dependency is installed but no WS client exists. All data is currently fetched via REST polling. The "WebSocket reconnect / REST fallback" requirement is **not met** — REST is the only path today.
- **No live demo session has ever been run.** Every adapter test uses mocks. Behavior against the real Bybit demo API is therefore *unverified* — response-shape assumptions (field names, status strings) are based on the Bybit V5 documented schema, not observed traffic.
- Spot position derivation from wallet balance is an approximation (`avg_entry` is reported as 0 for spot).

---

## 6. Running against demo

**Do not do this yet.** Gate E (Bybit Demo) requires Gates A–D to pass first, and Gate B (backtest validity) currently **fails** — no strategy has a demonstrated edge. See `AUTOTRADING_RELEASE_GATES.md`.

When gates permit, the intended flow is:

```bash
# 1. Validate config without connecting
curl http://localhost:8000/demo/preflight

# 2. Confirm kill switch is available and working
curl -X POST "http://localhost:8000/kill-switch/engage?reason=preflight-test"
curl http://localhost:8000/kill-switch/status
curl -X POST "http://localhost:8000/kill-switch/disengage"

# 3. Only then start the demo loop
TRADING_ENVIRONMENT=DEMO uvicorn api.server:app --port 8000
```

---

## 7. Kill switch (test before every demo session)

```bash
curl -X POST "http://localhost:8000/kill-switch/engage?reason=<why>&close_positions=false"
curl http://localhost:8000/kill-switch/status
curl -X POST "http://localhost:8000/kill-switch/disengage?operator=<you>"
```

Behavior when engaged: new entries blocked immediately; open-position monitoring continues; pending entry orders cancelled; positions closed **only** if `close_positions=true` was passed explicitly. State persists across restarts, and a corrupted state file fails **closed** (engaged), never open.
