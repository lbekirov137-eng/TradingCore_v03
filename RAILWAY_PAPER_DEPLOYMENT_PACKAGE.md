# Railway PAPER Deployment Package

**Branch:** `reconcile-railway-paper` @ `5933ecf`
**Status:** Prepared, **NOT deployed.** No deploy will occur without separate confirmation.
**LIVE trading:** blocked, unconditionally.

---

## 1. Package contents

| File | Lines | Purpose |
|---|---|---|
| `Dockerfile` | 32 | Build + single entrypoint |
| `railway.toml` | 18 | Builder, start command, healthcheck, replica policy |
| `.dockerignore` | 40 | Excludes `.venv` (203 MB Windows venv), `state/`, `data/`, `reports/`, `.git/` |
| `.env.example` | 80 | Variable template, no secret values |
| `requirements.txt` | 14 | Runtime deps — **no paid AI package** |

---

## 2. `railway.toml`

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "uvicorn api.server:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
numReplicas = 1
```

`numReplicas = 1` is **required**, not cosmetic: `PaperPositionManager`, the risk guards and the paper broker hold in-process state. Two replicas would each keep an independent "one open position" counter and silently double exposure.

---

## 3. `Dockerfile`

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

The base branch ran `python -u /app/paper_watchdog.py`. That was replaced with the web server because only `api/server.py` carries the startup safety gate and serves `/health` (which Railway's healthcheck requires). `paper_watchdog.py` and `paper_live_loop.py` are **retained**, not deleted, for local/background use.

---

## 4. Complete environment variable list

Every variable the project code actually reads (`.venv` excluded from the scan):

### Required for PAPER

| Variable | Value | Effect if omitted |
|---|---|---|
| `TRADING_ENVIRONMENT` | `PAPER` | Defaults to `PAPER` — safe |
| `LIVE_TRADING` | `false` | Defaults to `false` — safe |
| `PAPER_TRADING` | `true` | Defaults to `true` — safe |
| `DEMO_ONLY` | `true` | Defaults to `true` — safe |

Set all four explicitly anyway, so the intended mode is visible in the Railway dashboard rather than implied.

`PORT` is injected by Railway automatically — **do not set it manually.**

### Optional — leave unset for the first run

| Variable | Default | Notes |
|---|---|---|
| `TRADING_EXECUTION_MODE` | `SPOT_LONG_ONLY` | Only other legal value is `PAPER_LONG_SHORT`, which enables paper SHORT. **Leave unset.** |
| `PAPER_STARTING_BALANCE` | code default | Virtual balance |
| `PAPER_JOURNAL_PATH` | code default | Journal location |
| `AI_OPENAI_SHADOW_ENABLED` | `false` | Enables the paid AI layer. **Leave unset.** |
| `OPENAI_API_KEY` | — | Not needed; `openai` is not installed |
| `OPENAI_MODEL` | `gpt-5-mini` | Irrelevant while AI is off |
| `AI_FILTER_APPROVE_SCORE` | code default | AI layer only |
| `AI_FILTER_REVIEW_SCORE` | code default | AI layer only |
| `AI_FILTER_INPUT_FILE` | code default | AI layer only |
| `AI_FILTER_OUTPUT_FILE` | code default | AI layer only |
| `TELEGRAM_BOT_TOKEN` | — | See §6 |
| `TELEGRAM_CHAT_ID` | — | See §6 |

### Forbidden — must never be set

```
LIVE_TRADING=true               → process refuses to start
TRADING_ENVIRONMENT=LIVE        → process refuses to start
TRADING_ENVIRONMENT=<anything unrecognized>  → refuses to start (fail-closed)
TRADING_EXECUTION_MODE=PAPER_LONG_SHORT      → enables paper SHORT; not approved for first run
AI_OPENAI_SHADOW_ENABLED=true   → activates paid API
any real production exchange API key
```

---

## 5. Which values are mandatory

Strictly mandatory: **none** — every safety default is correct with an empty environment, which is itself the point (a forgotten variable cannot create an unsafe state).

Mandatory *as practice*: the four PAPER variables in §4, set explicitly for auditability.

---

## 6. Secrets — Railway-only, never in the repo

| Secret | Needed now? | Where |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Optional | Railway Variables only |
| `TELEGRAM_CHAT_ID` | Optional | Railway Variables only |
| `OPENAI_API_KEY` | **No** | Not needed; do not add |
| Exchange API keys | **No** | Market data uses public unauthenticated endpoints |

Verified: no secret values exist anywhere in the repository, `.env` is gitignored and absent, and `.env.example` contains only empty assignments. Enforced continuously by `tests/test_security_hygiene.py`.

---

## 7. PAPER mode persists after deployment — verified

Booted with the exact Railway variable set:

```
[STARTUP SAFETY] OK: {'trading_environment': 'PAPER', 'live_trading': False,
                      'paper_trading': True, 'demo_only': True, 'leverage': 1,
                      'live_order_code_present': False}

/safety : {"trading_environment":"PAPER","paper_trading":true,"live_trading":false,
           "demo_only":true,"max_leverage":1,"max_risk_percent":0.001,
           "live_order_code_present":false}
