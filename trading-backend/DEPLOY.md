# Deploiement du Backend Trading sur DigitalOcean

## Prerequis

- Serveur Ubuntu 22.04+ sur DigitalOcean
- Docker et Docker Compose installes
- Un nom de domaine pointant vers l'IP du serveur (ex: `api.day-trading.votre-domaine.com`)
- Compte Interactive Brokers avec acces API active

## 1. Preparation du serveur

```bash
# Installer Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Installer Docker Compose
sudo apt install docker-compose-plugin

# Firewall
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

## 2. Deploiement

```bash
# Cloner ou copier le dossier trading-backend sur le serveur
scp -r trading-backend/ user@votre-ip:~/trading-backend/

# Se connecter au serveur
ssh user@votre-ip
cd trading-backend

# Creer le fichier .env
cp .env.example .env
nano .env
# Remplir : IB_USERNAME, IB_PASSWORD, DB_PASSWORD, API_KEY
# Generer un API_KEY avec : openssl rand -hex 32

# Configurer le domaine dans le Caddyfile
nano caddy/Caddyfile
# Remplacer {$CADDY_DOMAIN:localhost} par votre domaine

# Lancer les services
docker compose up -d

# Verifier les logs
docker compose logs -f backend
```

## 3. Configuration du Frontend

Dans Cloudflare Pages, ajouter les variables d'environnement :
- `VITE_BACKEND_URL` = `https://api.day-trading.votre-domaine.com`
- `VITE_API_KEY` = la meme cle que dans le .env du backend

Puis redeployer le frontend.

## 4. Verification

```bash
# Tester la sante du backend
curl https://api.day-trading.votre-domaine.com/api/health

# Verifier la connexion IB Gateway
docker compose logs ibgateway
```

## 5. Commandes utiles

```bash
# Voir les logs du bot
docker compose logs -f backend

# Redemarrer un service
docker compose restart backend

# Arreter tout
docker compose down

# Mettre a jour
docker compose pull
docker compose up -d --build
```

## Variables d'environnement (.env)

| Variable | Description | Exemple |
|----------|-------------|---------|
| IB_USERNAME | Login Interactive Brokers | votre_login |
| IB_PASSWORD | Mot de passe IB | votre_mdp |
| IB_TRADING_MODE | Mode de trading | live |
| DB_PASSWORD | Mot de passe PostgreSQL | un_mot_de_passe_fort |
| API_KEY | Cle API pour le frontend | openssl rand -hex 32 |
| ALLOWED_ORIGINS | URL du frontend | https://day-trading.pages.dev |
| STARTING_CAPITAL | Capital initial EUR | 100.0 |
| MAX_ORDER_SIZE | Max par ordre EUR | 20.0 |
