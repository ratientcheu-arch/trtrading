# ══════════════════════════════════════════════════════════════════════════
# mt5-bridge — Ubuntu + Wine + MT5 terminal + EA ZMQ
# Runs the MT5 terminal + EA under Wine and exposes MT5 to the Python bot via
# a ZeroMQ bridge. Everything stays on the same DO droplet — no external SaaS.
# ══════════════════════════════════════════════════════════════════════════
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    WINEDEBUG=-all \
    WINEPREFIX=/root/.wine \
    WINEARCH=win64 \
    DISPLAY=:99 \
    TZ=Europe/Paris \
    LANG=C.UTF-8

# ── System deps ────────────────────────────────────────────────────────────
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        curl \
        gnupg2 \
        ca-certificates \
        software-properties-common \
        cabextract \
        unzip \
        xvfb \
        x11vnc \
        xdotool \
        supervisor \
        netcat-openbsd \
        procps \
        python3 \
        python3-pip \
        libzmq5 \
        tzdata && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    rm -rf /var/lib/apt/lists/*

# ── Wine (stable branch, WineHQ official repo) ─────────────────────────────
RUN mkdir -pm755 /etc/apt/keyrings && \
    wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key && \
    wget -qNP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources && \
    apt-get update && \
    apt-get install -y --install-recommends winehq-stable && \
    rm -rf /var/lib/apt/lists/*

# ── Winetricks (for mono/gecko and .NET if ever needed) ────────────────────
RUN wget -qO /usr/local/bin/winetricks https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks && \
    chmod +x /usr/local/bin/winetricks

# ── Wine prefix bootstrap (non-interactive, no mono/gecko prompt) ──────────
RUN wineboot --init && wineserver -w

# ── MT5 installer (Fusion Markets branded) ─────────────────────────────────
# MetaQuotes-hosted installer, auto-configured for FusionMarkets server.
RUN wget -qO /tmp/mt5setup.exe \
    "https://download.mql5.com/cdn/web/fusion.markets.pty/mt5/fusionmarkets5setup.exe" && \
    ls -la /tmp/mt5setup.exe

# ── ZMQ for the bridge heartbeat ───────────────────────────────────────────
RUN pip3 install --no-cache-dir pyzmq==25.1.2

# ── dingmaotu/mql-zmq — MQL5 ZeroMQ bindings + prebuilt DLLs ───────────────
# The repo ships:
#   - Library/MT5/libzmq.dll     (Windows x64 ZeroMQ)
#   - Library/MT5/libsodium.dll  (dependency of libzmq.dll — CURVE security)
#   - Include/Mql/**.mqh         (high-level MQL5 wrappers: Context, Socket, Msg)
#   - Include/Zmq/**.mqh         (C-style raw bindings — patched below)
# Staged at /mql-zmq/src/ at build time; entrypoint.sh copies into MT5 dirs
# on every start (idempotent; survives Wine volume reset).
#
# IMPORTANT: the library (last updated 2023) has `char[]` parameters the modern
# MT5 compiler now rejects (strict char/uchar signedness). We run patch-mql-zmq.py
# right after extraction to flip those to `uchar[]` — behavior-preserving since
# libzmq sees 8-bit bytes at the ABI level regardless of signedness.
COPY patch-mql-zmq.py /patch-mql-zmq.py
RUN mkdir -p /mql-zmq && \
    wget -qO /tmp/mql-zmq.tar.gz \
    "https://codeload.github.com/dingmaotu/mql-zmq/tar.gz/refs/heads/master" && \
    tar -xzf /tmp/mql-zmq.tar.gz -C /tmp/ && \
    mv /tmp/mql-zmq-master /mql-zmq/src && \
    ls -la /mql-zmq/src/Library/MT5/libzmq.dll /mql-zmq/src/Library/MT5/libsodium.dll && \
    python3 /patch-mql-zmq.py /mql-zmq/src/Include/Zmq && \
    rm /tmp/mql-zmq.tar.gz

# ── Supervisord + entrypoint scripts ───────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/mt5-bridge.conf
COPY entrypoint.sh /entrypoint.sh
COPY install-mt5.sh /install-mt5.sh
COPY auto-login.py /auto-login.py
COPY ea/ /ea/
RUN chmod +x /entrypoint.sh /install-mt5.sh

# ── Ports ──────────────────────────────────────────────────────────────────
# 5900 = VNC (first-time MT5 login + debugging), bind to localhost only
# 5555 = ZMQ PUB (ticks broadcast from EA)
# 5556 = ZMQ REP (order requests from bot)
EXPOSE 5900 5555 5556

# ── Volume for persistent Wine prefix (keeps MT5 login across restarts) ────
VOLUME ["/root/.wine"]

ENTRYPOINT ["/entrypoint.sh"]
