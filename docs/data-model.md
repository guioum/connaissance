# Modèle de données

## La racine `~/Connaissance/`

```
~/Connaissance/
├── Transcriptions/        texte brut (OCR, extraction, copie)
│   ├── Documents/         {organismes, personnes, divers}/<slug>/ (+ promus, - Inbox, Classer
│   │                      en attente de classement)
│   ├── Courriels/         {organismes, personnes, divers}/<slug>/ (+ Fastmail/<compte>/<dossier>
│   │                      en attente d'attribution)
│   └── Notes/             {organismes, personnes, divers}/<slug>/ (+ <dossier Apple>/ en attente)
├── Résumés/               résumés IA (1 par source résumable) — même chemin que la transcription
│   ├── Documents/         idem
│   ├── Courriels/         idem
│   └── Notes/             idem
├── Synthèse/              fiches & chronologies par entité
│   ├── personnes/<slug>/  fiche.md, chronologie, …
│   ├── organismes/<slug>/
│   ├── sujets/<slug>/     projets / thèmes
│   ├── divers/<slug>/
│   └── inconnus/          entités non encore résolues
├── Vues/                  vues symlink régénérables (Sujets, Catégories,
│                          Historique, Snapshots) — hors iCloud
├── .trash/                corbeille ledger (<run_id>/…), réversible,
│                          vidée par `ledger purge`
├── .config/               tracking.db, scoring-courriels.yaml, filtres.yaml
├── CLAUDE.md              hot cache des entités actives (régénéré)
└── dashboard.html         tableau de bord visuel (régénéré)
```

La racine est un **prérequis strict** : le CLI ne la crée jamais
automatiquement (voir [environments.md](environments.md)).

## Le triplet

Une source traverse jusqu'à trois représentations. **Transcription et résumé
sont au même chemin relatif l'un de l'autre** ; ce chemin est **par entité**
(voir ci-dessous), pas un miroir de la source :

```
source brute            ~/Documents/organismes/fmrq/2026-03 contrat.pdf
                        ~/Archives/Courriels/Fastmail/Guillaume/INBOX.mbox   (un message)
                        ~/Archives/Notes/Notes/AI en local.md                (export Apple Notes)
   │ transcrire
   ▼
Transcriptions/<Source>/organismes/fmrq/<date> titre.md   texte brut
   │ résumer
   ▼
Résumés/<Source>/organismes/fmrq/<date> titre.md          résumé IA structuré
   │ synthétiser
   ▼
Synthèse/organismes/fmrq/fiche.md                         agrégation par entité
```

Une transcription naît là où sa source la met (`promus/`, `- Inbox/`,
`Fastmail/<compte>/<dossier>/`, `<dossier Apple>/`) puis est **rangée une
fois** par entité (`classify apply` pour les documents — qui range aussi
l'original dans `~/Documents` —, `organize apply` pour les courriels et les
notes). Le backlog « non organisé » est une notion permanente du système.

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

