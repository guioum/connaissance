# Réorganisation de `~/Documents` (le « grand chantier »)

Comment `connaissance` range un `~/Documents` très désordonné (~67k fichiers,
~48 Go, 23 niveaux d'arborescence, 3 logiques de classement contradictoires)
selon la logique du système — **sans OCR payant** et de façon **entièrement
réversible**.

> Pour le parcours d'ingestion classique (transcrire → résumer → organiser →
> optimiser → synthétiser), voir [pipeline.md](pipeline.md). Ce document couvre
> le **pré-classement** des fichiers bruts *avant* qu'ils n'entrent dans la base.

> **Accents conservés.** Les noms de fichiers et de dossiers produits par le
> système **gardent les accents français** (pas de translittération `é→e`). Les
> slugs restent en minuscules-tirets mais accentués (`banque-de-développement-du-canada`,
> `2024-03-29 avis-de-cotisation-mélanie.pdf`) et normalisés **NFC** (clé stable
> sur macOS qui écrit en NFD). Convention unique : `resolution.slugify`.

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

Détecte **born-digital vs scanné** et conserve un **extrait du texte brut**
(`excerpt`, ~1500 car., espaces compactés) — c'est le **signal premier** envoyé
au classement (un LLM lit le texte réel bien mieux que des mots-clés). Garde
aussi un **résumé extractif** (Luhn + mots-clés + entités montants/dates/refs,
`core/summarize_extractif.py`) pour d'autres usages (fingerprint de quasi-doublon,
*haystack* du hint). Cache dans `doc_signals` (clé `rel_path` NFC relative à
`~/Documents`, validée par `(size, mtime)` + **version de schéma** `_v`). Secrets
et conteneurs exclus. Validé sur 1 804 docs réels : 86 % born-digital.

### C. Pré-classement — `classify` (plan → apply)

Pipeline hybride heuristique + LLM bon marché :

1. **hint heuristique** (`core/classify.py`) : devine date/entité/catégorie/
   sujet/titre + confiance, gratuitement.
2. **`classify prepare`** : **extrait du texte brut** + signaux + hint → requêtes
   Batch. Le *system prompt* (cacheable) porte le **bloc partagé** avec le
   classement final — discipline d'entité + règles de catégorie + entités connues
   (`shared_classification_suffix`, fragments `prompts/_entity_discipline.md` et
   `_category_rules.md`).
3. **submit_batch** (Haiku 4.5 ; A/B vs Sonnet = 100 % d'accord → Haiku par défaut).
   ⚠️ **Le prompt caching n'aide pas ici** (≈ $20 pour le corpus, 1 doc/requête —
   pas de tarif caché). DEUX raisons (le caching n'est PAS cassé, il marche en
   direct séquentiel) : (a) le **système du pré-classement (~2800 tok) est sous le
   seuil minimum cachable** de Haiku 4.5 (≈ 4096 tok) → no-op ; (b) même au-dessus
   du seuil, le **Batch parallèle écrit le cache mais ne le relit jamais** (workers
   froids). Levier de coût = trimmer le système / grouper, pas le caching.
4. **`classify register`** : valide les résultats (catégorie via
   `canonicalize_category`, date), réconcilie l'entité (`resolution.py`), écrit la
   **fiche d'identité** `doc_classification` + sujet (`doc_sujets`) et un
   **manifeste** plan→apply. **Porte auto = fiche structurellement complète**
   (type exploitable + entité + catégorie + date) — la **confiance basse ne bloque
   plus** (déplacement réversible via le ledger) ; il ne reste en **zone
   d'attente** que ce qui manque une donnée (entité `divers`/`inconnus`, sans
   catégorie/date) ou dont le parse a échoué.
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

