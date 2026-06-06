# Réorganisation de `~/Documents` (le « grand chantier »)

Comment `connaissance` range un `~/Documents` très désordonné (~67k fichiers,
~48 Go, 23 niveaux d'arborescence, 3 logiques de classement contradictoires)
selon la logique du système — **sans OCR payant** et de façon **entièrement
réversible**.

> Pour le parcours d'ingestion classique (transcrire → résumer → organiser →
> optimiser → synthétiser), voir [pipeline.md](pipeline.md). Ce document couvre
> le **pré-classement** des fichiers bruts *avant* qu'ils n'entrent dans la base.

## Principes directeurs

1. **Zéro OCR Mistral.** Le classement s'appuie uniquement sur des **signaux
   gratuits** : chemin, nom, dates filesystem, métadonnées Office, couche texte
   des PDF *born-digital*, cache OCR existant, et un résumé extractif maison.
   L'OCR payant reste réservé aux scans, plus tard.
2. **Tout est réversible.** Chaque déplacement passe par le **ledger**
   (`safe_move`, journalisé, rollback vérifié par hash). Chaque suppression
   passe par la **corbeille ledger** (`safe_trash`), jamais un `unlink` direct.
   Rien n'est détruit sans un `ledger purge` explicite.
3. **Dry-run par défaut.** Toutes les commandes mutatrices prévisualisent par
   défaut ; il faut `--apply` pour agir. La grande réorg de masse reste en
   dry-run tant qu'elle n'est pas validée à la main.
4. **Lecture via le SSD.** Le contenu est lu depuis le miroir SSD quand il
   existe ; un `stat` seul ne déclenche jamais un download iCloud (fichiers
   *dataless* → signaux nom/chemin/dates uniquement). Voir
   [environments.md](environments.md).
5. **Les secrets ne voyagent jamais en clair.** Un fichier contenant des
   clés/mots de passe est mis en quarantaine : exclu de l'OCR, de l'index qmd et
   du Batch API.

## Vue d'ensemble du flux

```
                    ┌─────────────────────────── socle réversible ───────────────────────────┐
                    │  ledger (safe_move / revert / verify)   +   corbeille (safe_trash / purge) │
                    └──────────────────────────────────────────────────────────────────────────┘
   ~/Documents
   (désordre)
        │
        ▼
  ┌───────────┐   A. cartographier (lecture seule)
  │  triage   │── vrac à classer · dossiers groupés (sujets) · conteneurs (de côté)
  └───────────┘
        │
        ▼
  ┌───────────┐   garde-fou : repérer clés/jetons → liste de quarantaine
  │  secrets  │── (exclus de tout service externe ; relocalisables sous - Protégés/)
  └───────────┘
        │
        ▼
  ┌───────────┐   B. extraire les signaux gratuits (zéro OCR), cache doc_signals
  │  signals  │── born-digital vs scanné · résumé extractif · entités/sujets
  └───────────┘
        │
        ▼
  ┌───────────┐   C. pré-classer : hint heuristique → Batch Haiku → manifeste
  │  classify │── plan→apply : organismes/personnes/divers + AAAA-MM-JJ titre.ext
  └───────────┘   (fiche d'identité doc_classification : entité/catégorie/date/sujet)
        │
        ├──────────────► sujet view : vue symlink « - Sujets » (par sujet)
        ├──────────────► duplicates : doublons exacts + quasi → corbeille
        └──────────────► media : médias par date « - Médias/AAAA/MM »
```

## Étape par étape

### A. Triage — `documents triage` (lecture seule)

Cartographie `~/Documents` en sorts distincts, **sans rien déplacer** :

- **Vrac** à classer individuellement (≈9,5k documents sur le corpus réel).
- **Dossiers thématiques cohérents** (impôts d'une année, formations, dossiers
  clients) détectés par nom + garde-fou « contient des documents » et **gardés
  groupés** comme futurs sujets — pas éclatés par entité.
- **Conteneurs** (repos de code via `.git`/marqueur, paquets macOS `.app`/
  `.ynab4`…) comptés en **unités** et mis de côté.
- **Archives par densité** : le résidu non-documentaire des gros dossiers quasi
  sans documents est mis de côté — mais leurs **vrais documents sont toujours
  extraits** vers le vrac (jamais enterrés).

Heuristiques tunables dans `commands/triage.py`.

### Garde-fou secrets — `documents secrets`

- **Scan** (lecture seule) : repère clés/jetons/mots de passe (préfixes connus
  `AKIA`/`ghp_`/`sk-`/`AIza`…, blocs PEM, JWT, `user:pass@host`, colonnes CSV
  « password », noms sensibles `.env`/`id_rsa`/`*.pem`/`*.kdbx`). Évidences
  caviardées. Zéro dépendance (`core/secrets.py`).