> **Relations = frontmatter (vérité) + vue navigable (corps).** Le frontmatter
> `relations` (`{entity: type/slug, role}`) reste la source de vérité (DB, audit
> `liens_casses`). Depuis lors, `relations-candidates` renvoie aussi, par
> candidat, un `title` et un `link` bundle-relative (`/Synthèse/{type}/{slug}/fiche.md`),
> et la section `## Relations` de la fiche est rendue en **liens markdown
> navigables** `- {rôle} : [{title}]({link})` — le graphe d'entités devient
> cliquable dans Obsidian (inspiré de la convention de liens OKF ; les fiches
> s'appelant toutes `fiche.md`, les wikilinks par nom seraient ambigus).

> ⚠️ Piège connu : un champ liste vide en YAML (`relations:` seul) parse en
> `None`, pas `[]`. Itérer dessus sans garde plante — voir le fix historique
> dans `verifier_liens_casses` et le point dette dans [roadmap.md](roadmap.md).

## Par entité partout

**Décision du 2026-08-24.** Les sous-dossiers `organismes/` `personnes/`
`divers/` structurent `Transcriptions/`, `Résumés/` et `Synthèse/` pour les
trois sources — un seul modèle mental, entité-d'abord, navigable au Finder,
dans Obsidian et dans les résultats qmd (le chemin dit l'entité). La synthèse
et `entity_paths` lisent cet axe dans l'arborescence. Le classement est
déterministe ([`core/resolution.py`](../src/connaissance/core/resolution.py)),
avec un enrichissement sémantique qmd pour les cas ambigus (`organize
enrich`). `promus/` contient les documents promus depuis des pièces jointes
(`optimize`).

Ce choix a un prix, borné par trois règles :

1. **L'identité d'origine vit dans le frontmatter de la transcription**,
   puisque son chemin ne la porte plus : `source` (chemin) pour un document ;
   `message-id` + `folder` + `source_path` (mbox canonique) pour un courriel ;
   `apple_id` (identité stable, survit au renommage) + `source_path` (chemin
   relatif à l'export) pour une note. `audit reindex-db` reconstruit
   `files.source_path` / `source_id` / `hash` / `entity_*` depuis ces champs
   et le chemin — la DB reste un index dérivé.
2. **Un chemin par entité est instable** : l'entité est un jugement
   révisable (`confidence`, fusions, renommages). Tout déplacement passe par
   `organize` / `entities` et le ledger (réversible), jamais à la main ; après
   une fusion, `audit check --steps liens_casses` et la régénération des
   fiches touchées font partie de l'opération.
3. **Un fichier, une seule entité.** Les relations entre entités vivent dans
   `Synthèse/` (relations), pas dans l'arborescence.

Le lien source ↔ transcription ne passe donc jamais par le chemin : hash de
contenu pour les documents, `message-id` pour les courriels, `apple_id` puis
chemin d'export pour les notes (`notes scan`).

> **Entité vs catégorie.** `~/Documents/` est rangé physiquement par **entité**
> (`<type>/<slug>/<date> titre.ext`) ; la **catégorie** (`banque`, `impots`…) du
> frontmatter n'apparaît **pas** dans le chemin — c'est une étiquette, pas un
> dossier (la catégorie est fortement corrélée à l'entité pour les organismes,
> et baker un jugement IA mutable dans l'arborescence la fragiliserait).
> `documents category-view` génère une **vue** navigable par catégorie sous
> `~/Connaissance/Vues/Catégories/`, en raccourcis vers les originaux : l'autre axe
> sans déplacer ni dupliquer. Hors ~/Documents (pas de pollution iCloud, jamais scannée) ; elle est
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
| `doc_signals` | `rel_path` (relatif à `~/Documents`) | **fiche d'identité, étage signaux** (Phase B) : paquet JSON nom/chemin/dates/métadonnées/texte born-digital + **`excerpt`** (extrait brut, signal premier du classement) + **`pages`** (nb de pages PDF, filtre de coût de la repasse Mistral) + **`pdf_status`** (`ok`/`encrypted`/`unreadable`, écarte les PDF qu'un OCR ne traitera pas) + résumé extractif, caché par `(rel_path, size, mtime)` et invalidé par **version de schéma** `_v` (v7). |
| `doc_classification` | `rel_path` (relatif à `~/Documents`) | **fiche d'identité, étage classement** (Phase C) : entité/catégorie/date/titre/sujet + `confidence` + `status` (`auto`/`attente`) + `model`. **Porte auto = fiche complète** (type+entité+catégorie+date), la confiance basse ne bloque plus. État mutable raffiné à chaque passe ; `hash` sert d'ancre quand le fichier bouge. |
| `entities` | `(type, slug)` | **registre canonique d'entités** (personnes/organismes), VIVANT : `name` + `aliases` (JSON) + `doc_count`. Seedé (`entities seed`) depuis les dossiers rangés + consolidations curées + backup, puis **enrichi de batch en batch** par `register` (ajout si nouvelle, sinon rattachement par alias via `resolve_entity`). Source de `known_entities()` injectée dans les prompts (canonique + aliases → le modèle rabat les variantes, anti-fragmentation). |
| `doc_sujets` | `(rel_path, sujet)` (relatif à `~/Documents`) | **appartenances multi-sujet** avec **précédence par source** : `resume` (sujet de contenu, issu du résumé — **autorité**) supersède `classify` (provisoire, deviné du dossier, filtré du bruit) ; `dedup` (cross-filing) additif. Sujets normalisés `slugify` (accents conservés). Lecture via `sujet_memberships` pour la vue `- Sujets`. |
| `file_ledger` | `run_id` + `old_path`/`new_path` | journal réversible des déplacements (`safe_move`) : `sha256` + `(size, mtime)` permettent un `revert` vérifié par hash. 1 `run_id` = 1 lot révertible. |
| `image_ocr_log` | `rel_path` (relatif à `~/Documents`) | journal de la passe `documents ocr-images` (v2.60.0) : une ligne par image traitée (`is_document` 1/0, `chars`, `confidence` Vision) → reprise idempotente d'un balayage long sans re-OCRiser les photos déjà classées. |
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
