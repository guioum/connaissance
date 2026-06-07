# Roadmap & TODO

Liste vivante de ce qu'il reste à faire. Cocher quand c'est livré, retirer
quand c'est obsolète. Priorités indicatives : 🔴 haute · 🟡 moyenne · 🟢 basse.

## Sujets : précédence par maturité (résumé > pré-classement)

- [x] **Le sujet de contenu supersède le sujet provisoire** (v2.49.0) : avant,
  la vue `- Sujets` faisait une **union plate** de `doc_sujets` (source `classify`,
  deviné du dossier d'origine — bruit : `archive-*`, `2018-02`, `non-organisées`)
  et de `doc_classification.sujet`, et les sujets « propres » des résumés
  **n'étaient même pas branchés**. Désormais : (1) `summarize register` synchronise
  le `sujet:` du résumé vers `doc_sujets` avec **`source='resume'`** (clé = le
  document, via le `source` de la transcription) ; (2) `sujet_memberships()`
  applique une **précédence** : si un sujet `resume` existe pour un doc, il est
  seul affiché (supersède `classify`) ; sinon repli sur `classify` **filtré du
  bruit** (`_is_junk_sujet` : dates nues, archive/triage, génériques) ; `dedup`
  (cross-filing) toujours additif. (3) Les sujets sont **normalisés via `slugify`
  (accents conservés**, comme les slugs d'entité) à l'écriture (classify + resume)
  → fin des variantes `cafes`/`cafés` pour les nouveaux. Validé : vue passée de
  sujets pollués à 81 sujets propres, 0 bruit. Même principe que entité/catégorie
  (pré = brouillon, résumé = autorité). Les sujets `classify` existants restent
  provisoires (régénérables) et se font superséder au fil des résumés.

## Taxonomie de catégories (data-driven)

- [x] **Catégorie `professionnel` + réconciliation des fuites** (v2.48.0) :
  profilage du corpus classé (doc_classification + résumés) → `divers` = 40 % et
  `emplois` mélangeait RH et livrables ; ~24 % du corpus = **produit du travail**
  éparpillé (Guillaume = consultant, taxonomie pensée pour l'admin perso). Ajout
  de la catégorie **`professionnel`** (livrables/réunions/projets/formation/suivi
  de temps), bornée vs `emplois` (relation d'emploi : contrat/paie/CV/RH). Fuites
  des anciens résumés réconciliées via `canonicalize_category()` (finances→banque,
  santé→sante, voyages→transport) + prompt durci (« n'invente pas de catégorie ;
  cuisine/voyages/projets → `divers` + champ `sujet` »). Validé sur lot test 200 :
  `divers` 95→56 (−41 %), `professionnel` = 60 (30 %), `emplois` 28→17. Source
  unique : `prompts/_category_rules.md` + `CANONICAL_CATEGORIES`.
- [x] **Remap des anciens résumés hors-liste** (v2.48.1) : 14 résumés à catégorie
  non canonique normalisés (backup `~/Connaissance/.trash/category-remap-20260607`).
  Content-aware : les 6 `finances` étaient en fait des **impôts** (chemins
  `impôts-*`, appel comptable TPS/TVQ) → `impots` (pas `banque`) ; `projets` d'un
  client FMRQ → `professionnel` ; `cuisine`/`organisation` → `divers` + thème
  préservé dans un nouveau champ `sujet:`. Leçon : **`finances` retiré du mapping
  auto** (ambigu banque vs impots → `None` → mis en revue plutôt que deviné).
  Vérif : 0 catégorie hors-liste restante dans les 278 résumés.

## Cohérence pré-classement ↔ classement final

Les pièces passent par DEUX classements : le pré-classement (signaux gratuits,
Phase C) qui range/renomme, puis le classement final (`organize`) à partir du
résumé MD (post-OCR) qui re-range/renomme. Objectif : une même pièce résout la
MÊME entité dans les deux passes (sinon double rangement + churn).

- [x] **B — Slug toujours recalculé** (v2.46.0) : `organize` faisait confiance au
  `entity_slug` du frontmatter (que le prompt résumé produisait *sans* accents →
  `revenu-quebec`), alors que le pré utilise `construire_slug` (*avec* accents →
  `revenu-québec`) → entité dédoublée + redéplacement au final. Désormais
  `organize` recalcule `construire_slug(entity_name)` (source unique de vérité) ;
  le prompt résumé est corrigé (accents conservés, slug dérivé du nom).
- [x] **A — Bloc « discipline d'entité » partagé** (v2.46.0) :
  `prompts/_entity_discipline.md` (normalisation vs entités connues, BNC≠BDC,
  doc de travail ≠ banque, anti-devinette, accents) injecté dans LES DEUX prompts
  (`classify` + `resume_document`) via `classify.entity_discipline_suffix()`, avec
  la même liste d'entités connues. Le classement final applique enfin la rigueur
  d'entité du pré (avant : aucune, il pouvait ré-introduire BNC→BDC / halluciner).
- [x] **C — Amorçage du final par le pré** (v2.46.0) : `summarize prepare` joint
  la transcription à sa fiche `doc_classification` (via le `source` du frontmatter)
  et passe le pré-classement (entité/cat/date/titre) comme HINT au résumé — à
  confirmer/corriger avec le texte complet. Chaîne heuristique → pré (signaux) →
  final (OCR), ancrée : la pièce reste où le pré l'a mise sauf contradiction du
  texte → churn minimale. Prouvé end-to-end (test synthétique) ; inactif tant que
  les pièces pré-classées ne sont pas OCRisées (ensembles disjoints aujourd'hui).
- [x] **Aligner les règles de catégorie** (v2.47.0) : fragment partagé
  `prompts/_category_rules.md` (table de valeurs + priorité 1→13 + précisions
  abonnements/placement/bourse/inscription), injecté dans `classify` ET les
  templates de résumé via `shared_classification_suffix()` (ex-`entity_discipline_suffix`,
  qui combine désormais entité + catégorie + entités connues). Les tables inline
  sont retirées de `classify_document.md`, `resume_document.md`,
  `resume_courriel.md` → une seule source de vérité, même taxonomie/priorité
  dans les deux passes.
- [x] **Nettoyage du résumé extractif** (v2.46.1) : les variables `keywords`/
  `sentences` du prompt `classify` (mortes depuis que l'extrait brut les remplace,
  v2.45) sont retirées, avec `noise_keyword_tokens`/`_GENERIC_KW_NOISE`. **Décision
  actée — NE PAS pointer la dédup sur `excerpt`** : le fingerprint quasi-doublon
  (`duplicates._summary_text`) reste sur le résumé extractif (phrases Luhn +
  mots-clés), qui échantillonne TOUT le texte (≤ 4000/20000 car.) ; l'`excerpt`
  (1500 car. = surtout l'en-tête commun) fusionnerait à tort deux relevés mensuels.
  `summary` n'est donc PAS mort : consommateurs vivants = dédup + haystack du hint
  heuristique. (YAKE resterait marginal pour ces deux usages — non retenu.)

## Intégrité référentielle des déplacements

- [x] **Primitive `core.relocate.relocate_document`** (v2.42.0) : déplace le
  graphe complet d'un document (source + transcription + résumé) et met à jour
  toutes les références (`source` frontmatter, `doc_*`, `text_simhash`, `files`)
  en une transaction ledger. Localise la transcription via le `source` du résumé
  (récupère les orphelines) ; `old==new` = réalignement idempotent. Tests.
  Appliqué : ré-accent des 79 noms de fichiers (triplet, via la table d'accents
  apprise des titres de résumés) + réalignement des transcriptions orphelines
  des entités renommées/fusionnées.
- [x] **`entities rename|merge` sur `relocate_document`** (v2.43.0) : chaque
  document de l'entité passe par la primitive (graphe complet + refs) ; le reste
  (fiche Synthèse, Courriels/Notes, orphelins) est balayé via le ledger. C'était
  la source de l'orphelinage des transcriptions. `classify apply` reste pré-OCR
  (pas de triplet → juste source + relink, correct) ; `organize` déplace déjà le
  triplet + met à jour `source` (mature).
- [ ] 🟢 **Uniformiser `organize` sur `relocate_document`** : il gère déjà
  triplet + `source`, mais ne met pas à jour `text_simhash`/`doc_classification`
  (flow legacy). Le faire passer par la primitive pour une cohérence totale.
- [ ] 🟢 **Nettoyer les transcriptions orphelines sans doc vivant** (résidu des
  fusions/dédup : ~10 sous d'anciens slugs `revenu-quebec`/`bdc`/`ville-de-montreal`)
  — un step `audit` qui les repère/archive.

## Grand chantier : réorganisation de ~/Documents (pré-classement)

Objectif : ranger un `~/Documents` très désordonné (~67k fichiers, ~48 Go, 23
niveaux, 3 logiques de classement contradictoires) selon la logique du système
(`organismes/personnes/divers/promus`, renommé `AAAA-MM-JJ titre.ext`), **sans
OCR Mistral** (signaux gratuits : chemin, nom, dates, métadonnées, texte
embarqué born-digital, cache OCR existant, extractive summarization) et de façon
**réversible**. Décisions actées : Ledger d'abord ; **dry-run only** pour la
grande réorg tant que ce n'est pas validé.

- [x] **Phase 0 — Ledger réversible** (v2.19.0) : `core/ledger.py` (`safe_move`
  journalisé + `revert_run` vérifié par hash), table `file_ledger`, groupe CLI
  `ledger list|show|verify|revert` + 4 outils MCP. Toute modif de nom/dossier
  passe par là ; rollback par run, ne restaure que si le hash est intact.
- [x] **Retrofit des déplacements** (v2.19.1, étendu v2.21.2) : TOUS les
  déplacements de fichiers passent par `safe_move` → journalisés et révertibles :
  `organize` (résumé/transcription/document source/fils), `audit
  archive-non-documents`, `emails cleanup-obsolete`. Chacun retourne/expose un
  `ledger_run`. Les **suppressions** d'`optimize` (dedup/orphelins) passent
  désormais par la « corbeille ledger » (v2.34.0, ci-dessous) — plus aucune
  mutation FS hors ledger.
- [x] **Phase A — Triage A/B/C/D** (v2.20.0, affiné v2.20.1) : `documents triage`
  cartographie ~/Documents en 4 groupes (lecture seule). Les **conteneurs de
  code/projet** (marqueur fichier OU dossier `.git`/`.claude`, bundles `.app`)
  sont comptés en **unités** ; les **exports sont parcourus** (un vieux Google
  Drive contient de vrais documents — bulletins, livres — qui remontent en A).
  Les **conteneurs** (13 repos de code, 23 paquets macOS `.app`/`.abbu`/`.ynab4`)
  sont des unités exclues du décompte ; une **détection « archive » par densité**
  met de côté le **résidu non-documentaire** des dossiers volumineux quasi sans
  documents (dumps : codebase 2020, cruft Takeout) — mais leurs **vrais
  documents sont toujours extraits vers le groupe A** (jamais enterrés : ex.
  les diplômes des enfants dans `_Permanent/Souvenirs` remontent en A).
  Enfin, les **dossiers thématiques cohérents** (impôts d'une année, formations,
  contrats/dossiers clients) sont détectés par nom + garde-fou « contient des
  documents » et **gardés groupés** comme unités (futurs sujets), pas éclatés
  par entité (44 dossiers / 1260 docs sur le corpus). Trois sorts : vrac
  (classer individuellement) · dossiers groupés (unités) · conteneurs (de côté).
  Corpus réel — EN VRAC à classer : **9,5k documents** · 9,4k médias · 4k code ·
  0,6k autre · 0,2k exports divers ; + 42k fichiers mis de côté en
  6 repos · 13 paquets · 16 archives. Heuristiques tunables (`commands/triage.py`).
- [x] **Garde-fou secrets — `documents secrets`** (v2.22.0) : scan lecture
  seule de ~/Documents qui repère les fichiers contenant des **clés/mots de
  passe/jetons** pour **quarantaine** (jamais classés en clair, jamais indexés
  qmd, jamais envoyés à un service externe — OCR Mistral/Batch API). Secret
  scanning léger, **zéro dépendance** (`core/secrets.py` : préfixes connus
  AKIA/ghp_/sk-/AIza/xox…, blocs PEM, JWT, `user:pass@host`, affectations
  `password=` gated entropie, colonnes CSV « password » → cas `credentials.csv`)
  + signal nom de fichier (`.env`, `id_rsa`, `*.pem`, `*.pfx`, `*.kdbx`… ; le
  `.key` Keynote n'est PAS pris pour une clé). Lecture via le **SSD**, jamais
  de download iCloud (`dataless` → nom seulement). Évidences **caviardées**.
  Presidio (PII, lourd) écarté : mauvais outil pour des secrets. Détecteur
  enrichi (v2.22.1–2) : +patterns providers (Azure/GCP/Twilio/SendGrid/npm/
  PyPI/Telegram/Discord…), entropie Base64/Hex **gated par mot-clé** (l'entropie
  libre = bruit massif sur un corpus perso), clés camelCase (`apiSiteKey`).
  **Validé par test comparatif vs detect-secrets** (éphémère, jamais intégré) :
  même set de vrais secrets, detect-secrets rate le binaire (keystores/.p12/
  .kdb) et les colonnes CSV, et n'apportait qu'**un** vrai manque (corrigé).
- [x] **Garde-fou ACTIF — quarantaine pipeline** (v2.23.0) : `filter_document`
  rejette (a) le matériel cryptographique reconnu au **nom** (clé privée,
  keystore…) → `secret_filename`, et (b) tout chemin de la **liste de
  quarantaine** `~/Connaissance/.config/secrets-quarantine.txt` →
  `secret_quarantine`. Un fichier listé est ainsi **exclu de l'OCR, de l'index
  qmd et du Batch API** (chokepoint unique). `documents secrets --quarantine
  [--include-medium]` peuple la liste (high par défaut) — **écrit une config,
  ne déplace/supprime rien**, idempotent, éditable.
- [x] **Quarantaine — déplacement physique optionnel** (v2.35.0) :
  `documents secrets --relocate` regroupe les fichiers en quarantaine sous
  `~/Documents/- Protégés/secrets/` **via le ledger** (réversible, structure
  préservée), met à jour la liste de quarantaine, dry-run sauf `--apply`.
  Distinct du garde-fou actif (qui suffit à exclure du pipeline sans rien bouger).
- [x] **Phase B — Extraction de signaux (groupe A, zéro OCR)** (v2.24.0) :
  `documents signals` produit un « paquet de signaux » par document via une
  **cascade du moins cher au plus cher** (n'ouvre pypdfium2 qu'en dernier
  recours) : nom + chemin + **dossier d'origine** (→ sujet) + dates FS + type
  (stdlib) → **métadonnées Office** (`docProps`, stdlib zip+XML) → **cache OCR**
  existant → texte Office/plain (stdlib) → **couche texte PDF born-digital**
  page 1 (`pypdfium2`, extra optionnel `connaissance[pdf]`). Détecte
  **born-digital vs scanné**. **Résumé extractif maison** (Luhn + mots-clés +
  entités montants/dates/refs, `core/summarize_extractif.py`, zéro dépendance —
  T5/GPT/gensim/sumy écartés). Garde-fous : conteneurs élagués, **secrets
  exclus** (quarantaine + nom), lecture **SSD** (dataless → nom/chemin seuls,
  pas de download), **cache** `tracking.db` (table `doc_signals`). Validé sur
  1 804 docs réels : **86 % born-digital** (texte gratuit), 199 scannés (futur
  backlog OCR), origines révèlent les entités/sujets. Rien déplacé.
- [x] **Phase C — Pré-classement hybride** (v2.25–2.30) : pipeline complet,
  validé de bout en bout sur 1803 docs réels. Briques : (1) **hint heuristique**
  `core/classify.py` (date/entité/catégorie/sujet/titre + confiance ; solidifié
  sur le vrai corpus : strip mots-de-type + scanner→dossier, catégorie par
  contenu, keywords nettoyés du foyer/boilerplate) ; (2) **`classify prepare`** —
  signaux + hint + entités connues → requêtes Batch (entités connues en system
  cacheable) ; (3) **submit_batch** (Haiku 4.5 — A/B vs Sonnet = match nul, donc
  Haiku par défaut, ~quelques $ tout le corpus) ; (4) **`classify register`** —
  résultats validés (catégorie canonique, date) + réconciliation entité
  (`resolution.py`) → manifeste plan→apply, basse confiance → **zone d'attente** ;
  (5) **`classify apply`** — déplacement **ledger** (réversible), **dry-run par
  défaut**. Validé : 33 auto / 9 attente sur l'échantillon, destinations propres
  (`RE-101(2020-01).pdf` → `organismes/revenu-quebec/2019-12-30 …`). Reste : le
  run du corpus complet (dry-run only tant que non validé). Le **sujet** est
  source-de-vérité dans `doc_classification.sujet` (v2.35.0 — pas de frontmatter
  sur un PDF brut ; la vue `- Sujets` lit la colonne).
- [x] **Calibrage Phase C sur lot test réel** (v2.44.0) : 3 batches test de 200
  docs (Haiku 4.5, ~$0,60) ont réglé prompt + porte auto. **Constat clé : le
  prompt caching ne fire PAS en Batch** (workers parallèles froids — vérifié,
  `cache_read=0`) ⇒ corpus complet ≈ $9,6 en 1 doc/requête ; le seul levier
  déterministe d'amortissement du system serait de **grouper N docs/requête**
  (K=10 ≈ $5,7, mais réécriture prepare/register + retest). Réglages livrés :
  (a) prompt — désambiguïsation entités (BNC=Banque Nationale ≠ BDC ; doc de
  travail ≠ banque ; diplôme→organisme), `abonnements` resserré (services
  récurrents only), règle **anti-devinette** (pas d'émetteur clair →
  `entity_type=divers`, ne pas piocher une entité connue au hasard) ;
  (b) `classify register` — **porte auto assouplie** : auto dès que la fiche est
  structurellement complète (type exploitable + entité + catégorie + date), la
  confiance basse ne bloque plus (réversible via ledger) ; `auto_low_confidence`
  exposé, `attente_reasons` restreint aux attente. Effet lot 200 : auto 83→97,
  zéro BDC, zéro hallucination en auto, `abonnements` maîtrisé.
- [x] **Signal premier = extrait du texte brut, plus le résumé extractif**
  (v2.45.0) : le résumé Luhn + mots-clés-par-fréquence était un proxy trop faible
  (mots vides, phrases répétées) ET le texte extrait (PDF born-digital 4000 car.,
  Office/plain 20000) était **jeté** après résumé. Désormais `extract_signals`
  garde un champ **`excerpt`** (1500 car., espaces compactés) ; `classify prepare`
  l'envoie dans le prompt à la place des mots-clés/Luhn (entités regex conservées).
  Schéma de signaux **versionné** (`SIGNALS_SCHEMA_VERSION`) → le cache `doc_signals`
  recalcule les entrées antérieures. Règle prompt **durcie** : l'entité doit être
  NOMMÉE dans le document, jamais « par défaut/par contexte » (le dossier n'est pas
  une preuve d'émetteur) — corrige la régression où l'extrait rendait Haiku
  *confiant dans une entité devinée*. Effet lot 200 : auto 97→102, classements
  ancrés dans le vrai texte (employeur lu sur la fiche de paie, etc.), confiance
  basse 17→11. Coût corpus ≈ $10,5 (+~$1 vs v3, extrait ~+580 tok/doc). Reste :
  YAKE pour les mots-clés *stockés* (qmd/metadata) et piste Ollama/OCR local —
  non faits, cf. [[caching-inefficace-en-batch]] pour le contexte coût.
- [x] **Sujets = vue virtuelle unique** (v2.35.0 — modèle sujets) :
  `sujet view` génère `~/Documents/- Sujets/<sujet>/` en symlinks depuis
  `doc_classification.sujet` (régénérable), **remplace `- Par catégorie/`** ;
  `sujet export <nom>` (`--zip`) matérialise un sujet à la demande (copie/zip,
  ex. comptable) sans dossier physique permanent ; `sujet list` compte.
- [x] **Phase D — Doublons** (v2.35.0) : `duplicates scan` détecte exacts
  (SHA256) + quasi (SimHash texte du résumé extractif, cache `doc_simhash`,
  référentiel `~/Documents`, distinct de `text_simhash`) ; `duplicates plan`
  garde un keeper par cluster ; `duplicates apply` envoie les doublons à la
  **corbeille ledger** (réversible, dry-run par défaut).
- [x] **Groupes B/C/D par logique propre** (v2.35.0) : code et exports gardés en
  unités par le triage (Phase A) ; `media plan|apply` range les **médias** sous
  `~/Documents/- Médias/AAAA/MM/` par date (nom sinon filesystem), via le ledger,
  dry-run par défaut.
- [x] **« Corbeille ledger »** (v2.34.0) : `optimize` dedup ET cleanup_orphans
  n'`unlink` plus — ils envoient le fichier à `~/Connaissance/.trash/<run_id>/`
  via `ledger.safe_trash` (`op='trash'`), réversible par `ledger revert` et
  détruit seulement par `ledger purge [--run | --older-than-days]` (dry-run par
  défaut, `--apply` pour exécuter). `optimize apply` expose `ledger_run` +
  `trashed_recoverable` ; l'espace n'est « libéré » qu'à la purge. Le pruning de
  dossiers vides reste un `rmdir` direct (aucune donnée).
- [x] **Dédup du registre d'entités** (v2.36.0) : `entities candidates` repère
  les quasi-doublons d'entités (containment, Jaccard, edit distance, acronyme ;
  variantes annuelles `impots-2023/2024` exclues) sur l'union fiches Synthèse +
  `doc_classification`. `entities merge --from --into` (plan→apply) repointe la
  DB (atomique), fusionne les aliases dans la fiche gardée, déplace les résumés
  (ledger) et corbeille la fiche perdante. Validé sur le vrai registre (111
  entités → 16 paires : ville-de/ville-montreal, monteillet-conseil(-inc)…).

## Améliorations

### Déduplication (suite de la v2.15.0)

- [ ] 🔴 **Phase 2 hybride — repli image** : pHash **multi-pages** pour les
  fichiers bruts non transcrits (relevés sous `~/Documents/- Protégés/`, etc.).
  La page 1 seule échoue sur les gabarits ; hasher toutes les pages sépare deux
  relevés mensuels (pages de transactions différentes). Lire le **SSD** (pas
  iCloud). Brancher sur le même rapport `audit check`. Voir
  [pipeline.md](pipeline.md).
- [ ] 🟡 **Action de nettoyage** : `optimize` plan→apply qui fusionne **seulement**
  les clusters `doublon_probable` (jamais `recurrent_probable` ni
  `classement_croise`).
- [ ] 🟢 Étendre `quasi_doublons` aux **Courriels** et **Notes** (aujourd'hui
  Documents uniquement).
- [ ] 🟢 `recurrent_probable` se fie à la date dans le **nom** de fichier — la
  croiser avec la date du **frontmatter** pour robustesse.
- [ ] 🟢 Le clustering est O(n²) — passer à un BK-tree ou un LSH par bandes si le
  corpus de transcriptions grossit beaucoup (aujourd'hui quelques centaines,
  largement OK).

### SSD comme cache de lecture (v2.16.0)

- [x] **Câblage SSD-aware** : `documents_cache_root()` / `documents_read_path()`
  / `is_dataless()` dans `paths.py` ; `get_or_compute_hash(read_path=)` lit le
  miroir mais indexe sous le canonique ; `documents scan` émet `read_source`.
  Routé dans `documents` (scan/register) et `audit reindex`. Voir
  [environments.md](environments.md).
- [ ] 🔴 **Skill `transcrire` : OCR depuis `read_source`** — le gros gain de
  masse. Le CLI émet déjà `read_source` (SSD) ; le skill/MCP d'OCR doit lire ce
  chemin (pas `source`) pour éviter de matérialiser iCloud à chaque nouveau
  document. `register` garde `source` comme identité. (Repo séparé du shim de
  skills.)
- [ ] 🟢 Mode `--materialized-only` pour les passes de masse **sans** SSD :
  sauter les fichiers `dataless` (helper déjà présent) au lieu de déclencher
  des téléchargements.
- [ ] 🟢 `optimize` ne bénéficie pas du SSD (lit des PJ sous `Connaissance/` et
  des `promus/` fraîchement écrits) — laissé tel quel, documenté.

## Corrections & dette technique (v2.16.1)

- [x] **Bug None-iteration** (même classe que `liens_casses`) : `resolution.py`
  itérait `fm.get("aliases", [])` qui vaut `None` si le champ YAML est vide
  (et `fm` lui-même pouvait être `None`). Corrigé + durcissement défensif
  `or []` dans `filtres` (attachments), `audit_archive` (items),
  `organize` (candidates). `synthesis` utilisait déjà le motif sûr.
- [x] **Code mort** : `hash_file()` (+ import `hashlib`) retiré de
  `commands/documents.py`.
- [x] **Suite `pytest`** (`tests/`) : `dedup` (pur), cache `tracking` (JIT +
  `read_path` SSD), scoring `filtres` (configs injectées). 22 tests, portables.
  Lancer : `uv run --extra test pytest`. Voir [development.md](development.md).
- [x] **`package.json` synchronisé** à la version courante (était figé à 2.13.0).
- [x] **`uv.lock` ignoré** (gitignore) : `uv tool install git+…` ne le consomme
  pas et les deps runtime sont minimales/lâches.
- [ ] 🟢 **Faux positif corrigé** : le point « `scope.py`/`audit_archive.py`
  utilisent `HOME` » était une erreur de ma part — les deux font `HOME =
  BASE_PATH` (alias), donc c'est correct. Reste un nettoyage *cosmétique*
  optionnel : importer `paths.DOCUMENTS_DIR` au lieu de redéfinir localement.
- [ ] 🟢 Étendre les tests aux modules couplés à l'environnement (`audit`
  verifiers, `resolution`) via des fixtures de répertoires tmp + monkeypatch.

## Documentation (fait)

- [x] **Décompte d'outils reconcilié** : README + `CLAUDE.md` à **72 outils /
  15 groupes** (source de vérité : les `registerTool` de `index.js`). Le palier
  intermédiaire « 48 / 13 » a été dépassé par les phases triage/secrets/signals
  (+3), classify (+4), ledger (+4), manifest (+1) et les `backlog_count`.
  Tableau README corrigé (`pipeline simulate` fantôme retiré, groupes `actions`,
  `classify`, `ledger`, `manifest` ajoutés).
- [x] **Quick start** : version figée `connaissance-2.1.0.mcpb` → générique.
- [x] **Pointeur `docs/`** ajouté en tête de `CLAUDE.md`.

## Idées exploratoires

Découlent de deux références étudiées : l'approche « LLM wiki » de Karpathy et
l'outil de ménage de fichiers `czkawka`.

### Inspirées de Karpathy (wiki compoundant)

- [ ] 🟡 **`_log.md` append-only et grep-able** : un journal Markdown
  chronologique des ingestions/opérations, dérivé de `tracking.db`
  (`## [YYYY-MM-DD] …`). `tracking.db` est parfait pour la machine mais opaque
  pour reprendre le fil à froid. Geste : `connaissance log tail`.
- [ ] 🟢 **Lint sémantique** (nouveaux steps `audit`) : contradictions entre
  résumés d'une même entité (dates/montants incompatibles), entités fréquemment
  citées **sans fiche** (lacunes), données périmées. Fait passer la synthèse de
  « génération » à « génération + contrôle qualité ».
- [ ] 🟢 **Capture query→note** : épingler une réponse de recherche comme note
  permanente (`notes` depuis une réponse de chat).

### Inspirées de czkawka (ménage pré-OCR)

- [ ] 🟡 **Détection de fichiers cassés** avant OCR (`pikepdf`) : valider qu'un
  PDF s'ouvre avant de payer un OCR voué à l'échec. À brancher en pré-vol de
  `documents scan`.
- [ ] 🟢 **Détection de mauvaises extensions** (`python-magic` / libmagic) : un
  `.pdf` qui est en réalité un JPEG, un `.docx` zip cassé — fréquent dans les
  exports de PJ, fait échouer l'OCR silencieusement.

> Note : `czkawka` lui-même (binaire Rust) n'a pas été retenu comme dépendance —
> ses fonctions utiles se font en libs Python ciblées, fidèles au core léger.
> Le perceptual hash d'image a été écarté comme moteur **principal** de dédup
> (faux positifs sur documents templatés) au profit du SimHash texte ; il revient
> en **repli** pour les fichiers non transcrits (phase 2 ci-dessus).
