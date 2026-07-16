# Mémoire perso — fichiers Markdown (OKF) sous `~/Connaissance/`

> **Statut (2026-07-14).** La mémoire personnelle = un dossier de fichiers
> Markdown au format **OKF** (Open Knowledge Format) sous
> `~/Connaissance/Mémoire/`. **Aucun moteur, aucun serveur, aucune exposition
> réseau.** Recherche via **qmd**, lecture via **Obsidian**, écriture par
> **Claude en direct** (Read/Write).
>
> **Basic Memory a été retiré (2026-07-14).** On n'en utilisait qu'une fraction
> (qmd fait déjà la recherche ; l'embedding interne de BM était anglophone et
> inutilisé) pour beaucoup de machinerie (MCP, SQLite, graphe). OKF donne le même
> résultat — des `.md` interliés — sans rien à faire tourner. Détail des
> approches écartées en fin de doc.

## Pourquoi OKF

[Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog)
(Google Cloud, v0.1, Apache 2.0, lancé le 12 juin 2026) : spec **ouverte,
vendor-neutre** — un dossier de `.md` à frontmatter YAML, un fichier = un
concept, interliés par des liens markdown standard. Pas de SDK, pas de compte,
pas de lock-in ; n'importe quel éditeur de texte ou dépôt git le lit. C'est
exactement notre besoin, standardisé.

## Le format (OKF v0.1)

- 1 fichier `.md` = **1 concept**. Concept ID = chemin relatif sans `.md`.
- Frontmatter YAML : **`type` requis** (chaîne libre — vocabulaire projet :
  `Note`, `Guide`, `Décision`, `Référence`, `Personne`, `Projet`…). Recommandés,
  par priorité : `title`, `description` (une phrase), `resource` (URI si l'objet
  a une source), `tags` (liste), `timestamp` (ISO 8601, dernière modif).
- Corps : **markdown libre**. Titres conventionnels au besoin (`# Exemples`,
  `# Citations`…), non requis.
- Liens entre notes : **liens markdown standard** `[titre](/chemin)`
  (bundle-relative, commence par `/` — recommandé) — **non typés**, la relation
  se lit dans la prose. **Pas de `[[wikilink]]`.**
- Fichiers réservés optionnels : `index.md` (sommaire), `log.md` (historique).
- **Conforme** si chaque `.md` non réservé a un frontmatter parsable avec un
  `type` non vide. Les consommateurs tolèrent champs manquants, types inconnus,
  liens cassés.

Exemple minimal :

```markdown
---
type: Guide
title: Mode d'emploi de ma mémoire
description: Comment j'organise mon savoir et où ranger quoi.
tags: [meta, mémoire, okf]
timestamp: 2026-07-14
---

# Mode d'emploi de ma mémoire
Corps libre. Lien vers un autre concept : [voir la note X](/sous-dossier/x).
```

## Où ça vit, avec quels outils

| Besoin | Outil |
|---|---|
| Fichiers | `~/Connaissance/Mémoire/` (dans le vault Obsidian) |
| Écrire / lire | **Claude en direct** (Read/Write) — aucun MCP requis |
| Rechercher | **qmd** (indexe déjà `~/Connaissance/**/*.md`) |
| Lire / éditer à l'œil | **Obsidian** (natif) |
| Accès mobile | **Obsidian mobile** sur le vault — 100 % local |

## Frontière & complémentarité avec `connaissance`

Deux mémoires de nature opposée, qui ne se marchent pas dessus :

- **`connaissance`** = mémoire **dérivée du monde** (documents/courriels/notes →
  transcription → résumé → synthèse). Source de vérité = les documents ;
  régénérable, réversible. Possède les **entités du monde réel**.
- **Mémoire OKF** = mémoire ***authored*** (décisions, contexte projet,
  réflexion). Source de vérité = la note ; aucun amont. Possède le **savoir
  conceptuel**.

**Test de routage** : « ça trace vers un document/source externe ? » Oui →
`connaissance`. Non, ça naît d'une réflexion → mémoire OKF.

**Pont à sens unique** : une note OKF peut *référencer* une entité `connaissance`
(lien markdown), jamais l'inverse — sinon le graphe dérivé n'est plus
régénérable. Évite la **dérive de frontière** (deux dossiers divergents pour la
même chose).

**Étanchéité** : le pipeline ne balaie **jamais** la racine `~/Connaissance`
(seulement `Transcriptions/`, `Résumés/`, `Synthèse/`) → `Mémoire/` lui est
**nativement invisible**. Rien à exclure.

**Ne migrent PAS vers la mémoire OKF** : Transcriptions, Résumés, Courriels,
Documents — et surtout la **Synthèse** (même forme de graphe d'entités, mais
dérivée/régénérable/couplée au pipeline).

## Arbitrage des trois mémoires (routage)

| | Mémoire fichier Claude Code | Mémoire OKF (`~/Connaissance/Mémoire/`) | Pipeline `connaissance` |
|---|---|---|---|
| Rappel auto | Oui (`<system-reminder>`) | Non (Claude lit à la demande) | n/a |
| Forme | Atome court typé | Note OKF (concept + liens) | Dérivé du monde |
| Vérité | La note | La note | Le document source |

**Règle** :
- Savoir de **code/projet** → **repo de code** (`docs/`, `CLAUDE.md`).
- Savoir **perso non-code** → **mémoire OKF**.
- **Atomes comportementaux** (`user`, `feedback`) + pointeurs → **mémoire fichier
  Claude** (pour le rappel auto).

## Collections qmd (plus tard)

Aujourd'hui qmd a une seule collection `connaissance` sur tout `~/Connaissance`.
Le **triplet** fait apparaître la même source jusqu'à 3× → quasi-doublons en
résultats. Cible (différée, quand `Mémoire/` aura du contenu) : découper par
couche — `transcriptions` / `resumes` / `synthese` / `memoire` — chaque requête
tapant au bon niveau. Touche aussi les skills du repo `guioum-plugins/connaissance`
qui codent `["connaissance"]` en dur. Config : `~/.config/qmd/index.yml`,
CLI `qmd collection add`, embeddings **locaux** (pas de coût API).

## Historique des approches écartées

- **Basic Memory** (serveur MCP Markdown + graphe observations/relations) —
  installé puis **retiré le 2026-07-14** : sur-outillé pour l'usage réel (qmd
  fait la recherche ; graphe et embeddings anglophones inutilisés). OKF donne le
  même résultat sans moteur.
- **Pont Tailscale / HTTPS** (exposer un serveur MCP à l'app iPhone) — abandonné
  le 2026-06-24 : l'app Claude fetch le MCP depuis le **cloud Anthropic**, qui ne
  voit pas un tailnet privé → `tailscale serve` (sans funnel) structurellement
  invisible. Objectifs « tailnet-privé » et « app iPhone » incompatibles.
- **Pont mémoire cloud** (`#cloud` → mémoire chat de Claude, partagée mobile) —
  testé **et fonctionnel** (Mac→iPhone), mais abandonné : push manuel, fragile,
  mémoire cloud opaque/lossy. Préférence retenue = **pur local**, avec Obsidian
  mobile pour le téléphone.
