# mt5-bridge — MT5 terminal in Docker + ZMQ bridge to Python bot

Native MT5 terminal running in Wine/Docker on the existing DO droplet. Python bot
talks to the terminal via ZeroMQ sockets (sub-10ms local IPC) — no external API,
no OAuth, no third-party latency.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  DO Droplet 146.190.17.26                                     │
│                                                               │
│  trading-bot  ◄── ZMQ (5555/5556) ──► mt5-bridge              │
│  (Python)                              (Ubuntu+Wine+MT5+EA)   │
│                                             │                 │
│                                             ▼                 │
│                                    FusionMarkets-Live (MT5)  │
└──────────────────────────────────────────────────────────────┘
```

## Phase 1 — Proof of life (CURRENT)

Goal: boot MT5 terminal in Docker, confirm login to FusionMarkets-Live MT5 account
429608, see balance and heartbeat in logs.

**No trading yet. No Python integration yet.** Just proves Wine+MT5 works.

## Deployment (first run)

### 1. Upload files to droplet

From Mac:
```bash
cd "/Users/macbookpro/Downloads/project bolt/trading-backend"
scp -r mt5-bridge root@146.190.17.26:/opt/trading-backend/
```

### 2. Add MT5 credentials to `.env`

On droplet:
```bash
ssh root@146.190.17.26
cd /opt/trading-backend
nano .env
```

Add these lines (use the NEW password after rotation):
```
MT5_LOGIN=429608
MT5_PASSWORD=<new password>
MT5_SERVER=FusionMarkets-Live
```

### 3. Build the container

```bash
cd /opt/trading-backend
docker compose build mt5-bridge
```

Expected build time: 5-10 min (Wine + MT5 download).

### 4. Start mt5-bridge

```bash
docker compose up -d mt5-bridge
docker logs -f trading-mt5-bridge
```

### 5. Expected success output

Within 60-120s, logs should show:
```
[mt5-bridge] MT5_LOGIN=429608
[mt5-bridge] MT5_SERVER=FusionMarkets-Live
[mt5-bridge] Starting supervisord...
[ZmqBridge] EA initialized. Terminal=MetaTrader 5 Account=429608 ...
[ZmqBridge] heartbeat balance=192.89 equity=192.89 positions=0 connected=true
```

**If you see `balance=192.89`**, Phase 1 is validated.

## Troubleshooting (Phase 1 failures)

### MT5 installer hangs

Wine+MT5 silent install sometimes fails. Fallback = manual install via VNC:

```bash
# From your Mac, tunnel port 5900:
ssh -L 5900:localhost:5900 root@146.190.17.26

# In another terminal, open Finder → Cmd+K → vnc://localhost:5900
# You'll see the MT5 installer GUI. Click through.
```

### MT5 terminal opens but login fails

Check logs: `docker logs trading-mt5-bridge | grep -i "login\|fail"`.

Common causes:
- Wrong server name (must be exactly `FusionMarkets-Live` — no typo)
- Password containing `$` or `!` (bash expansion in .env — use different symbols)
- Account inactive/frozen on broker side

### Wine crashes

Check RAM: `free -h` on droplet. If swap usage >50%, MT5 is OOM-ing.
Workaround: stop trading-bot container while debugging: `docker stop trading-bot`.

## Phase 2 — Python bridge (DONE)

- `mt5-bridge/ea/ZmqBridge.mq5` full version (PUB ticks + REP orders)
- `app/trading/mt5_client.py` (Python ZMQ client)
- `docker-compose.yml` integration

## Phase 3 — Bot integration (DONE)

- `self.mt5` is the single broker client in `app/trading/bot.py`
- `max_order_size`, leverage and pyramid/trail params set in `app/config.py`
- 24h validation run on demo account before live
