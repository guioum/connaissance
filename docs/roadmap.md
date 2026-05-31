# Roadmap & TODO

Liste vivante de ce qu'il reste à faire. Cocher quand c'est livré, retirer
quand c'est obsolète. Priorités indicatives : 🔴 haute · 🟡 moyenne · 🟢 basse.

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

## Corrections & dette technique

- [ ] 🟡 **`scope.py` et `audit_archive.py` utilisent `HOME / "Documents"`** au
  lieu de `BASE_PATH / "Documents"` — faux en **cowork** (HOME ≠ BASE_PATH →
  pointe hors du montage). Centraliser sur `paths.DOCUMENTS_DIR`.
- [ ] 🟡 **Auditer les `fm.get("…", [])`** susceptibles du même bug que
  `verifier_liens_casses` : un champ liste vide en YAML parse en `None`, pas
  `[]`, et plante à l'itération. Garder le motif `(fm.get("x") or [])`.
- [ ] 🟢 `hash_file()` dans `commands/documents.py` est du **code mort** (aucun
  appelant) — retirer.
- [ ] 🟡 **Pas de suite de tests** : ajouter `pytest`, en commençant par les
  composants `core/` purs et déterministes — `dedup` (simhash, clustering),
  `filtres` (scoring courriels), le cache de `tracking` (hit/miss sur
  `size`/`mtime`).
- [ ] 🟢 `mcpb/server/package.json` est figé à `2.13.0` alors que pyproject +
  manifest suivent les releases — synchroniser ou documenter pourquoi il dérive.
- [ ] 🟢 `uv.lock` est présent mais non suivi par git — décider : le tracker
  (lock reproductible) ou l'ignorer explicitement.

## Documentation

- [ ] 🟡 **Dérive du décompte d'outils** : le README et `CLAUDE.md` disent
  « 42 outils » / « 12 groupes » ; la réalité est **48 outils** / **13 groupes**
  (`manifest.json` dit déjà 48). Reconcilier, et ajouter une ligne pour le step
  `quasi_doublons`.
- [ ] 🟢 Le Quick start du README référence `connaissance-2.1.0.mcpb` (version
  figée dans l'exemple) — utiliser une version générique.

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
