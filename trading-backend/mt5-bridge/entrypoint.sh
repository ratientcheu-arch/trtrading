#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════════════════
# mt5-bridge entrypoint
# - Starts Xvfb (virtual X server)
# - Starts x11vnc (remote access on :5900 for first-time login)
# - Installs MT5 terminal on first run (if not already installed)
# - Launches MT5 terminal headless with auto-login
# - Supervisor keeps everything running
# ═══════════════════════════════════════════════════════════════════════════

log() { echo "[mt5-bridge $(date -u +%H:%M:%S)] $*"; }

# ── Sanity check env vars ──────────────────────────────────────────────────
: "${MT5_LOGIN:?MT5_LOGIN is required (e.g. 429608)}"
: "${MT5_PASSWORD:?MT5_PASSWORD is required}"
: "${MT5_SERVER:?MT5_SERVER is required (e.g. FusionMarkets-Live)}"

log "MT5_LOGIN=$MT5_LOGIN"
log "MT5_SERVER=$MT5_SERVER"
log "MT5_PASSWORD=<masked, ${#MT5_PASSWORD} chars>"

# ── Wine prefix init (first run only) ──────────────────────────────────────
if [ ! -d "$WINEPREFIX/drive_c/windows" ]; then
    log "Bootstrapping Wine prefix ($WINEPREFIX)..."
    wineboot --init
    wineserver -w
fi

# ── MT5 install (first run only) ───────────────────────────────────────────
# Fusion-branded installer creates folder "Fusion Markets MetaTrader 5"
MT5_TERMINAL_FUSION="$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5/terminal64.exe"
MT5_TERMINAL_GENERIC="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ ! -f "$MT5_TERMINAL_FUSION" ] && [ ! -f "$MT5_TERMINAL_GENERIC" ]; then
    log "Installing MT5 terminal via /install-mt5.sh..."
    /install-mt5.sh || {
        log "MT5 auto-install failed. Manual install required via VNC on :5900"
        log "Connect from your Mac: ssh -L 5900:localhost:5900 root@146.190.17.26"
        log "Then open 'vnc://localhost:5900' in Finder or a VNC client."
    }
fi

# ── Determine MT5 install dir (Fusion-branded vs generic) ──────────────────
if [ -d "$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5" ]; then
    MT5_DIR="$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5"
else
    MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
fi
log "MT5 install dir: $MT5_DIR"

# ── Generate config.ini INSIDE MT5 folder (auto-login for MT5 terminal) ────
# CRITICAL: MT5 is a Windows app and cannot resolve Unix paths like /config.ini
# passed via `/config:/config.ini`. By placing config.ini directly inside the
# MT5 install dir, we can reference it with the Windows path C:\Program Files\...
# which Wine passes through correctly. Must run on every container start because
# we want fresh credentials from env vars.
log "Generating config.ini inside MT5 folder with auto-login credentials..."
cat > "$MT5_DIR/config.ini" <<EOF
[Common]
Login=${MT5_LOGIN}
Password=${MT5_PASSWORD}
Server=${MT5_SERVER}
ProxyEnable=0
CertInstall=0
NewsEnable=0

[Experts]
AllowLiveTrading=1
AllowDllImport=1
Enabled=1
Account=0
Profile=0

[StartUp]
Expert=ZmqBridge
Symbol=EURUSD
Period=M1
EOF
chmod 600 "$MT5_DIR/config.ini"
log "config.ini written to $MT5_DIR/config.ini"

# Also ensure the EA is copied every time (volume might not have it if built
# with an older image). Idempotent: cp is no-op if already up-to-date.
if [ -f /ea/ZmqBridge.mq5 ]; then
    EA_DIR="$MT5_DIR/MQL5/Experts"
    mkdir -p "$EA_DIR"
    cp /ea/ZmqBridge.mq5 "$EA_DIR/"
    log "EA ZmqBridge.mq5 synced to $EA_DIR"
fi

# ── Sync mql-zmq (DLLs + patched headers) into MT5 MQL5 dirs ───────────────
# libzmq.dll + libsodium.dll → MQL5/Libraries (MT5 loads DLLs from here)
# libsodium.dll is a runtime dependency of libzmq.dll — without it, MT5 reports
# error 126 "cannot load libzmq.dll".
# Include/** → MQL5/Include (patched headers — see patch-mql-zmq.py for details)
if [ -d /mql-zmq/src ]; then
    LIB_DIR="$MT5_DIR/MQL5/Libraries"
    INC_DIR="$MT5_DIR/MQL5/Include"
    mkdir -p "$LIB_DIR" "$INC_DIR"
    cp /mql-zmq/src/Library/MT5/libzmq.dll    "$LIB_DIR/libzmq.dll"
    cp /mql-zmq/src/Library/MT5/libsodium.dll "$LIB_DIR/libsodium.dll"
    # Force clean re-sync of headers so stale files from a previous image
    # don't survive in the persistent wine volume.
    rm -rf "$INC_DIR/Mql" "$INC_DIR/Zmq"
    cp -r /mql-zmq/src/Include/Mql "$INC_DIR/" 2>/dev/null || true
    cp -r /mql-zmq/src/Include/Zmq "$INC_DIR/" 2>/dev/null || true
    log "mql-zmq synced: libzmq.dll + libsodium.dll → $LIB_DIR, patched headers → $INC_DIR"
fi

# ── Precompile EA on every start ───────────────────────────────────────────
# The compiled .ex5 lives in the persistent wine volume. If the .mq5 source
# (copied fresh from the image each start) is newer than the .ex5, recompile
# via MetaEditor headless so the running terminal loads the updated EA.
EA_DIR="$MT5_DIR/MQL5/Experts"
if [ -f "$EA_DIR/ZmqBridge.mq5" ] && { [ ! -f "$EA_DIR/ZmqBridge.ex5" ] || [ "$EA_DIR/ZmqBridge.mq5" -nt "$EA_DIR/ZmqBridge.ex5" ]; }; then
    log "Compiling EA ZmqBridge.mq5 → .ex5 (source newer than compiled)…"
    # MetaEditor needs a display even for /compile:… — spin a temporary Xvfb
    # before supervisord claims DISPLAY=:99.
    Xvfb :99 -screen 0 1024x768x16 >/dev/null 2>&1 &
    XVFB_TMP_PID=$!
    sleep 2
    (cd "$MT5_DIR" && DISPLAY=:99 wine MetaEditor64.exe /compile:"MQL5/Experts/ZmqBridge.mq5" /log 2>&1 | tail -3) || true
    kill $XVFB_TMP_PID 2>/dev/null || true
    wait $XVFB_TMP_PID 2>/dev/null || true
    if [ -f "$EA_DIR/ZmqBridge.ex5" ]; then
        log "EA compiled OK ($(stat -c %s "$EA_DIR/ZmqBridge.ex5") bytes)"
    else
        log "EA compile FAILED — see $EA_DIR/ZmqBridge.log (VNC to debug)"
    fi
fi

# ── Hand over to supervisord ───────────────────────────────────────────────
log "Starting supervisord (xvfb, x11vnc, MT5 terminal)..."
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf -n
