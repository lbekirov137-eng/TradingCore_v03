# TradingCore — Cloud Paper Monitor Runbook

**Scope: PAPER / DEMO dry-run monitoring only. Real trading is impossible in this codebase — see `AUTOTRADING_RELEASE_GATES.md`, Gate G.**

This runbook covers running the cloud paper-forward monitor for honest statistics collection. **ORB and VWAP are RESEARCH_ONLY / NOT_PRODUCTION_APPROVED** (see `AUTOTRADING_BACKTEST_REPORT.md`) — this deployment exists to collect forward-looking paper statistics for future strategy rework, not to demonstrate a working trading system.

---

## 1. Entry point

The real, actual entry point is `api/server.py`, served via `uvicorn`. (`main.py` at the repo root is a separate one-shot CLI script — it is **not** the cloud entry point and must never be used as one; the `Dockerfile` used to point at it by mistake, which has been fixed.)

---

## 2. Exact local start command

```bash
# from the repo root, with the project venv active
.venv/Scripts/python.exe -m uvicorn api.server:app --host 0.0.0.0 --port 8000
```

Or via Docker (same command the container runs):

```bash
docker build -t tradingcore-paper .
docker run -p 8000:8000 --env-file .env tradingcore-paper
```

To actually run the continuous monitor (not just answer HTTP requests), set `ENABLE_CLOUD_MONITOR=true` in the environment before starting.

---

## 3. Exact Railway start command

Railway builds from the `Dockerfile` and uses the `startCommand` in `railway.toml`:

```
uvicorn api.server:app --host 0.0.0.0 --port $PORT
```

`$PORT` is injected automatically by Railway — do not hardcode a port in Railway variables.

---

## 4. Required environment variables

| Variable | Required? | Safe value | Purpose |
|---|---|---|---|
| `TRADING_ENVIRONMENT` | Recommended | `PAPER` | `PAPER` or `DEMO` only. Anything else (including `LIVE` or a typo) refuses to start. |
| `LIVE_TRADING` | Recommended | `false` (or unset) | Must never be `true`. |
| `PAPER_TRADING` | Optional | `true` | Cosmetic/reporting flag; paper execution is structural, not flag-gated. |
| `DEMO_ONLY` | Optional | `true` | Documents intent; enforced structurally regardless. |
| `ENABLE_CLOUD_MONITOR` | **Required for actual monitoring** | `true` | Without this, the service answers HTTP but never ticks on its own. |
| `BYBIT_DEMO_API_KEY` / `BYBIT_DEMO_API_SECRET` | Optional | leave empty | Only used if `TRADING_ENVIRONMENT=DEMO`. If absent, the system runs on public market-data endpoints only — this is a fully supported, safe mode. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional | leave empty | Only the mock transport exists; nothing is actually sent regardless. |

See `.env.example` for the full, current template with inline explanations.

### Forbidden — do not set these under any circumstances

```
LIVE_TRADING=true            # application refuses to start
TRADING_ENVIRONMENT=LIVE     # application refuses to start
TRADING_ENVIRONMENT=mainnet  # unrecognized value, refuses to start (fail-closed)
any real production exchange API key
any LEVERAGE/MARGIN variable  # not supported; leverage is always 1, hardcoded
```

---

## 5. How to check `/health`

```bash
curl https://<your-railway-url>/health
```

Expected shape:
```json
{
  "status": "HEALTHY",
  "mode": "PAPER",
  "paper_trading": true,
  "live_trading": false,
  "demo_only": true,
  "monitor_running": true,
  "last_candle_timestamp_ms": 1785220800000,
  "data_feed_state": "OK",
  "open_virtual_positions": 0,
  "last_cycle_timestamp": 1785226364.32,
  "uptime_seconds": 1234.5,
  "server_time": 1785226400.1
}
```

- `status: STARTING` — no cycle has completed yet (normal right after boot).
- `status: HEALTHY` — recent cycle completed.
- `status: DEGRADED` — no cycle in longer than `MAX_DATA_AGE_SECONDS` (900s default) — investigate.
- `data_feed_state: STALE` — last candle is older than the freshness threshold — check exchange connectivity.
- `monitor_running: false` with `ENABLE_CLOUD_MONITOR=true` set — the background thread died; check logs and restart.

Other useful endpoints:
```bash
curl .../safety                    # live_trading/paper_trading/kill-switch summary
curl .../strategies/status         # confirms ORB/VWAP are RESEARCH_ONLY
curl .../observability/risk        # current risk-guard state
curl .../observability/pnl         # realized PnL summary
curl .../observability/position    # current open virtual position, if any
curl .../kill-switch/status
```

---

## 6. How to check logs

Railway: **Project → Deployments → View Logs**, or via CLI:
```bash
railway logs
```

Look for:
- `[STARTUP SAFETY] OK: {...}` — confirms the safety gate passed and shows the safe (non-secret) startup summary.
- `[STARTUP SAFETY] REFUSED TO START: ...` — the deployment refused to start; the message explains exactly why (e.g. `LIVE_TRADING=true detected`).
- Structured JSON log lines (one per tick) from the observability logger, containing `state`, `message`, `decision`, `execution` — never secret values.

Application-level logs are also queryable via `GET /observability/logs?limit=N`.

---

## 7. How to stop the service

- **Railway:** Project → Settings → pause/remove the service, or `railway down`.
- **Local Docker:** `docker stop <container>` (or `docker-compose down` if using compose).
- **Local process:** send SIGINT (Ctrl+C) or SIGTERM — the app has a lifespan shutdown handler that stops the monitor thread gracefully.
- **Without stopping the process:** engage the kill switch to halt new entries while keeping the service and its `/health` endpoint reachable:
  ```bash
  curl -X POST ".../kill-switch/engage?reason=<why>"
  ```

---

## 8. How to export paper-forward results

```bash
curl "https://<your-railway-url>/paper-forward/journal?limit=5000" -o paper_forward_export.json
```

Each entry contains: timestamp, exchange, symbol, timeframe, strategy (+ version + status), market regime, signal, TRADE/NO_TRADE + reason, virtual entry/stop/take-profit, position size, assumed fees/slippage, virtual net PnL, drawdown %, signal/trade IDs, and the code commit hash the decision was made under. Every NO_TRADE and TRADE candidate is included, not just fills.

For a full PnL/risk snapshot rather than the raw event log:
```bash
curl .../observability/pnl
curl .../observability/risk
```

---

## 9. Known limitations (read before relying on this deployment)

- **ORB and VWAP are RESEARCH_ONLY.** Both were independently backtested and are consistently, robustly unprofitable (see `AUTOTRADING_BACKTEST_REPORT.md`). This deployment exists to gather honest forward statistics for future rework, not to run a working strategy.
- Class-level state (`PositionManager`, all risk guards, the paper broker) is **not multi-process safe**. Keep `numReplicas = 1` in `railway.toml` — do not scale this service horizontally.
- The Bybit Demo adapter has never contacted the real API; if `TRADING_ENVIRONMENT=DEMO` is used with real demo credentials, expect the first connection to surface integration issues.
- No WebSocket client exists; market data is polled via REST on each cycle.
