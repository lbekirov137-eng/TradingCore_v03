# TradingCore — Cloud Paper Deployment Checklist

Complete top-to-bottom before deploying, and re-check the starred (★) items after every deploy.

## Before deploying

- [ ] `git status` reviewed — no unintended files staged
- [ ] Full test suite green: `.venv/Scripts/python.exe -m pytest tests/ -q`
- [ ] Smoke suite green: `.venv/Scripts/python.exe -m pytest tests/smoke -v`
- [ ] `.env` (if used locally) is **not** committed — confirmed via `git status`
- [ ] No real API keys pasted anywhere in chat, code, or `.env.example`
- [ ] Railway project Variables reviewed — only variables from `.env.example` are set, with **safe** values
- [ ] ★ `TRADING_ENVIRONMENT` is `PAPER` or `DEMO` — never `LIVE`, never a typo
- [ ] ★ `LIVE_TRADING` is unset or `false`
- [ ] `ENABLE_CLOUD_MONITOR=true` set (if continuous monitoring is desired — omit only for a request-only deployment)
- [ ] `railway.toml` still has `numReplicas = 1` (multi-process is unsafe — see limitations)
- [ ] `Dockerfile` `CMD` still points at `uvicorn api.server:app`, not `main.py`

## Immediately after deploying

- [ ] ★ `GET /health` returns `200` and `"live_trading": false`
- [ ] ★ `GET /safety` confirms `live_order_code_present: false`
- [ ] `GET /strategies/status` confirms both strategies are `RESEARCH_ONLY`
- [ ] Logs show `[STARTUP SAFETY] OK: {...}` (not a refusal, not silence)
- [ ] `monitor_running: true` in `/health` if `ENABLE_CLOUD_MONITOR=true` was set
- [ ] `/kill-switch/status` reachable and returns `{"engaged": false}` (or intentionally `true`)
- [ ] Wait one candle interval, re-check `/health` — `data_feed_state` should become `OK` and `last_candle_timestamp_ms` should be recent

## Before trusting any collected data

- [ ] Confirm `strategy_version` and `code_commit_hash` in exported journal entries match the deployed commit
- [ ] Confirm no gaps in the journal beyond expected restarts (check `signal_id`/`trade_id` continuity)
- [ ] Cross-check `/observability/pnl` against a manual review of a few journal entries

## If anything looks wrong

- [ ] Engage the kill switch immediately: `POST /kill-switch/engage?reason=<why>`
- [ ] Pull logs before restarting (Railway logs are not infinitely retained)
- [ ] Export the journal before any destructive action: `GET /paper-forward/journal?limit=5000`

## Absolute non-negotiables (never true, ever)

- [ ] `live_trading` is `true` anywhere → **stop immediately, this should be structurally impossible**
- [ ] Any real order ID from a real exchange appears in logs or the journal → **stop immediately, this should be structurally impossible**
- [ ] A secret value (API key/token) appears in `/health`, `/safety`, logs, or the journal → **stop immediately, rotate the secret, investigate**
