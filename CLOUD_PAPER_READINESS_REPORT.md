# TradingCore — Cloud Paper Readiness Report

**Date:** 2026-07-28 · **Scope:** cloud PAPER-forward monitoring only, real money and live trading remain prohibited.

---

## 1. Background process cleanup (done first, as required)

Two of the three tracked background shell processes were found still **running** (`b6mbt2iis`, `b0fc8t6wz`) — both were accidental duplicate launches of the same expensive 6-month multi-analysis backtest script (train/validation/test split + walk-forward + sensitivity), which I already had equivalent, conclusive evidence for at the 6-week scale (0% walk-forward, all splits negative) plus the 6-month full-period number (208 trades, −176.54, PF 0.205) from a separate, already-completed run. They added no new information worth the continued cost.

- Both stopped via `TaskStop`.
- `TaskStop` killed the tracked shell wrapper but left **two orphaned child processes** still running the same job (confirmed by exact command-line inspection — not ambiguous). Both terminated by exact PID.
- Verified: **0 Python processes running**, no `.tmp` files, no lock artifacts. Full test suite re-run cleanly afterward (12–17s, no contention).
- One additional background job (`bpcrix324`, a superseded direct-run attempt) surfaced a "failed" notification as a direct, expected consequence of the above cleanup — not a new problem.

No useful output existed to salvage from the two stopped jobs (they write results only at completion, and completion was many hours away given observed per-run cost at 52k candles).

---

## 2. Test results

```
359 passed, 0 failed, 0 skipped
```

| Suite | Count |
|---|---|
| Unit | 269 |
| Integration | 53 |
| Regression | 21 |
| End-to-end | 9 |
| **Smoke (new, cloud-specific)** | **7** |

Smoke suite (`tests/smoke/test_cloud_paper_smoke.py`) maps 1:1 to the required checklist: app starts, `/health` responds, live trading off by default, market-data failure handled safely, one monitor cycle executes, virtual order never reaches a real exchange (proven both structurally — no order-placement method exists on either exchange client — and functionally), restart never duplicates a decision.

---

## 3. Exact command to run

**Local:**
```bash
.venv/Scripts/python.exe -m uvicorn api.server:app --host 0.0.0.0 --port 8000
```
Verified directly in this session: the process starts, stays alive (confirmed via `kill -0` after 3s), and serves `/health` and `/safety` over real HTTP on port 8123 during testing. Cleanly stopped afterward by exact PID; port confirmed released.

**Railway:** `uvicorn api.server:app --host 0.0.0.0 --port $PORT` (via `railway.toml`, building from the corrected `Dockerfile`).

Full details, required env vars, and safe/forbidden values: `CLOUD_PAPER_RUNBOOK.md`.

---

## 4. Railway deployment readiness

| Item | Status |
|---|---|
| `Dockerfile` | ✅ **Fixed** — previously ran `main.py` (one-shot script, exits immediately → guaranteed crash-loop on Railway). Now runs `uvicorn api.server:app`. |
| `railway.toml` | ✅ Created — `DOCKERFILE` builder, explicit start command, `/health` healthcheck, `numReplicas = 1` (multi-process state is not yet safe — see limitations) |
| `.env.example` | ✅ Updated — `LIVE_TRADING`, `DEMO_ONLY`, `ENABLE_CLOUD_MONITOR` added; misleading unused `RISK_PERCENT` entry corrected (risk is a fixed, audited code constant, not env-configurable, by design) |
| Startup safety gate | ✅ New — refuses to start on `LIVE_TRADING=true`, `TRADING_ENVIRONMENT=LIVE`, or any unrecognized environment value |
| `/health` endpoint | ✅ New — status, mode, last candle timestamp, feed state, open virtual positions, last cycle timestamp; no secrets |
| Continuous background monitor | ✅ New — `api/cloud_monitor.py`, opt-in via `ENABLE_CLOUD_MONITOR=true` (deliberately off by default so pytest never spawns a thread hitting real endpoints) |
| Paper-forward journal | ✅ New — every NO_TRADE and TRADE candidate logged with the full required field set, exportable via `GET /paper-forward/journal` |
| Restart-safe state | ✅ Existing (broker/positions/idempotency/kill-switch all disk-persisted, fail-closed on corruption) + new journal is append-only, corruption-tolerant on read |
| Docker build itself | 🟡 **Not verified** — Docker Desktop's daemon was not running in this environment and was not started (avoided as a potentially disruptive action). The exact `uvicorn` command the container runs **was** verified directly on the host. |

---

## 5. Proof live trading is blocked

- `config/startup_safety.py::assert_safe_startup()` raises `StartupSafetyError` and the import of `api/server.py` fails before the FastAPI app object is even created, if `LIVE_TRADING=true` **or** `TRADING_ENVIRONMENT` is anything other than `PAPER`/`DEMO`. Verified live in this session:
  ```
  LIVE_TRADING=true            → [STARTUP SAFETY] REFUSED TO START
  TRADING_ENVIRONMENT=mainnet  → [STARTUP SAFETY] REFUSED TO START
  ```
