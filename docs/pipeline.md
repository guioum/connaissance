# Le pipeline

Le parcours complet d'une source brute jusqu'à la synthèse, en 5 étapes
déterministes. Chaque étape est une commande CLI ; l'orchestration (l'ordre,
les confirmations) est faite par les skills du plugin cowork `connaissance` ou
à la main.

```
   scope ──► transcrire ──► résumer ──► organiser ──► optimiser ──► synthétiser
  (quoi      (OCR/extract   (résumé IA) (classement   (dédup,       (fiches,
   scanner)   /copie)                    par entité)   promotion)    chronologies)
```

## 0. Scope — quoi scanner

`scope` définit les dossiers sources pris en compte (inclusions/exclusions).
`scope scan` liste les candidats, `scope include/exclude` mute la config.
C'est le garde-fou qui évite d'aspirer tout le disque.

## 1. Transcrire — source → texte brut

Trois sources, trois commandes :

- **Documents** (`documents scan` → OCR → `register-batch`) : repère les
  nouveaux fichiers sous la source (par défaut `~/Documents`), délègue l'OCR au
  plugin externe `mistral-ocr`, écrit la transcription au chemin miroir.
  `register-batch --from-scan <manifeste>` enregistre tout le lot en réutilisant
  les chemins calculés au scan, et **remonte bruyamment** les transcriptions
  manquantes (OCR écrit au mauvais endroit) au lieu de produire des orphelins.
  `documents suspects` repère les transcriptions OCR mal formées à reprendre.
  En complément du flux Mistral : **OCR local Vision** gratuit (`documents
  ocr-local` pour les PDF scannés, `--born-digital` pour les PDF à couche texte
  — fusion structure Vision + caractères de la couche, ou OCR pur si la couche
  vient d'un vieil OCR invisible —, `ocr-images` pour les images-documents,
  `ocr-review` pour lister les transcriptions à faible confiance), et **cascade
  Vision → Mistral** via `documents transcribe-plan --max-pages N` : worklist
  des transcriptions `vision-local` à confiance ≤ 0,55 (Mistral OCR 4 réservé
  aux cas difficiles) + scannés sans transcription, avec estimation de coût.
- **Courriels** (`emails extract`) : lit les archives mbox, **score** chaque
  courriel (multi-signaux), ne capture que ce qui dépasse le seuil, regroupe
  les fils. Voir [emails.md](emails.md).
- **Notes** (`notes scan` → `copy`) : copie incrémentale de l'export Markdown
  quotidien d'Apple Notes (`~/Archives/Notes/`, job `export-apple-notes` de
  `mac-automations` via `anotes export --incremental --git`). `scan` et
  `backlog-count` rapportent une sonde de fraîcheur `export`
  (`{last_export, age_days, stale}`, seuil 7 jours) : un export en panne se
  voit, au lieu d'ingérer un instantané périmé en silence. **« Déjà copiée ? »
  se décide dans `tracking.db`**, pas par l'existence d'un miroir
  `Transcriptions/Notes/<rel>` (`organize apply` range les transcriptions par
  entité, le miroir disparaît) : `files` (`source_type='note'`) donne
  l'emplacement ACTUEL (`path`) et la note d'origine (`source_path`, toutes
  conventions historiques confondues). Une note connue est « modifiée » si le
  **hash de son corps** diffère de celui enregistré — ou, pour une copie
  d'avant le hash, si son *texte nu* diffère (les deux exporteurs ne rendent
  pas le Markdown pareil : `a_jour_rendu` compte ces faux positifs écartés).
  `copy` réécrit alors la transcription **sur place** (frontmatter enrichi
  conservé, corps de la note), et remet `files.mtime`/`hash` au présent, ce
  qui périme le résumé comme pour un document (`resumes_perimes` : préfiltre
  mtime puis hash vs `source_content_hash`). Statuts : `nouveau`, `modifie`,
  `manquante` (transcription disparue, recréée à l'emplacement enregistré).
  Les dossiers *vivants* du système minimaliste (zones Perso / Finances /
  Entreprise, Notes rapides) ont vocation à être exclus via `filtres.yaml`
  (`notes.dossiers_ignores`) — voir [memoire.md](memoire.md).

## 2. Résumer — texte → résumé IA

`summarize` ne fait **pas** l'appel IA lui-même : il **prépare** des requêtes
prêtes pour `claude-api-mcp` à partir des templates de
[`prompts/`](../src/connaissance/prompts/), puis **enregistre** les réponses.

1. `summarize plan` — liste les transcriptions sans résumé (backlog).
2. `summarize prepare --paths … --mode {direct|batch}` — écrit un manifeste de
   requêtes. Le mode `batch` passe par l'API Batch (−50 %, jusqu'à 24 h).
