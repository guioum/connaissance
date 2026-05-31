# Modèle de données

## La racine `~/Connaissance/`

```
~/Connaissance/
├── Transcriptions/        texte brut (OCR, extraction, copie)
│   ├── Documents/         {divers, promus, personnes, organismes}
│   ├── Courriels/         {Fastmail/<compte>/<dossier>}
│   └── Notes/             {divers, personnes, organismes}
├── Résumés/               résumés IA (1 par source résumable)
│   ├── Documents/         {divers, promus, personnes, organismes}
│   └── Notes/             {divers, personnes, organismes}
├── Synthèse/              fiches & chronologies par entité
│   ├── personnes/<slug>/  fiche.md, chronologie, …
│   ├── organismes/<slug>/
│   ├── sujets/<slug>/     projets / thèmes
│   ├── divers/<slug>/
│   └── inconnus/          entités non encore résolues
├── .config/               tracking.db, scoring-courriels.yaml, filtres.yaml
├── CLAUDE.md              hot cache des entités actives (régénéré)
└── dashboard.html         tableau de bord visuel (régénéré)
```

La racine est un **prérequis strict** : le CLI ne la crée jamais
automatiquement (voir [environments.md](environments.md)).

## Le triplet

Une source traverse jusqu'à trois représentations miroirs, au **même chemin
relatif** d'un arbre à l'autre :

```
source brute            ~/Documents/promus/ABC.pdf      (ou mbox, ou note Apple)
   │ transcrire
   ▼
Transcriptions/Documents/promus/ABC.md                  texte OCR brut
   │ résumer
   ▼
Résumés/Documents/promus/ABC.md                         résumé IA structuré
   │ organiser + synthétiser
   ▼
Synthèse/organismes/<entité>/fiche.md                   agrégation par entité
```

- **Transcription** : fidèle à la source, aucune interprétation. Pour les
  courriels, plusieurs courriels d'un fil peuvent être regroupés.
- **Résumé** : produit par `claude-api-mcp` à partir d'un template
  ([`prompts/`](../src/connaissance/prompts/)), avec un frontmatter normalisé.
  Les courriels purement transactionnels peuvent ne pas être résumés.
- **Synthèse** : la fiche d'entité agrège les résumés qui la concernent ; la
  chronologie en extrait les événements datés ; les actions en extraient les
  engagements/échéances (groupe `actions`).

Le couplage transcription↔résumé↔source est suivi dans `tracking.db` ; l'audit
vérifie que les triplets ne se désynchronisent pas
(`audit check --steps triplets_desynchronises`).

## Conventions de frontmatter

Chaque Markdown porte un frontmatter YAML. Les champs **requis par type** sont
la source de vérité dans [`commands/audit.py`](../src/connaissance/commands/audit.py)
(`CHAMPS_REQUIS`), vérifiés par `audit check --steps frontmatter_invalide` :

| Type | Champs requis |
|---|---|
| `courriel` | type, date, from, direction, title, category |
| `fil` (fil de courriels) | type, date-start, date-end, from, title, category, message-count |
| `document` | type, date, title, category |
| `note` | type, date, title, category |
| `personne` | type, slug, status, first-contact, last-contact, created, modified |
| `organisme` | type, subtype, slug, status, first-contact, last-contact, created, modified |

Les fiches d'entité portent en plus `aliases` (noms/adresses alternatifs) et
`relations` (liens vers d'autres entités) — alimentés par
`synthesis aliases-candidates` / `relations-candidates`.

> ⚠️ Piège connu : un champ liste vide en YAML (`relations:` seul) parse en
> `None`, pas `[]`. Itérer dessus sans garde plante — voir le fix historique
> dans `verifier_liens_casses` et le point dette dans [roadmap.md](roadmap.md).

## L'organisation par entité

Les sous-dossiers `personnes/` `organismes/` `sujets/` `divers/` structurent
Résumés et Synthèse. Le classement est déterministe
([`core/resolution.py`](../src/connaissance/core/resolution.py)), avec un
enrichissement sémantique qmd pour les cas ambigus (`organize enrich`). Les
résumés non encore classables tombent dans `divers/` ou `inconnus/`. `promus/`
contient les documents promus depuis des pièces jointes (`optimize`).

> **Entité vs catégorie.** `~/Documents/` est rangé physiquement par **entité**
> (`<type>/<slug>/<date> titre.ext`) ; la **catégorie** (`banque`, `impots`…) du
> frontmatter n'apparaît **pas** dans le chemin — c'est une étiquette, pas un
> dossier (la catégorie est fortement corrélée à l'entité pour les organismes,
> et baker un jugement IA mutable dans l'arborescence la fragiliserait).
> `documents category-view` génère une **vue** navigable par catégorie sous
> `~/Documents/- Par catégorie/`, en raccourcis vers les originaux : l'autre axe
> sans déplacer ni dupliquer. Le préfixe `- ` l'exclut du scan ; elle est
> régénérable (`--apply`) et réversible (`--clear`).

## tracking.db

SQLite sous `~/Connaissance/.config/tracking.db`, **propriété exclusive du
CLI**. Schéma byte-compatible avec la v1.9.0 du plugin cowork d'origine
(aucune migration destructive). Tables :

| Table | Rôle |
|---|---|
| `operations` | journal horodaté des opérations (plugin, operation, source/dest, status). |
| `files` | fichiers connus : type, entité, `message_id`, `hash` SHA256, `size`, `mtime`. Cœur du cache JIT de déduplication. |
| `text_simhash` | cache des SimHash texte des transcriptions (quasi-doublons), clé = chemin **logique** relatif à la racine. Voir [pipeline.md](pipeline.md). |
| `llm_usage` | tokens et coûts par appel (input/output, cache, `cost_usd`). |

### Le cache JIT

`get_or_compute_hash(path)` et `get_or_compute_simhash(abs, rel)` suivent le
même contrat : si la ligne existe avec `(size, mtime)` identiques et un hash
non-NULL, on retourne sans relire le fichier ; sinon on calcule et on persiste.
Conséquence : en base stable, **zéro recalcul**. Un préfiltre par taille évite
même de hasher quand aucune collision n'est possible. Voir
[pipeline.md](pipeline.md) pour le rationnel JIT.
