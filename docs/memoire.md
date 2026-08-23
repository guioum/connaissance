# Mémoire perso — Apple Notes (système minimaliste) + export Markdown

> **Statut (2026-08-23).** La mémoire personnelle *authored* vit dans **Apple
> Notes**, organisée selon le *système minimaliste* (une note par sujet, les
> actions dans Rappels). Sa face machine est l'**export Markdown quotidien**
> `~/Archives/Notes/` (frontmatter YAML, versionné git — job `export-apple-notes`
> de `mac-automations`, via `anotes export --incremental --git`). La spec du
> système et les règles pour l'assistant sont dans le plugin
> [`minimaliste`](https://github.com/guioum/guioum-plugins/tree/main/minimaliste)
> du marketplace `guioum-plugins`.
>
> **La mémoire OKF (`~/Connaissance/Mémoire/`) a été retirée le 2026-08-23.**
> Créée le 2026-07-14, elle n'a jamais dépassé un fichier : le contenu qu'elle
> visait (décisions, contexte de sujet, réflexions) est exactement celui que le
> système minimaliste assigne à Notes, et l'export anotes fournit déjà ce qui
> justifiait OKF — des `.md` à frontmatter, cherchables, diffables — sans un
> troisième conteneur à alimenter. Détail des approches écartées en fin de doc.

## Les trois mémoires

| | Mémoire fichier Claude Code | Apple Notes (+ export) | Pipeline `connaissance` |
|---|---|---|---|
| Contient | Atomes comportementaux (`user`, `feedback`) + pointeurs | Savoir *authored* : sujets (état, décisions, historique), référence, écriture personnelle | Savoir *dérivé du monde* : documents, courriels → transcription → résumé → synthèse |
| Rappel auto | Oui (`<system-reminder>`) | Non (Claude lit à la demande via le MCP `anotes` ou l'export) | n/a |
| Vérité | La note | La note Apple (l'export est une copie) | Le document source |
| Change par | Claude | L'utilisateur (et l'assistant, sur validation) | Le pipeline |

**Règle de routage** :
- Savoir de **code/projet** → **repo de code** (`docs/`, `CLAUDE.md`).
- Savoir **perso non-code** → **Apple Notes**, selon le système minimaliste
  (note-sujet, référence ou écriture personnelle — jamais d'action dans une note).
- **Atomes comportementaux** + pointeurs → mémoire fichier Claude.

## Frontière avec `connaissance`

Deux mémoires de nature opposée, sur deux axes différents :

- **`connaissance`** possède les **entités** (~900 interlocuteurs) et ce que le
  monde a écrit à leur sujet. Source de vérité = les documents ; régénérable.
- **Notes** possède les **sujets** (~30 thèmes : Toiture, Impôts, Véhicule…) et
  ce que le monde n'a pas écrit : l'état, les décisions, ce qui s'est dit au
  téléphone. Source de vérité = la note ; aucun amont.

La relation sujet ↔ entité est N:N (Toiture touche un couvreur, un assureur ;
l'assureur touche Toiture, Véhicule, Hypothèque). On ne cherche **pas** à les
aligner. Le **pont est le nom du sujet** : la note `Toiture` ↔ le champ `sujet`
de `doc_classification` ↔ la vue `~/Connaissance/Vues/Sujets/toiture/`.

- **Pont à sens unique** : une note *référence* la base (« connaissance : sujet
  toiture »), jamais l'inverse — sinon le graphe dérivé n'est plus régénérable.
- **Aucune migration de contenu**, dans aucun sens. Un sujet ne « se termine »
  pas : la note coule dans sa zone quand elle cesse d'être modifiée. L'historique
  *authored* reste dans la note ; ce que le pipeline a capté reste dans les
  chronologies.
- **Un seul magasin d'actions : Rappels.** Les engagements détectés par la
  synthèse (`- [ ]` des `chronologie.md`, commande `actions`) sont des
  *candidatures* pour la capture, pas une liste parallèle.
- **Ingestion** : `notes copy` lit l'export `~/Archives/Notes/` (voir
  [pipeline.md](pipeline.md)). Les dossiers *vivants* du système (zones Perso /
  Finances / Entreprise, Notes rapides) ont vocation à en être **exclus**
  (`filtres.yaml`, `notes.dossiers_ignores`) : ce sont de l'authored à état, pas
  des documents — ils restent cherchables via une collection qmd dédiée sur
  l'export. Les anciens dossiers (imports, archives) restent ingérés.

## Collections qmd (à faire)

qmd n'a qu'une collection `connaissance` sur tout `~/Connaissance`. Cible :
une collection `notes` sur `~/Archives/Notes/` (l'export anotes) pour que la
mémoire authored soit cherchable sans passer par le pipeline, puis — plus tard —
découper `connaissance` par couche (`transcriptions` / `resumes` / `synthese`)
pour éviter les quasi-doublons du triplet. Touche les skills du plugin
`connaissance` qui codent `["connaissance"]` en dur. Config :
`~/.config/qmd/index.yml`, CLI `qmd collection add`, embeddings locaux.

## Historique des approches écartées

- **Mémoire OKF** (`~/Connaissance/Mémoire/`, [Open Knowledge
  Format](https://github.com/GoogleCloudPlatform/knowledge-catalog), fichiers
  `.md` à frontmatter `type` requis, lus dans Obsidian) — créée le 2026-07-14,
  **retirée le 2026-08-23** : un seul fichier en cinq semaines ; redondante avec
  Apple Notes une fois le système minimaliste adopté, et l'export anotes donne
  les mêmes propriétés sans rien écrire à la main. La règle de frontière
  (pont à sens unique) lui survit.
- **Basic Memory** (serveur MCP Markdown + graphe observations/relations) —
  installé puis **retiré le 2026-07-14** : sur-outillé pour l'usage réel (qmd
  fait la recherche ; graphe et embeddings anglophones inutilisés).
- **Pont Tailscale / HTTPS** (exposer un serveur MCP à l'app iPhone) — abandonné
  le 2026-06-24 : l'app Claude fetch le MCP depuis le **cloud Anthropic**, qui ne
  voit pas un tailnet privé → `tailscale serve` (sans funnel) structurellement
  invisible.
- **Pont mémoire cloud** (`#cloud` → mémoire chat de Claude, partagée mobile) —
  testé **et fonctionnel** (Mac→iPhone), mais abandonné : push manuel, fragile,
  mémoire cloud opaque/lossy. Le téléphone lit désormais Apple Notes nativement.