3. (appel via `claude-api-mcp`, hors CLI)
4. `summarize register` — post-traite les réponses, écrit les résumés au chemin
   miroir, journalise tokens/coûts dans `tracking.db`.

Le choix du modèle (Sonnet pour le narratif, Haiku pour les tâches courtes) est
dans [`core/model_selection.py`](../src/connaissance/core/model_selection.py).

## 3. Organiser — résumé → entité

`organize` classe chaque résumé sous sa personne/organisme/sujet.

1. `organize plan` — classement déterministe ; produit un manifeste.
2. `organize enrich` — pour les cas à confirmer, enrichit via recherche
   sémantique qmd.
3. `organize resolve` — résout/raffine les rattachements d'entité.
4. `organize apply manifest.json [--dry-run]` — déplace les fichiers.

## 4. Optimiser — ménage

`optimize` libère de l'espace et range les pièces jointes :

- **Promotion** : une PJ de document utile est promue vers `Documents/promus/`.
- **Déduplication exacte** : SHA256 (cache JIT) ; les doublons byte-identiques
  d'une PJ déjà connue sont retirés, avec une référence vers le gardien.

## 5. Synthétiser — entité → fiche

`synthesis` régénère les fiches, chronologies et MOC (maps of content) d'entité,
et alimente le groupe `actions` (extraction des échéances/engagements).

- `synthesis plan` — entités et MOC périmés à régénérer.
- `synthesis aliases-candidates` / `relations-candidates` — scan déterministe
  des résumés pour proposer alias et relations (à valider).
- `synthesis prepare` / `register` — même logique plan→IA→register que
  `summarize`.

## Le moteur : `pipeline detect` et le JIT

`pipeline detect --steps …` est le **tableau de bord** : il agrège des
vérifications (`stats`, `resumes_manquants`, `resumes_perimes`,
`non_organises`, `synthese_perimee`, `moc_perimes`, `couts`…) en un seul JSON.
C'est ce que les skills appellent pour répondre à « où en est la base ? ».

**JIT (Just-In-Time)** — principe directeur depuis la v2.13/2.14 : le scan ne
hashe plus systématiquement. Un fichier n'est lu/hashé que si une collision est
possible (même taille qu'un fichier connu) et que son `(size, mtime)` a changé
depuis le dernier passage. En base stable, `detect` ne fait que des `stat()` et
des lectures DB → quasi instantané. Les compteurs rapides
(`documents/notes/emails backlog-count`) suivent le même pattern « fast-count
sans hash ».

## Audit

`audit check --steps …` regroupe les vérifications d'intégrité **déterministes**
(lecture seule) :

| Step | Vérifie |
|---|---|
| `liens_casses` | les `relations` des fiches pointent vers des entités existantes |
| `frontmatter_invalide` | champs requis présents par type (voir [data-model.md](data-model.md)) |
| `triplets_desynchronises` | transcription/résumé/source cohérents |
| `attachements_manquants` | les PJ référencées existent |
| `doublons` | doublons exacts par `message_id` (courriels) |
| `quasi_doublons` | quasi-doublons de documents par **SimHash texte** |

### Quasi-doublons (SimHash texte)

`quasi_doublons` détecte les documents quasi identiques via un SimHash 64 bits
du **texte OCR** (pas un perceptual hash d'image — celui-ci fusionne des
documents distincts qui partagent un gabarit, cf. la calibration). Deux
transcriptions à distance de Hamming ≤ 3 forment un cluster, annoté :

- `doublon_probable` — même entité **et** même date → vrai doublon, candidat sûr.
- `recurrent_probable` — même entité, dates différentes → document récurrent
  distinct (relevé/reçu annuel), **à ne pas fusionner**.
- `classement_croise` — entités différentes → cross-filing intentionnel
  d'`organize`, à confirmer.

Moteur dans [`core/dedup.py`](../src/connaissance/core/dedup.py), cache dans la
table `text_simhash`. La suite (repli image multi-pages pour les fichiers non
transcrits) est dans [roadmap.md](roadmap.md).

Autres opérations d'audit : `reindex_db` (repeuple `tracking.db` depuis les
fichiers), `repair_attachments`, `archive_non_documents`.
