#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════════════════
# install-mt5.sh — First-run MT5 installer under Wine
# Launches the MT5 setup wizard with /auto flag (silent mode).
# ═══════════════════════════════════════════════════════════════════════════

log() { echo "[install-mt5 $(date -u +%H:%M:%S)] $*"; }

log "Starting Xvfb for install..."
Xvfb :99 -screen 0 1280x1024x24 -ac &
XVFB_PID=$!
sleep 3

log "Launching MT5 silent installer..."
# /auto = silent install, /portable = single-directory install (no registry pollution)
DISPLAY=:99 wine /tmp/mt5setup.exe /auto || true

# Wait up to 180s for terminal64.exe to appear
# Fusion-branded installer creates folder "Fusion Markets MetaTrader 5"
MT5_TERMINAL_FUSION="$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5/terminal64.exe"
MT5_TERMINAL_GENERIC="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
MT5_TERMINAL=""
for i in $(seq 1 180); do
    if [ -f "$MT5_TERMINAL_FUSION" ]; then
        MT5_TERMINAL="$MT5_TERMINAL_FUSION"
        break
    fi
    if [ -f "$MT5_TERMINAL_GENERIC" ]; then
        MT5_TERMINAL="$MT5_TERMINAL_GENERIC"
        break
    fi
    sleep 1
done
if [ -n "$MT5_TERMINAL" ]; then
    log "MT5 installed successfully at $MT5_TERMINAL"
else
    log "ERROR: terminal64.exe not found after 180s wait"
fi

# Generate /config.ini for auto-login on next launch
log "Generating /config.ini with auto-login credentials..."
cat > /config.ini <<EOF
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

chmod 600 /config.ini

log "Shutting down temporary Xvfb..."
kill $XVFB_PID 2>/dev/null || true

# Copy the ZmqBridge EA into MT5's MQL5/Experts folder
# Try Fusion-branded path first, fall back to generic
if [ -d "$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5" ]; then
    EA_DIR="$WINEPREFIX/drive_c/Program Files/Fusion Markets MetaTrader 5/MQL5/Experts"
else
    EA_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/MQL5/Experts"
fi
mkdir -p "$EA_DIR"
if [ -f /ea/ZmqBridge.mq5 ]; then
    cp /ea/ZmqBridge.mq5 "$EA_DIR/"
    log "EA ZmqBridge.mq5 copied to $EA_DIR"
else
    log "WARNING: /ea/ZmqBridge.mq5 not found in image. Will need manual copy."
fi

log "Install phase complete."
