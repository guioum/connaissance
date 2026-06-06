# Roadmap & TODO

Liste vivante de ce qu'il reste à faire. Cocher quand c'est livré, retirer
quand c'est obsolète. Priorités indicatives : 🔴 haute · 🟡 moyenne · 🟢 basse.

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
  `ledger_run`. Restent les **suppressions** d'`optimize` (dedup/orphelins) qui
  attendent la « corbeille ledger » ci-dessous (un `unlink` n'est pas réversible
  sans préserver le fichier).
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
- [ ] 🟢 **Quarantaine — déplacement physique optionnel** : pour les secrets à
  faire VOYAGER hors du dossier (ex. regrouper sous `- Protégés/secrets/`),
  une action plan→apply journalisée au ledger. Distinct du garde-fou actif
  ci-dessus (qui suffit à exclure du pipeline sans rien bouger).
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
  **tag `sujet:`** dans le frontmatter (aujourd'hui le sujet est calculé mais pas
  encore posé en frontmatter), et le run du corpus complet.
- [ ] 🟡 **Sujets = vue virtuelle unique** (décision actée — modèle sujets) :
  un doc est classé physiquement par ENTITÉ + tagué `sujet:` ; une seule vue
  `~/Documents/- Sujets/` (symlinks, régénérable) rassemble par sujet et
  **remplace `- Par catégorie/`** (la catégorie devient un sujet grossier).
  Virtuel par défaut ; physique = exception (`divers/<sujet>/`). Pour le cas
  « envoi au comptable » : commande `sujet export <nom>` (copie/zip réel à la
  demande), pas de dossier physique permanent.
- [ ] 🟡 **Phase D — Doublons** : exacts (SHA256 caché) + quasi (SimHash texte).
  Rail prêt : table `doc_simhash` + `TrackingDB.get_or_compute_doc_simhash`
  (référentiel `~/Documents`, NFC) — **ne pas réutiliser `text_simhash`** (corpus,
  référentiel `~/Connaissance`) pour éviter la collision de référentiels.
- [ ] 🟢 Groupes B/C/D classés par logique propre (code regroupé, médias par
  date, exports tels quels).
- [ ] 🟢 « Corbeille ledger » : transformer les suppressions (dedup) en
  déplacement vers une zone réversible plutôt qu'un `unlink`.

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

- [x] **Décompte d'outils reconcilié** : README + `CLAUDE.md` à **61 outils /
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
