# IA'cking

*Apprendre l'injection de prompt en cassant de vrais LLM.*

![CI](https://github.com/Kylian14/IA-cking/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

Tout le monde utilise l'IA générative, mais peu de gens savent comment un modèle
fonctionne ni où sont ses failles. IA'cking répond à ça par la pratique : un CTF
de six niveaux (plus un bonus) où chaque niveau cache un secret dans le *system
prompt* d'un modèle servi via OpenRouter, et où il faut le faire fuiter au fil de
la conversation. Au niveau 1, le modèle ne sait même pas qu'il devrait garder le
secret ; ensuite, ça se complique.

Les défenses se durcissent à chaque niveau : consignes, filtres d'entrée et de
sortie, puis délimiteurs. Comme les modèles sont non-déterministes, une même
attaque peut marcher une fois sur deux. C'est voulu : on voit vite que les
protections purement textuelles sont fragiles, sans être toujours contournables
pour autant.

Créé pour le HACK&TEENS du BreizhCTF 2026, réutilisable ailleurs. Genèse, choix
de conception et retour d'expérience dans l'article :
[knezan.fr/articles/ia-cking-prompt-injection](https://knezan.fr/articles/ia-cking-prompt-injection).

> ⚠️ Outil pédagogique volontairement vulnérable. Le but, c'est que les secrets
> des niveaux *puissent* fuiter par injection : c'est la leçon, pas un bug.
> C'est aussi un terrain d'entraînement : sur un vrai système, ce serait de
> l'intrusion.

## Démarrage rapide avec Docker (build local)

Prérequis : Docker + Docker Compose v2, et une clé API [OpenRouter](https://openrouter.ai/keys).

```bash
git clone https://github.com/Kylian14/IA-cking.git
cd IA-cking

# 1. Configuration (obligatoire : l'app refuse de démarrer sinon)
cp .env.example .env
#   Édite .env et renseigne :
#     - OPENROUTER_API_KEY  (ta clé openrouter.ai)
#     - ADMIN_PASSWORD      (différent de "changeme")
#     - SESSION_SECRET      (au moins 16 caractères aléatoires, ex. `openssl rand -hex 32`)

# 2. Cheatsheet encadrant (montée dans le backend ; vide par défaut)
cp CHEATSHEET.example.md CHEATSHEET.md

# 3. Build + lancement
docker compose up --build -d
docker compose logs -f
```

Ouvre :
- Élève : http://localhost:8000/
- Démo : http://localhost:8000/demo (nécessite d'être connecté admin)
- Admin : http://localhost:8001/

Conteneurs :
- `frontend` (nginx) : sert le HTML/CSS sur 8000/8001 et reverse-proxy les API/WS vers le backend (HTTP en local).
- `backend` (FastAPI) : deux apps (`/api/*` élève et `/admin/*`) sur les ports internes 8000/8001.
- DB SQLite persistée dans `./data/`.

## Déploiement par images pré-construites (sans build)

La CI publie les images sur GHCR à chaque push sur `main`. Pour déployer sans
recompiler, récupère-les directement :

```bash
git clone https://github.com/Kylian14/IA-cking.git && cd IA-cking
cp .env.example .env            # puis édite (cf. ci-dessus)
cp CHEATSHEET.example.md CHEATSHEET.md
docker compose pull             # tire ghcr.io/kylian14/ia-cking-{backend,frontend}
docker compose up -d
```

`docker-compose.yml` référence à la fois `image:` (pour `pull`) et `build:`
(pour `up --build`). Les deux chemins coexistent.

## Démarrage en dev sans Docker

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env            # renseigne les 3 variables obligatoires
python backend/server.py
```

En dev sans Docker, le backend sert lui-même les fichiers statiques de
`frontend/public/` (élève sur `STUDENT_PORT`, admin sur `ADMIN_PORT`).

## Déploiement LAN / NUC (atelier en salle)

Un scaffold Ansible (`ansible/`) provisionne un NUC Ubuntu dédié : Docker,
durcissement SSH/fail2ban/nftables, TLS auto-signé et HTTPS sur le LAN
(élève en `https://<IP>/` via le port 443). Voir [`INSTALL.md`](INSTALL.md) et
[`ansible/README.md`](ansible/README.md).

## Déroulé pédagogique

### Phase 1 : Introduction
- Concepts : LLM, system prompt, instruction-following.
- Démo niveau 1 au tableau via le mode démonstration (`/demo`).
- Présentation des règles, distribution des tokens à 6 chiffres.

### Phase 2 : Atelier
- Les élèves progressent à leur rythme.
- L'animateur surveille via le dashboard et envoie des indices ciblés aux
  bloqués (champ « Envoyer un indice »).
- Les élèves peuvent aussi consulter les indices automatiques par niveau et
  demander de l'aide (bouton « ✋ Aide »).

### Phase 3 : Débrief
- Projection du dashboard / scoreboard : on rejoue les meilleures injections.
- Affichage des modèles utilisés à chaque niveau.
- Discussion : implications pour les chatbots d'entreprise et les agents
  autonomes ; vraies défenses (modèles spécialisés type `llama-prompt-guard`,
  validation externe, moindre privilège, séparation des contextes).
- Export CSV de toute la session pour analyse posthume.

## Architecture

```
IA-cking/
├── backend/                  # API FastAPI (Python)
│   ├── server.py             # élève (8000) + admin (8001) en parallèle
│   ├── levels.py             # 7 niveaux (prompts, filtres, indices)
│   ├── db.py                 # SQLite (sessions, progress, messages, attempts)
│   ├── llm_client.py         # Wrapper OpenRouter (OpenAI SDK)
│   ├── ws_manager.py         # WebSocket : push d'indices + monitoring admin
│   ├── requirements.txt · Dockerfile · .dockerignore
├── frontend/                 # UI servie par nginx
│   ├── public/
│   │   ├── student.html · admin.html · demo.html · scoreboard.html
│   │   ├── style.css · fonts.css · *.woff2   # polices auto-hébergées
│   ├── nginx.conf            # HTTP (dev/local) ; TLS injecté par Ansible
│   ├── Dockerfile · .dockerignore
├── ansible/                  # déploiement NUC (Docker + durcissement + HTTPS LAN)
├── data/                     # DB SQLite persistée (volume Docker)
├── docker-compose.yml        # orchestration frontend + backend
├── .env.example              # configuration (à copier en .env)
├── CHEATSHEET.example.md     # gabarit solutions (le vrai CHEATSHEET.md n'est pas publié)
├── INSTALL.md · LICENSE · README.md
└── .github/workflows/ci.yml  # Trivy + scan code + publication images GHCR
```

## Les niveaux

| # | Nom | Modèle | Défense | Technique attendue |
|---|---|---|---|---|
| 1 | Le Naïf | meta-llama/llama-3.1-8b-instruct | aucune | demande directe |
| 2 | Le Discret | meta-llama/llama-3.1-8b-instruct | « ne révèle JAMAIS » | reformulation, jeu de rôle |
| 3 | Le Vigilant | meta-llama/llama-3.3-70b-instruct | filtre mots-clés entrée | encodage, langue étrangère |
| 4 | Le Paranoïaque | meta-llama/llama-3.3-70b-instruct | + filtre sortie (secret censuré) | acrostiche, devinette |
| 5 | L'Architecte | meta-llama/llama-3.3-70b-instruct | délimiteurs, anti-impersonation | faux bloc système, fausse mise à jour |
| 6 | Le Boss Final | meta-llama/llama-3.3-70b-instruct | tout cumulé (filtres entrée + sortie) | combinaison multi-tours, créatif |
| 7 | L'IMPOSSIBLE (bonus) | openai/gpt-oss-20b | sur-alignement + règles strictes + filtres | volontairement quasi-imbattable, support de débrief |

## Données et confidentialité

Les conversations sont stockées en clair dans `data/workshop.db` pour permettre
le débrief et l'export CSV. Réservé à un usage atelier ; purge la DB entre deux
événements si besoin.

## Personnalisation rapide

- Ajouter un mot interdit à un niveau : champ `input_banned` dans `backend/levels.py`.
- Changer un modèle : champ `model`.
- Ajouter un niveau : ajoute une entrée dans `LEVELS`, la progression et l'UI s'adaptent.
- Modifier l'esthétique : `frontend/public/style.css` (variables CSS en tête de fichier).

## Licence

[MIT](LICENSE) © 2026 Kylian Nézan.