- **Quarantaine** (`--quarantine`) : écrit les chemins repérés dans
  `~/Connaissance/.config/secrets-quarantine.txt`. Le **chokepoint unique**
  `filtres.filter_document` exclut alors ces fichiers de l'OCR, de l'index qmd
  et du Batch API. N'écrit qu'une config — **ne déplace rien**.
- **Relocalisation physique optionnelle** (`--relocate`, `--apply` pour agir) :
  regroupe les fichiers en quarantaine sous `~/Documents/- Protégés/secrets/`
  **via le ledger** (réversible). Le préfixe « - » les sort du scan. Distinct du
  garde-fou actif, qui suffit déjà à les exclure sans bouger.

### B. Signaux — `documents signals` (zéro OCR)

Produit un « paquet de signaux » par document via une **cascade du moins cher au
plus cher** (n'ouvre `pypdfium2` qu'en dernier recours) :

1. nom + chemin + **dossier d'origine** (→ indice de sujet) + dates FS + type ;
2. **métadonnées Office** (`docProps`, stdlib zip+XML) ;
3. **cache OCR** existant (gratuit, prioritaire) ;
4. texte Office/plain (stdlib) ;
5. **couche texte des PDF born-digital**, page 1 (`pypdfium2`, extra optionnel
   `connaissance[pdf]`).

Détecte **born-digital vs scanné**, produit un **résumé extractif maison** (Luhn
+ mots-clés + entités montants/dates/refs, `core/summarize_extractif.py`). Cache
dans `doc_signals` (clé `rel_path` NFC relative à `~/Documents`, validée par
`(size, mtime)`). Secrets et conteneurs exclus. Validé sur 1 804 docs réels :
86 % born-digital (texte gratuit).

### C. Pré-classement — `classify` (plan → apply)

Pipeline hybride heuristique + LLM bon marché :

1. **hint heuristique** (`core/classify.py`) : devine date/entité/catégorie/
   sujet/titre + confiance, gratuitement.
2. **`classify prepare`** : signaux + hint + entités connues → requêtes Batch
   (entités connues en *system prompt* cacheable).
3. **submit_batch** (Haiku 4.5 ; A/B vs Sonnet = match nul → Haiku par défaut,
   quelques dollars pour tout le corpus).
4. **`classify register`** : valide les résultats (catégorie canonique, date),
   réconcilie l'entité (`resolution.py`), écrit la **fiche d'identité**
   `doc_classification` et un **manifeste** plan→apply ; la basse confiance part
   en **zone d'attente**.
