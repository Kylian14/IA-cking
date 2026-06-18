# Ansible : déploiement de l'atelier

Playbook **début / scaffold** pour provisionner un hôte dédié à l'atelier
(Docker + durcissement + HTTPS LAN) en limitant les manipulations à la main.
Pour l'installation générale (Docker local, pull d'images, dev), voir
[`../INSTALL.md`](../INSTALL.md).

**Systèmes supportés :** famille Debian (Debian, Ubuntu et dérivés), car
l'installation de Docker passe par `apt` (paquet `docker.io`). Testé sur Ubuntu
Server 24.04+ (dont 26.04) et Debian 12. Pour une distribution non-apt
(RHEL/Fedora, Alpine, Arch...), adapter les rôles `common` et `docker`.

> État actuel : **scaffold testé sur lecture, pas encore exécuté
> end-to-end sur un host vierge.** Les rôles individuels sont fonctionnels
> et alignés avec ce qu'on a fait à la main. À durcir / tester avant la
> prochaine campagne.

## Pré-requis

- Ansible ≥ 2.16 sur le poste encadrant.
- Hôte cible de la famille Debian (cf. « Systèmes supportés » ci-dessus).
- Un user non-root (`atelier`) existe déjà sur l'hôte, avec votre clé
  publique dans `~/.ssh/authorized_keys`. Sinon, la première connexion
  Ansible va échouer.
- Vous avez le mot de passe sudo du user en question (à fournir au lancement
  via `--ask-become-pass` ou en variable de vault).

## Layout

```
ansible/
├── ansible.cfg
├── inventory.example.yml          → copier vers inventory.yml
├── playbook.yml                   → entrypoint
├── group_vars/
│   └── workshop.yml.example → variables (réseau, ports, TLS, etc.)
├── vars/
│   └── secrets.example.yml        → secrets (à chiffrer en ansible-vault)
└── roles/
    ├── common/     paquets, services désactivés, auto-updates
    ├── ssh/        durcissement sshd (key-only)
    ├── fail2ban/   jail sshd
    ├── docker/     install Docker + docker-compose v2
    ├── firewall/   nftables + sysctl
    └── workshop/   clone du repo + .env + cert TLS + conf nginx HTTPS montée + docker compose up
```

## Utilisation rapide

```bash
cd ansible

# 1) Recopie les exemples
cp inventory.example.yml inventory.yml
cp group_vars/workshop.yml.example group_vars/workshop.yml
cp vars/secrets.example.yml vars/secrets.yml

# 2) Édite les IPs + variables
$EDITOR inventory.yml
$EDITOR group_vars/workshop.yml

# 3) Chiffre les secrets (mot de passe vault choisi à la création)
ansible-vault encrypt vars/secrets.yml

# 4) Test de connectivité
ansible -i inventory.yml workshop -m ping

# 5) Dry-run (montre ce qui changerait sans rien modifier)
ansible-playbook -i inventory.yml playbook.yml \
  -e @vars/secrets.yml --ask-vault-pass --ask-become-pass --check --diff

# 6) Run réel
ansible-playbook -i inventory.yml playbook.yml \
  -e @vars/secrets.yml --ask-vault-pass --ask-become-pass
```

## Variables principales

Cf. `group_vars/workshop.yml.example` :

| Variable | Défaut | Sens |
|---|---|---|
| `workshop_user` | `atelier` | user non-root sur le NUC |
| `workshop_dir` | `/home/{{ workshop_user }}/atelier` | où on clone le repo |
| `workshop_repo` | `https://github.com/Kylian14/IA-cking.git` | repo de l'atelier |
| `workshop_branch` | `main` | branche à checkout |
| `lan_iface` | `eth0` | interface LAN (à adapter, `ip -br link`) |
| `wan_iface` | `wan0` | interface WAN (à adapter) |
| `lan_subnet` | `10.0.0.0/24` | subnet du LAN (à adapter) |
| `student_host_port` | `443` | port hôte HTTPS exposé aux élèves |
| `student_port` | `8000` | port frontend élève |
| `admin_port` | `8001` | port frontend admin |
| `enable_firewall` | `false` | passe à `true` pour activer nftables |
| `ssl_dir` | `/etc/iacking/ssl` | où sont stockés cert + key du TLS auto-signé |
| `ssl_cert_days` | `365` | durée de validité du cert auto-signé |
| `ssl_cn` | `IA'cking Workshop` | CN du cert |
| `ssl_san_list` | `[IP:{{nuc_lan_ip}}, IP:127.0.0.1, DNS:localhost]` | liste des SAN du cert |

Et dans `vars/secrets.yml` (vaulté) :

| Variable | Sens |
|---|---|
| `vault_openrouter_api_key` | clé API OpenRouter |
| `vault_admin_password` | mot de passe admin de l'atelier |
| `vault_session_secret` | secret signature cookies (64 hex). Laisser vide → généré au déploiement. |
| `ansible_become_password` | mot de passe sudo, sinon `--ask-become-pass` |

## Choix qui ne sont PAS automatisés

À faire à la main pour des raisons de risque ou de variabilité :

- **Pousser la clé SSH publique sur le NUC** la première fois (`ssh-copy-id`). Sinon le playbook ne peut pas se connecter.
- **Le routeur/switch LAN** (interface web, action physique) : DHCP, IP statique du NUC, isolation du segment.
- **L'activation effective du firewall** : `enable_firewall: false` par défaut. Le rôle nftables ouvre SSH (22) et l'admin (8001) à tout le `lan_subnet` ; SSH n'accepte que l'auth par clé (+ fail2ban) et l'admin est protégé par mot de passe + rate-limit. Si vous voulez restreindre par IP, réintroduisez une allowlist dans `roles/firewall/templates/nftables.conf.j2` avant de passer `enable_firewall` à `true`.

## TODO / pistes d'amélioration

- [ ] Tester end-to-end sur un hôte vierge.
- [ ] Ajouter un rôle `monitoring` léger (journalctl tail vers tmux pendant l'event).
- [ ] Ansible Molecule pour tester les rôles en isolation (Docker).
- [ ] Gestion multi-environnement (dev / staging / prod-event) via `group_vars/`.
