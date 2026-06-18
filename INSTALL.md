# Guide d'installation IA'cking

Trois façons de lancer l'atelier :

1. [Docker, build local](#1-docker-build-local) : le plus simple pour tester.
2. [Docker, images pré-construites (GHCR)](#2-docker-images-pré-construites-ghcr) : déploiement sans recompiler.
3. [Sans Docker (dev)](#3-sans-docker-dev) : itération sur le code Python.

Puis le [déploiement LAN/NUC via Ansible](#4-déploiement-lannuc-ansible) pour un atelier en salle.

---

## Prérequis

| Outil | Version | Pour |
|---|---|---|
| Docker + Compose v2 | récent | options 1 & 2 |
| Python | 3.11+ | option 3 (dev) |
| Clé API OpenRouter | n/a | tous (https://openrouter.ai/keys) |

## Configuration du `.env` (obligatoire)

L'application **refuse de démarrer** (`fail_fast`) tant que ces trois variables
ne sont pas correctement renseignées :

```bash
cp .env.example .env
```

| Variable | Obligatoire | Note |
|---|---|---|
| `OPENROUTER_API_KEY` | ✅ | clé openrouter.ai (ne doit pas rester `sk-or-your...`) |
| `ADMIN_PASSWORD` | ✅ | mot de passe du dashboard admin (≠ `changeme`) |
| `SESSION_SECRET` | ✅ | ≥ 16 caractères aléatoires, ex. `openssl rand -hex 32` |
| `SECURE_COOKIES` | derrière HTTPS | `1` en prod TLS, `0` en dev HTTP |
| `MAX_LLM_CALLS_PER_SESSION` | non | plafond anti-coût (défaut 300) |

Les rate-limits, tailles de payload et quotas sont aussi configurables, voir les
commentaires de [`.env.example`](.env.example).

## Cheatsheet encadrant (obligatoire pour le backend)

Les solutions ne sont pas publiées. Crée le fichier à partir du gabarit (il est
monté en lecture seule dans le conteneur backend, comme `.env`) :

```bash
cp CHEATSHEET.example.md CHEATSHEET.md
```

---

## 1. Docker (build local)

```bash
git clone https://github.com/Kylian14/IA-cking.git
cd IA-cking
cp .env.example .env                 # puis édite les 3 variables obligatoires
cp CHEATSHEET.example.md CHEATSHEET.md
docker compose up --build -d
docker compose logs -f
```

- Élève : http://localhost:8000/
- Admin : http://localhost:8001/
- Démo  : http://localhost:8000/demo (connecté admin)

## 2. Docker (images pré-construites, GHCR)

La CI pousse `ghcr.io/kylian14/ia-cking-backend` et `…-frontend` à chaque push
sur `main`. Pour déployer sans compiler :

```bash
git clone https://github.com/Kylian14/IA-cking.git && cd IA-cking
cp .env.example .env                 # édite
cp CHEATSHEET.example.md CHEATSHEET.md
docker compose pull
docker compose up -d
```

`docker-compose.yml` porte à la fois `image:` et `build:` : `pull` récupère les
images publiées, `up --build` recompile localement.

## 3. Sans Docker (dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env                 # édite
python backend/server.py
```

Le backend sert lui-même `frontend/public/` (élève sur `STUDENT_PORT`, admin sur
`ADMIN_PORT`, par défaut 8000 / 8001).

---

## 4. Déploiement LAN/NUC (Ansible)

Pour un atelier en salle sur un NUC dédié (Ubuntu 24.04+) : Docker, durcissement
SSH / fail2ban / nftables, TLS auto-signé, et HTTPS sur le LAN (élève en
`https://<IP>/` via le port 443, admin sur 8001).

Le rôle `workshop` :
- clone le repo et génère un `.env` (perms 0600, secrets via Ansible Vault) ;
- génère un cert auto-signé et **monte** une conf nginx HTTPS par-dessus celle
  du conteneur (sans modifier le fichier versionné) ;
- pose `STUDENT_HOST_PORT=443` dans `.env`, interpolé par `docker-compose.yml` ;
- lance `docker compose up -d` et vérifie le HTTPS (smoke test).

Procédure détaillée et variables : [`ansible/README.md`](ansible/README.md).

```bash
cd ansible
cp inventory.example.yml inventory.yml
cp group_vars/breizhctf_nucs.yml.example group_vars/breizhctf_nucs.yml
cp vars/secrets.example.yml vars/secrets.yml
ansible-vault encrypt vars/secrets.yml
ansible-playbook -i inventory.yml playbook.yml \
  -e @vars/secrets.yml --ask-vault-pass --ask-become-pass --check --diff   # dry-run
```

---

## Vérifier que ça tourne

```bash
docker compose ps                              # backend + frontend "healthy"
curl -fsS http://localhost:8000/health         # {"ok":true}
curl -fsS http://localhost:8001/health         # {"ok":true}
```

Workflow atelier :
1. Admin → http://localhost:8001/ → connexion (`ADMIN_PASSWORD`).
2. Génère des tokens à 6 chiffres, distribue-les aux élèves.
3. Élève → http://localhost:8000/ → connexion avec le token + prénom.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Le backend quitte au démarrage | une des 3 variables obligatoires manque/invalide dans `.env` |
| `/admin/cheatsheet` renvoie 404 | `CHEATSHEET.md` absent (`cp CHEATSHEET.example.md CHEATSHEET.md`) |
| Erreur modèle dans le chat | `OPENROUTER_API_KEY` invalide ou crédit OpenRouter épuisé |
| Port 8000/8001 déjà pris | change `STUDENT_HOST_PORT` / `ADMIN_HOST_PORT` dans `.env` |
| Cookies non conservés en HTTPS | mets `SECURE_COOKIES=1` |