5. **`classify apply`** : déplace chaque entrée `auto` vers
   `organismes|personnes|divers/<slug>/AAAA-MM-JJ titre.ext` **via le ledger**
   (l'enregistrement ledger et le repointage de la fiche sont **atomiques**).
   **Dry-run par défaut.**

La fiche `doc_classification` (entité, catégorie, date, titre, **sujet**,
confiance, statut) est la **source de vérité** du classement — un PDF brut n'a
pas de frontmatter.

### Sujets — `sujet view` / `sujet export`

Modèle acté : un document est rangé **physiquement par entité** et porte un
**sujet** (`doc_classification.sujet`). Les sujets ne sont **pas** une
arborescence physique mais une **vue de symlinks** régénérable :

- **`sujet view`** (`--apply` pour écrire, `--clear` pour supprimer) : génère
  `~/Documents/- Sujets/<sujet>/` en raccourcis vers les fichiers à leur
  emplacement courant. **Remplace `- Par catégorie/`** (la catégorie devient un
  sujet grossier). Régénérer après tout déplacement remet la vue à jour.
- **`sujet export <nom>`** (`--zip`, `--dest`) : matérialise un sujet à la
  demande (copie/zip réel, ex. envoi au comptable). Ne touche jamais les
  sources — pas de dossier physique permanent.
- **`sujet list`** : sujets + compteurs.

### D. Doublons — `duplicates` (plan → apply)

Sur le corpus déjà signalé (donc sans secrets ni conteneurs) :

- **`duplicates scan`** (lecture seule) : détecte les **exacts** (même SHA256)
  et les **quasi** (SimHash texte proche du résumé extractif, Hamming ≤ 3/64,
  `core/dedup.py`). Cache `doc_simhash` (référentiel `~/Documents`), **distinct**
  du cache `text_simhash` du corpus transcrit.
- **`duplicates plan`** : garde un *keeper* par cluster (le mieux rangé : chemin
  le moins profond) et marque les autres pour la corbeille.
- **`duplicates apply`** (`--apply` pour agir) : envoie les doublons à la
  **corbeille ledger** (réversible). Dry-run par défaut.

> **Dédup consciente du contexte.** Un même fichier classé dans plusieurs
> dossiers encode souvent plusieurs **appartenances** (ex. un avis de cotisation
> sous `impôts 2025` ET sous `preuves marge BNC`). Avant de corbeiller, `apply`
> capture le **sujet de chaque copie** (`sujet_from_path`, heuristique tunable :
> règle curatée puis slug du dossier non générique) et les attache **tous** au
> fichier gardé dans `doc_sujets` (multi-sujet). La vue `- Sujets` éventaille
> ensuite le fichier sous chacun : le multi-classement physique devient virtuel,
> **aucune association n'est perdue**. Le `ledger` conserve de toute façon le
> chemin exact de chaque copie supprimée.

### Médias — `media` (plan → apply)

Le code et les exports restent en unités (gérés par le triage) ; les **médias**
(images/audio/vidéo) ont leur logique propre :

- **`media plan`** : range les médias sous `~/Documents/- Médias/AAAA/MM/` par
  date (date dans le nom, sinon création/modif filesystem). Manifeste plan→apply.
- **`media apply`** (`--apply` pour agir) : déplace via le ledger, dry-run par
  défaut, collisions de noms gérées.

### Hygiène du registre — `entities candidates` / `entities merge`

Le classement par entité se fragmente quand le même organisme apparaît sous deux
slugs (`ville-de-montreal` vs `ville-montreal`, `monteillet-conseil` vs
`monteillet-conseil-inc`, `banque-nationale` vs `bnc`).

- **`entities candidates`** (lecture seule) : repère les paires suspectes par
  signaux lexicaux — *containment*, Jaccard de tokens, *edit distance*,
  acronyme — sur l'union des fiches `Synthèse/` et des entités en usage dans
  `doc_classification`. Les **variantes annuelles** (`impots-2023` vs
  `impots-2024`) sont exclues : ce sont des entités distinctes par conception. Un
  humain tranche — jamais d'auto-fusion.
- **`entities merge --from <type/slug> --into <type/slug>`** (`--apply`) :
  repointe `doc_classification` + `files` (atomique), ajoute le nom/aliases du
  perdant aux `aliases` de la fiche gardée, déplace ses **résumés** ET ses
  **documents bruts** (`~/Documents/<type>/<slug>/`) **via le ledger**, envoie sa
  fiche à la **corbeille** et supprime ses dossiers vidés. Dry-run par défaut,
  réversible. `entities candidates` scanne aussi les dossiers `~/Documents` pour
  repérer les entités sans fiche (acronymes `bdc`/`bnc`…).

## Le socle réversible

Toutes les phases mutatrices reposent sur deux mécanismes transverses (voir
aussi [data-model.md](data-model.md), table `file_ledger`) :

### Ledger — déplacements réversibles

`core/ledger.safe_move` journalise chaque déplacement (ancien/nouveau chemin,
**SHA256**, taille, mtime, `run_id`). Un `run_id` regroupe un lot.

- `ledger list` — runs récents (avec compteurs applied/reverted/purged).
- `ledger show <run>` — détail d'un run.
- `ledger verify <run>` — cohérence ledger ↔ disque (par hash).
- `ledger revert <run>` — rollback ; ne restaure un fichier que si son **hash**
  est intact (jamais d'écrasement aveugle).

### Corbeille — suppressions réversibles

`core/ledger.safe_trash` déplace vers `~/Connaissance/.trash/<run_id>/<chemin
d'origine>` (`op='trash'`) au lieu de supprimer. Utilisé par `optimize`
(dedup/orphelins) et `duplicates`.

- réversible par `ledger revert <run>` ;
- **`ledger purge`** (`--run`, `--older-than-days`, `--apply`) : vide
  définitivement la corbeille (vrai `unlink`, statut `purged` → exclu du revert).
  Dry-run par défaut. **C'est le seul geste qui détruit pour de vrai.**

## Ordre d'exécution recommandé

```bash
# 1. Cartographier (lecture seule)
connaissance documents triage

# 2. Garde-fou secrets (config seulement)
connaissance documents secrets --quarantine

# 3. Signaux gratuits (zéro OCR) — peuple doc_signals
connaissance documents signals

# 4. Pré-classement (Batch externe entre prepare et register)
connaissance classify prepare --from-signals <fichier>
#   … submit_batch via claude-api-mcp …
connaissance classify register --results <res> --from-prepare <prep>
connaissance classify apply <manifeste>            # dry-run ; --apply pour agir

# 5. Vues & nettoyages dérivés
connaissance sujet view --apply                    # vue par sujet
connaissance duplicates scan                        # rapport doublons
connaissance duplicates plan && connaissance duplicates apply <m> --apply
connaissance media plan && connaissance media apply <m> --apply

# Filet : tout est annulable tant que la corbeille n'est pas purgée
connaissance ledger list
connaissance ledger revert <run>
connaissance ledger purge --older-than-days 30 --apply   # destructif, explicite
```

> Toutes ces commandes existent aussi en outils MCP `mcp__connaissance__*` pour
> Claude Desktop / cowork — voir le tableau du [README racine](../README.md).
