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

Toutes les tables ajoutées par le pipeline sont **additives**
(`CREATE TABLE IF NOT EXISTS`). Les colonnes manquantes d'une base ancienne
sont rattrapées par `_migrate()` (`PRAGMA table_info` → `ALTER TABLE ADD
COLUMN`) ; pour `doc_classification` la liste attendue est dérivée de
`_CLS_COLS`, si bien qu'ajouter une colonne à la fiche migre automatiquement
les bases existantes.

| Table | Clé d'identité | Rôle |
|---|---|---|
| `operations` | — | journal horodaté des opérations (plugin, operation, source/dest, status). |
| `files` | `path` (absolu) | fichiers connus : type, entité, `message_id`, `hash` SHA256, `size`, `mtime`. Cœur du cache JIT de déduplication. |
| `text_simhash` | `rel_path` NFC (relatif à `CONNAISSANCE_ROOT`) | cache des SimHash texte des **transcriptions** (quasi-doublons du corpus). Voir [pipeline.md](pipeline.md). |
| `doc_simhash` | `rel_path` NFC (relatif à `~/Documents`) | cache des SimHash texte des **fichiers bruts** (Phase D — doublons du pré-classement). Table séparée de `text_simhash` : un référentiel par table. |
| `doc_signals` | `rel_path` (relatif à `~/Documents`) | **fiche d'identité, étage signaux** (Phase B) : paquet JSON nom/chemin/dates/métadonnées/texte born-digital + **`excerpt`** (extrait brut, signal premier du classement) + résumé extractif, caché par `(rel_path, size, mtime)` et invalidé par **version de schéma** `_v`. |
| `doc_classification` | `rel_path` (relatif à `~/Documents`) | **fiche d'identité, étage classement** (Phase C) : entité/catégorie/date/titre/sujet + `confidence` + `status` (`auto`/`attente`) + `model`. **Porte auto = fiche complète** (type+entité+catégorie+date), la confiance basse ne bloque plus. État mutable raffiné à chaque passe ; `hash` sert d'ancre quand le fichier bouge. |
| `doc_sujets` | `(rel_path, sujet)` (relatif à `~/Documents`) | **appartenances multi-sujet** avec **précédence par source** : `resume` (sujet de contenu, issu du résumé — **autorité**) supersède `classify` (provisoire, deviné du dossier, filtré du bruit) ; `dedup` (cross-filing) additif. Sujets normalisés `slugify` (accents conservés). Lecture via `sujet_memberships` pour la vue `- Sujets`. |
| `file_ledger` | `run_id` + `old_path`/`new_path` | journal réversible des déplacements (`safe_move`) : `sha256` + `(size, mtime)` permettent un `revert` vérifié par hash. 1 `run_id` = 1 lot révertible. |
| `llm_usage` | — | tokens et coûts par appel (input/output, cache, `cost_usd`). |

> ⚠️ Deux **univers de fichiers disjoints** partagent le nom de colonne
> `rel_path` mais **pas le référentiel** : `text_simhash` indexe les
> transcriptions markdown du corpus (relatif à `CONNAISSANCE_ROOT`) ;
> `doc_simhash`/`doc_signals`/`doc_classification` indexent les fichiers bruts
> du pré-classement (relatif à `~/Documents`). Ils ne décrivent jamais le même
> fichier — ne pas les joindre par `USING(rel_path)`. **Convention figée** : un
> seul référentiel par table ; le SimHash des bruts va dans `doc_simhash`,
> jamais dans `text_simhash` (sinon collision de référentiels). Toutes les clés
> sont normalisées **NFC** (macOS écrit en NFD).

### Le cache JIT

`get_or_compute_hash(path)` et `get_or_compute_simhash(abs, rel)` suivent le
même contrat : si la ligne existe avec `(size, mtime)` identiques et un hash
non-NULL, on retourne sans relire le fichier ; sinon on calcule et on persiste.
Conséquence : en base stable, **zéro recalcul**. Un préfiltre par taille évite
même de hasher quand aucune collision n'est possible. Voir
[pipeline.md](pipeline.md) pour le rationnel JIT.