/health : 200 PAPER
/ready  : 200 READY
```

Three independent layers keep it that way:

1. **Startup gate** (`config/startup_safety.py`) — runs at import, before uvicorn binds the port. `LIVE_TRADING=true`, `TRADING_ENVIRONMENT=LIVE`, or any unrecognized mode aborts the process. Both refusal paths tested live.
2. **Runtime re-check** — `/ready` re-validates on every call and returns **503 FAILED_SAFELY** on unsafe config, leverage > 1, or risk > 0.1%. Verified by simulating `MAX_LEVERAGE=3` → 503 naming the exact violation.
3. **Safe defaults** — an empty environment yields PAPER; unsafe states require deliberate action.

---

## 8. Real orders are structurally impossible — verified

Not a flag, a structural property:

```
order-placement methods: NONE
```

Introspection of `BinanceAPI` and `BybitAPI` for `create_order`, `place_order`, `new_order`, `submit_order`, `cancel_order`, `post_order` returns nothing. **No method exists to call.** Both clients implement only read-only `GET` endpoints (klines, ticker, orderbook, server time) with no request signing.

Supporting constraints:

| Constraint | Verified value |
|---|---|
| `MAX_LEVERAGE` | `1` |
| `MAX_RISK_PERCENT` | `0.001` (0.1%) |
| `DEFAULT_EXECUTION_MODE` | `SPOT_LONG_ONLY` |
| SELL / SHORT | opt-in only, gated in both `RiskStep` and `PaperExecutionStep` |
| `real_order_sent` in paper path | hardcoded `False` |

Locked by `tests/test_leverage_cap.py` (8 tests) and `tests/test_cloud_paper_entrypoint.py` (15 tests).

---

## 9. Telegram notifications — verified inert

`ai_observer/telegram_notifier.py` posts to `api.telegram.org` **only if both** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.

I checked the guard specifically, because a common bug here is writing `if not self.configured:` where `configured` is a plain method — a bound method is always truthy, so the guard would never fire and it would attempt an HTTP call with an empty token. **`configured` is correctly decorated `@property`**, so the guard works. Confirmed empirically:

```
configured (property): False
send() returned: False   → short-circuited before any network call
```

Decision for the first run: **leave both unset.** Notifications are optional; adding them later is a one-variable change requiring no redeploy of code.

---

## 10. Railway deployment checklist

### Before deploying
- [ ] Branch `reconcile-railway-paper` reviewed (it is **not** pushed — see Blockers)
- [ ] `pytest tests/ -q` run locally
- [ ] `git status` clean, no `.env` present
- [ ] Confirm `numReplicas = 1` in `railway.toml`
- [ ] Confirm `Dockerfile` CMD is `uvicorn api.server:app`, not `main.py` or `paper_watchdog.py`

### Railway project setup
- [ ] New Project → Deploy from GitHub repo → select branch
- [ ] Confirm builder auto-detects `Dockerfile` / `railway.toml`
- [ ] Add the 4 PAPER variables (§4). Add nothing else.
- [ ] Do **not** set `PORT`
- [ ] Do **not** set `TRADING_EXECUTION_MODE`, `AI_OPENAI_SHADOW_ENABLED`, or any API key

### Immediately after deploy
- [ ] Logs show `[STARTUP SAFETY] OK: {...}` — not `REFUSED TO START`, not silence
- [ ] `GET /health` → 200, `"mode": "PAPER"`, `"live_trading": false`
- [ ] `GET /ready` → 200, `"status": "READY"`, `"reasons": []`
- [ ] `GET /safety` → `max_leverage: 1`, `max_risk_percent: 0.001`
- [ ] `GET /strategies/status` → all `RESEARCH_ONLY`
- [ ] No crash-loop / repeated restarts

### First 24 hours
- [ ] `/health` still 200 after one full candle interval
- [ ] No secret values in logs (spot-check)
- [ ] Journal accumulating entries
- [ ] Memory/CPU stable

### Abort conditions — stop immediately
- [ ] `/safety` ever reports `live_trading: true`
- [ ] A real exchange order ID appears anywhere
- [ ] A secret value appears in logs or an endpoint
- [ ] `/ready` returns 503 `FAILED_SAFELY` — read `reasons` before restarting

---

## Blockers before deployment

| # | Blocker | Owner |
|---|---|---|
| 1 | Branch is **not pushed** — 8 commits local only. Requires your authorization. | You |
| 2 | `docker build` **never executed** — Docker daemon unavailable here. The `uvicorn` command inside was verified on the host; the image build was not. | You / CI |
| 3 | 31 pre-existing test failures remain (24 = `selected_trade` contract migration, not yet approved for fixing) | You |

---

## GO / NO-GO

| Item | Status |
|---|---|
| Package completeness | 🟢 **GO** |
| PAPER safety verified | 🟢 **GO** |
| Real orders impossible | 🟢 **GO** (structural) |
| Telegram | 🟢 GO (inert, optional) |
| **Deploy now** | ⏸️ **HOLD** — awaiting your confirmation; branch unpushed and image unbuilt |
| **LIVE trading** | 🔴 **NO-GO** — unconditional |

Nothing has been deployed. No push performed.
