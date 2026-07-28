# Railway PAPER Deployment — Step-by-Step

**Scope: CLOUD PAPER mode only.** Live trading is structurally impossible in this codebase (no order-placement code exists) and additionally blocked by a startup safety gate that refuses to run if `LIVE_TRADING=true` or `TRADING_ENVIRONMENT` is anything other than `PAPER`/`DEMO`.

This document is for the **user** to execute manually. Nothing here has been pushed or deployed automatically.

---

## Step 0 — Prerequisites

- A GitHub account with push access to `https://github.com/lbekirov137-eng/TradingCore_v03.git`
- A Railway account (free tier is sufficient for this — no paid service is required)
- No exchange API keys are required for PAPER mode. Leave `BYBIT_DEMO_API_KEY`/`BYBIT_DEMO_API_SECRET` empty unless you specifically want `TRADING_ENVIRONMENT=DEMO`.

---

## Step 1 — Push to GitHub (manual, you run this)

Review the diff one more time yourself if you'd like, then:

```bash
git push origin main
```

This pushes 6 local commits (`e8a30bf` and everything before it back to `3a7f055`) — 180 tracked files total, none of which contain secrets (verified — see `CLOUD_PAPER_READINESS_REPORT.md` §3).

---

## Step 2 — Create the Railway project

1. Railway dashboard → **New Project** → **Deploy from GitHub repo**.
2. Select `TradingCore_v03`, branch `main`.
3. Railway will detect the `Dockerfile` and `railway.toml` automatically (builder = `DOCKERFILE`, healthcheck = `/health`, `numReplicas = 1`).

---

## Step 3 — Set environment variables

In Railway → your service → **Variables**, add exactly:

```
TRADING_ENVIRONMENT=PAPER
LIVE_TRADING=false
PAPER_TRADING=true
DEMO_ONLY=true
ENABLE_CLOUD_MONITOR=true
```

Do **not** add `BYBIT_DEMO_API_KEY` / `BYBIT_DEMO_API_SECRET` unless you intend to use `TRADING_ENVIRONMENT=DEMO` with your own Bybit demo credentials — in pure `PAPER` mode they are unused, and the system runs on public market-data endpoints only, which is the safe, fully-supported default.

See `CLOUD_PAPER_RUNBOOK.md` §4 for the complete variable reference and the explicit forbidden-values list.

---

## Step 4 — Deploy

Railway deploys automatically once variables are saved and the build completes. Watch the build logs; you should see, near the start of the application logs (not the build logs):

```
[STARTUP SAFETY] OK: {'trading_environment': 'PAPER', 'live_trading': False, ...}
```

If you instead see:

```
[STARTUP SAFETY] REFUSED TO START: ...
```

the deployment will crash-loop — this is the intended, safe behavior for a misconfigured environment. Fix the variable named in the message and redeploy.

---

## Step 5 — Verify it's alive

```bash
curl https://<your-railway-domain>/health
curl https://<your-railway-domain>/ready
curl https://<your-railway-domain>/safety
curl https://<your-railway-domain>/strategies/status
```

Expected: `/health` and `/ready` return `200`, `/ready`'s body has `"status": "READY"`, `/safety` shows `"live_trading": false`, `/strategies/status` shows both strategies as `RESEARCH_ONLY`.

Continue with `RAILWAY_PAPER_POST_DEPLOY_CHECKLIST.md` for the full post-deploy verification.

---

## What this deployment does NOT do

- It does not place real orders (no code path exists for this).
- It does not connect to any exchange with real credentials unless you explicitly configure Bybit demo keys.
- It does not claim ORB or VWAP are profitable — both are marked `RESEARCH_ONLY` and are being run specifically to collect honest forward statistics for a future rework, per the accepted decision that neither is currently viable.

## Manual confirmation required from you before proceeding

- [ ] You have reviewed and are pushing from your own GitHub credentials (this session never touches your git credentials).
- [ ] You have decided whether to use pure `PAPER` mode or `DEMO` mode with your own Bybit demo keys.
- [ ] You understand `numReplicas` must stay at `1` (documented limitation — shared in-memory state is not multi-process safe yet).
