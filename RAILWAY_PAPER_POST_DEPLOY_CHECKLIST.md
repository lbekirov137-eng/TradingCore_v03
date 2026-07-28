# Railway PAPER Deployment — Post-Deploy Checklist

Run through this immediately after every deploy, and periodically during observation.

**Standing rule (enforced in code, verified below):** if the service detects an unknown/unrecognized configuration, stale market data, corrupted local state, or any attempt to enable live mode, it must stop as **FAILED_SAFELY** rather than continue operating silently. This is checked twice — once at process startup (`config/startup_safety.py::assert_safe_startup`) and continuously at runtime on every monitor tick and via `/ready` (`runtime_safety_check`).

---

## 1. Check that it actually started

```bash
curl https://<domain>/health
```
- [ ] HTTP `200`
- [ ] `"mode": "PAPER"`
- [ ] `"live_trading": false`
- [ ] `"status"` is `STARTING` (just booted) or `HEALTHY` (running normally) — **not** silence/timeout

```bash
curl https://<domain>/ready
```
- [ ] HTTP `200` and `"status": "READY"`, `"reasons": []`
- [ ] If instead HTTP `503` with `"status": "FAILED_SAFELY"` — read the `reasons` array, it names exactly what's wrong (unsafe config / stale data / corrupted state)

## 2. Check logs

Railway → Deployments → Logs, or `railway logs`.
- [ ] `[STARTUP SAFETY] OK: {...}` appears near the top of application logs
- [ ] No `[STARTUP SAFETY] REFUSED TO START` message
- [ ] No repeated crash-loop restarts
- [ ] Structured JSON log lines appear once `ENABLE_CLOUD_MONITOR=true` is set and a candle interval has passed
- [ ] No secret values (API keys, tokens) appear anywhere in the log output — spot-check a few lines

## 3. Check the last candle / data feed

```bash
curl https://<domain>/health
```
- [ ] `last_candle_timestamp_ms` is non-null after the first monitor cycle
- [ ] `data_feed_state` is `OK` (not `STALE` or `UNKNOWN`) after at least one full candle interval has passed
- [ ] `last_cycle_timestamp` updates on repeated calls a few minutes apart (proves the monitor is actually ticking, not stuck)

```bash
curl https://<domain>/observability/clock-skew?exchange=binance
```
- [ ] `skewed: false` (host clock is not meaningfully off from the exchange)

## 4. Check PAPER mode is actually active

```bash
curl https://<domain>/safety
```
- [ ] `"paper_trading": true`
- [ ] `"live_trading": false`
- [ ] `"live_order_code_present": false`

```bash
curl https://<domain>/strategies/status
```
- [ ] Both `ORB` and `VWAP_TREND_PULLBACK` show `"status": "RESEARCH_ONLY"` and `"production_approved": false`

## 5. Check no order-placement occurs

- [ ] `GET /paper-forward/journal?limit=20` — every entry's `execution_status` is one of `NO_TRADE`, `OPENED`, `CLOSED`, `FAILED_SAFELY`, `ORDER_PENDING` — never a real exchange order ID format from Binance/Bybit
- [ ] No outbound requests to any `POST /order` / `/v5/order/create`-style endpoint appear in logs (there is no code path that would produce one — this is a structural guarantee, not just a runtime observation — see `CLOUD_PAPER_READINESS_REPORT.md` §6)
- [ ] If `TRADING_ENVIRONMENT=DEMO` was used: confirm on the Bybit Demo Trading dashboard itself that any activity shown there is demo-account activity, not mainnet

## 6. Check persistence / restart behavior

1. Note the current `open_virtual_positions` and the most recent `trade_id` from `/health` and `/paper-forward/journal`.
2. Trigger a restart (Railway → Redeploy, or a deliberate crash test).
3. After restart, re-check:
   - [ ] `open_virtual_positions` is consistent with before (not duplicated, not silently reset to a phantom state)
   - [ ] `/paper-forward/journal` does not show a duplicate `trade_id` for the same signal being opened twice
   - [ ] `/kill-switch/status` reflects its pre-restart engaged/disengaged state correctly (state is disk-persisted)
   - [ ] If any state file was corrupted by the crash, the affected component fails **closed**: kill switch defaults to `engaged`, position manager defaults to flat (no phantom position) — never fails open

## 7. Emergency stop procedure

**To halt new entries immediately while keeping the service and its `/health`/`/ready` endpoints reachable:**
```bash
curl -X POST "https://<domain>/kill-switch/engage?reason=<why>&operator=<you>"
curl https://<domain>/kill-switch/status   # confirm engaged: true
```
This stops new entries, keeps monitoring any open virtual position, and cancels pending entry orders. It does **not** close an open position unless you explicitly pass `close_positions=true`.

**To fully stop the service:**
- Railway → pause or remove the service, or
- `railway down`

**To recover after an emergency stop:**
```bash
curl -X POST "https://<domain>/kill-switch/disengage?operator=<you>"
```
Only do this after understanding why it was engaged in the first place.

**If `/ready` reports `FAILED_SAFELY`:** do not disengage/restart blindly — read the `reasons` field first. If it says `unsafe_configuration`, fix the named environment variable before redeploying. If it says `stale_market_data`, check exchange connectivity. If it says `corrupted_state`, export the journal (`GET /paper-forward/journal?limit=5000`) before taking any further action, then investigate the underlying state file.