**Précédence par maturité (pas une union plate).** Un sujet a deux sources
possibles : `classify` (provisoire, deviné du dossier d'origine — souvent du
bruit : `archive-*`, `2018-02`) et `resume` (issu du **contenu** du résumé,
propre), synchronisé dans `doc_sujets` au `summarize register`. `sujet_memberships`
applique : si un sujet `resume` existe pour un document → il **supersède** le
`classify` ; sinon repli sur `classify` **filtré du bruit** (`_is_junk_sujet`) ;
`dedup` (cross-filing) reste additif. Les sujets sont normalisés via `slugify`
(**accents conservés**, comme les slugs d'entité). Même principe que entité et
catégorie : *pré = brouillon, résumé = autorité.*

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
> capture le **sujet de chaque copie** (`sujet_from_path`, heuristique tunable —
> voir ci-dessous) et les attache **tous** au
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

### Dérivation des sujets (`sujet_from_path`)

Pour le dossier-projet le plus profond exploitable d'un chemin (en **sautant**
les conteneurs génériques, les dossiers « année seule » et les dossiers de
**personne** — fournis par l'appelant, car ce sont des entités) :

1. **impôts daté** → `impôts-AAAA` (dossier daté préservé, personne retirée) ;
   **thème diffus** (`maison`, `santé`, `véhicule`, `voyage`, `formation`,
   `assurance`, `taxes`, `enfants`) → le thème propre ;
2. sinon → **slug granulaire tel quel** : finance / factures / paie restent
   **éclatés** par sous-dossier (`bnc-paiements-mastercard`, `factures-aws`,
   `payes-québecor-2015-2016`) et les dossiers-projets datés sont préservés
   (`bnc-contrat-marge-de-crédit-2024`).

⚠️ Le texte des dossiers est **normalisé NFC** avant tout match de règle —
macOS écrit en NFD, sans quoi les regex accentuées (`impôt`, `santé`…) ratent.
Heuristique tunable (`core/classify._SUJET_RULES`, `sujet_from_path`).

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

### Déplacer un document de façon cohérente (`core.relocate`)

Un document a 3 représentations partageant `<type>/<slug>/<stem>` (source
`~/Documents`, **transcription** et **résumé** sous `~/Connaissance`) et des
**références** : `source` du résumé → transcription, tables
`doc_classification`/`doc_signals`/`doc_sujets` (rel ~/Documents), `text_simhash`
(rel transcription), `files`. `core.relocate.relocate_document(old_rel → new_rel)`
**déplace le graphe complet** via le ledger et met à jour **toutes** ces
références en une transaction. La transcription est localisée via le `source` du
résumé (donc une transcription orpheline sous un ancien slug est récupérée et
réalignée). Appelé avec `old_rel == new_rel`, il sert de **réalignement
idempotent** (ramène une transcription égarée à sa place sans rien renommer).

> ⚠️ **Dette restante** : les opérations historiques `classify apply` /
> `organize` / `entities rename|merge` ne passent pas encore toutes par
> `relocate_document` (elles déplaçaient un sous-ensemble) → à **retrofitter**
> pour que tout le flow garantisse la cohérence du graphe. Voir roadmap.

### Historique — naviguer le passé (`ledger snapshot`)

Même principe que `- Sujets` mais pour le **temps** : `ledger snapshot` génère
`~/Documents/- Historique/` avec **un sous-dossier par jour** (`AAAA-MM-JJ`) qui
reconstruit l'**ancienne arborescence** (noms + structure d'avant les
déplacements) en **symlinks** pointant l'emplacement **actuel** de chaque
fichier. La chaîne old→new est suivie (un fichier déplacé plusieurs fois pointe
sa position finale), et seules les **origines** sont incluses (pas les chemins
intermédiaires d'une chaîne). Lecture seule, régénérable, quasi gratuit (données
déjà dans `file_ledger`) ; fichier disparu (corbeille purgée) → marqueur
`.disparu`. Distinct de `revert` : on **navigue** le passé sans rien défaire.
`--run` cible un run ; `--clear` retire la vue.

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
