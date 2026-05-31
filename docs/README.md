# Documentation — connaissance

Documentation de conception de la base de connaissances personnelle
`connaissance` (CLI Python + serveur MCP). Le [README racine](../README.md)
couvre l'**installation et l'usage** ; cette doc couvre le **comment ça
marche et pourquoi**.

## Par où commencer

| Si vous voulez… | Lisez |
|---|---|
| Comprendre le découpage CLI / MCPB et les contrats | [architecture.md](architecture.md) |
| Savoir où vivent les données et comment elles sont structurées | [data-model.md](data-model.md) |
| Suivre le parcours d'un document/courriel/note de bout en bout | [pipeline.md](pipeline.md) |
| Comprendre le filtrage et le scoring des courriels | [emails.md](emails.md) |
| Faire tourner ça en natif Mac, en VM cowork, ou via le SSD | [environments.md](environments.md) |
| Contribuer : ajouter une commande, packager, tester | [development.md](development.md) |
| Voir ce qu'il reste à faire | [roadmap.md](roadmap.md) |

## Vue d'ensemble en un paragraphe

`connaissance` transforme des sources brutes (documents scannés, courriels,
notes Apple) en une base Markdown structurée et interliée. Le parcours est un
**pipeline en 5 étapes** — transcrire → résumer → organiser → optimiser →
synthétiser — où chaque étape est une commande CLI déterministe à sortie JSON.
Les étapes coûteuses (OCR, résumés, synthèses) délèguent à des services
externes (`mistral-ocr`, `claude-api-mcp`) ; le cœur reste du Python stdlib +
SQLite. Un serveur MCP Node.js (`mcpb/`) expose chaque sous-commande comme outil
pour Claude Desktop / cowork, sans dupliquer la moindre logique métier.

## Concepts clés (glossaire express)

- **Le triplet** — chaque source produit jusqu'à trois fichiers miroirs :
  une **transcription** (texte brut OCR/extrait), un **résumé** (synthèse IA
  d'un document), et sa contribution à une **synthèse** d'entité (fiche /
  chronologie). Voir [data-model.md](data-model.md).
- **Entité** — une personne, un organisme ou un sujet/projet. Les résumés sont
  rangés par entité ; les fiches agrègent tout ce qui concerne une entité.
- **plan → apply** — toute mutation s'écrit d'abord comme manifeste JSON
  (`plan`), relu et patchable, puis appliqué (`apply`). Voir
  [architecture.md](architecture.md).
- **JIT** — le pipeline ne calcule (hash, scan) qu'à la demande ; en base
  stable, zéro recalcul. Voir [pipeline.md](pipeline.md).
- **`tracking.db`** — SQLite, propriété exclusive du CLI, mémorise ce qui a
  été traité. Voir [data-model.md](data-model.md).

## Source de vérité

Quand cette doc et le code divergent, **le code gagne**. Les contrats de sortie
sont les TypedDict de [`core/schemas.py`](../src/connaissance/core/schemas.py) ;
la liste des outils MCP est [`mcpb/manifest.json`](../mcpb/manifest.json) et
[`mcpb/server/index.js`](../mcpb/server/index.js). Cette doc est mise à jour à
la main — signaler toute dérive dans [roadmap.md](roadmap.md).
