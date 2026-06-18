# Cheatsheet encadrant : modèle (sans solutions)

> ⚠️ **Gabarit public.** Les solutions réelles ne sont volontairement pas
> publiées dans ce dépôt, pour préserver le challenge. Copie ce fichier en
> `CHEATSHEET.md` puis remplis-le avec tes propres techniques/prompts :
>
> ```bash
> cp CHEATSHEET.example.md CHEATSHEET.md
> ```
>
> Le dashboard admin sert le contenu de `CHEATSHEET.md` (route `/admin/cheatsheet`,
> authentification admin requise) et `docker-compose.yml` le monte en lecture
> seule dans le conteneur backend. Le fichier doit donc exister avant de lancer
> la stack, au même titre que `.env`.

Pour chaque niveau, documente :

## Niveau N : Nom (`modèle`, défense)

Rappel de la défense en place (filtre d'entrée/sortie, consignes système…).

### Prompts qui marchent (souvent)
- *(à compléter, les LLM sont non-déterministes : un prompt peut réussir un essai sur deux)*

### Indices à envoyer
- *Vague* : « … »
- *Orienté* : « … »

---

> Astuce pédagogique : note les attaques qui **échouent** aussi. Le but de
> l'atelier est de montrer que les défenses purement textuelles sont fragiles
> mais pas systématiquement contournables, et la non-reproductibilité est un
> enseignement en soi.
