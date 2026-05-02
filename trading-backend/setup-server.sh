#!/bin/bash
# ================================================================
# Script d'installation du backend trading sur Ubuntu (DigitalOcean)
# Usage: ssh root@146.190.17.26 puis coller ce script
# ================================================================

set -e
echo "========================================"
echo "  Installation Backend Day Trading Bot"
echo "========================================"

# ── 1. Installer Docker ──────────────────────────────────────────
echo ""
echo "[1/5] Installation de Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    echo "Docker installe avec succes"
else
    echo "Docker deja installe"
fi

# Installer Docker Compose plugin si absent
if ! docker compose version &> /dev/null; then
    apt-get update && apt-get install -y docker-compose-plugin
    echo "Docker Compose installe"
else
    echo "Docker Compose deja installe"
fi

# ── 2. Configurer le firewall ─────────────────────────────────────
echo ""
echo "[2/5] Configuration du firewall..."
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP (pour Caddy / Let's Encrypt)
ufw allow 443/tcp  # HTTPS
ufw --force enable
echo "Firewall configure : ports 22, 80, 443 ouverts"

# ── 3. Creer le dossier du projet ─────────────────────────────────
echo ""
echo "[3/5] Creation du dossier projet..."
mkdir -p /opt/trading-backend
cd /opt/trading-backend

echo "Dossier /opt/trading-backend cree"

# ── 4. Message de fin ─────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Installation terminee !"
echo "========================================"
echo ""
echo "Prochaine etape : copier les fichiers du backend sur le serveur."
echo "Depuis votre Mac, executez :"
echo ""
echo "  scp -r /Users/macbookpro/Downloads/project\\ bolt/trading-backend/* root@146.190.17.26:/opt/trading-backend/"
echo ""
echo "Puis revenez sur le serveur et suivez les etapes."