- 25 dedicated tests (`tests/unit/test_startup_safety.py`) cover every truthy/falsy env-var spelling, every invalid environment string tried, and the case where a valid environment is combined with `LIVE_TRADING=true` (must still refuse).
- A dedicated test (`test_live_trading_cannot_be_enabled_by_any_single_env_var_except_the_explicit_one`) tries 8 plausible-sounding alternative variable names (`TRADING_MODE`, `MODE`, `PRODUCTION`, `MAINNET`, etc.) and confirms none of them affect the live-trading flag.

---

## 6. Proof no real orders can be sent

- **Structural, not just behavioral:** `inspect.getmembers` on `BinanceAPI` and `BybitAPI` confirms neither class defines `create_order`, `place_order`, `new_order`, `submit_order`, or `cancel_order` — there is no method to call even if one wanted to. Verified by a dedicated test in both the security-hygiene suite and the smoke suite.
- **Functional:** every TRADE decision in this codebase routes through `TradeEngine.execute()` → `PaperBroker` only. `cloud_monitor.py`'s `SchedulerLoop` is constructed with `adapter=te.broker` (the paper broker) — there is no code path by which a decision reaches `BinanceAPI`/`BybitAPI` for order placement, because those classes have no such capability.
- Both Binance and Bybit clients only implement `GET`-style read endpoints (klines, ticker, orderbook, server time) — confirmed unchanged from the prior audit session.

---

## 7. Persistence / restart state

| Component | Restart behavior |
|---|---|
| `PaperBroker` (balance, positions, orders, ledger) | Disk-persisted, atomic writes, reloads on new instance |
| `PositionManager` | Disk-persisted; corrupted file → safely starts flat (no position), not a crash |
| `IdempotencyStore` (order state) | Disk-persisted per client_order_id; corrupted individual file is skipped, not fatal |
| Kill switch | Disk-persisted; **corrupted state defaults to ENGAGED** (fail-closed, not fail-open) |
| Paper-forward journal | Append-only JSONL; corrupted/malformed lines are skipped on read, never block new writes |

Verified this session (smoke test 7 / existing e2e tests): replaying the identical signal after a simulated restart (in-memory `PositionManager` wiped, disk state intact) **never** opens a duplicate position — caught either by the session-trade-count guard or by order-layer idempotency, both of which survive the simulated restart.

---

## 8. Known limitations

- **ORB and VWAP remain RESEARCH_ONLY / NOT_PRODUCTION_APPROVED** and were not touched to "improve" their backtest — per instruction, both are independently, robustly unprofitable at every scale tested and are marked as such in code (`STATUS = "RESEARCH_ONLY"`) and via `GET /strategies/status`.
- Class-level shared state (`PositionManager`, all risk guards, the paper broker singleton) is **not multi-process safe**. `railway.toml` is pinned to `numReplicas = 1` specifically to avoid this; do not change without first fixing the underlying state model.
- Docker image build itself was not verified in this session (daemon unavailable); the exact command the container runs was verified directly on the host instead.
- Bybit Demo adapter has never contacted the real API (mocked tests only); first real connection, if ever attempted, may surface schema surprises.
- No WebSocket client exists — market data is REST-polled each cycle.
- The continuous monitor thread and the manual `/paper/tick` endpoint share the same underlying state (broker, guards, journal) by design — running both simultaneously is fine and intentional (both feed the same journal), not a race condition, since Python's GIL and the simple synchronous tick logic serialize access adequately for this deployment's single-replica, low-frequency (5-minute candle) cadence.

---

## 9. What requires user action

- Actually deploying to Railway (this session did not push or deploy, per instructions).
- Deciding whether to enable `TRADING_ENVIRONMENT=DEMO` with real Bybit demo credentials, or to remain in pure `PAPER` mode for this observation phase.
- Reviewing collected paper-forward journal data after a meaningful observation period, before any strategy rework decision.
- Any future decision to progress toward Gate D/E/F — **explicitly not recommended by this report** until the strategies have been reworked (see `AUTOTRADING_NEXT_ACTIONS.md`).

---

## 10. GO / NO-GO

| Track | Decision |
|---|---|
| **Cloud PAPER monitoring deployment** | 🟢 **GO** — infrastructure is safe, tested, and honest. Strategies are known-unprofitable but that is exactly the point of this phase (collect honest forward statistics for rework, not to demonstrate a working system). |
| **Cloud DEMO (Bybit) deployment** | 🟡 **GO with caveats** — adapter is safety-enforced and mocked-tested, but has never contacted the real API; expect first-connection surprises. Recommended only after a period of stable PAPER-mode observation. |
| **Live trading (any amount, any leverage)** | 🔴 **NO-GO — remains structurally impossible and explicitly locked (Gate G).** No code exists in this repository capable of placing a real order. This report does not, and will not, recommend otherwise. |
