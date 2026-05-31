# Architecture

## Deux couches, zéro duplication

```
┌─────────────────────────────────────────────────────────┐
│  Claude Desktop / cowork                                 │
│     └── mcpb/server/index.js  (48 outils MCP)            │
│            │  shell-out + parse JSON                      │
│            ▼                                              │
│  binaire `connaissance`  (entry point cli.py)            │
│     ├── commands/   un module par groupe de commandes    │
│     └── core/       paths, tracking, filtres, schemas…   │
│            │                                              │
│            ▼                                              │
│  ~/Connaissance/    Markdown + tracking.db (SQLite)      │
└─────────────────────────────────────────────────────────┘
```

Le repo reproduit le pattern de [`guioum/mistral-ocr`](https://github.com/guioum/mistral-ocr) :
un package Python installable fournit un binaire CLI ; un wrapper MCP Node.js
packagé en `.mcpb` shell-out vers ce binaire. **Aucune logique métier n'est
dupliquée** : chaque outil MCP mappe une sous-commande CLI 1:1, lui passe des
flags, et remonte le JSON tel quel.

Pourquoi ce découpage : le CLI est testable, scriptable et utilisable seul ; le
MCPB reste un shell léger (dépendances : `@modelcontextprotocol/sdk` + `zod`).
Mettre à jour la logique = toucher le Python uniquement.

## Le CLI

Grammaire : `connaissance <groupe> <verbe> [--flags]`.

- **13 groupes de commandes** : `pipeline`, `documents`, `emails`, `notes`,
  `organize`, `optimize`, `summarize`, `synthesis`, `audit`, `actions`,
  `scope`, `config`, `manifest`.
- Point d'entrée [`cli.py`](../src/connaissance/cli.py) : `build_parser()`
  déclare les sous-parsers, `main()` dispatche vers `_cmd_<groupe>`.
- Chaque `_cmd_<groupe>` appelle une fonction publique du module
  `commands/<groupe>.py` qui retourne un `dict` typé.

## Les contrats

Trois invariants régissent tout le CLI :

### 1. Toute sortie est du JSON sur stdout

Les commandes impriment un JSON conforme à un TypedDict de
[`core/schemas.py`](../src/connaissance/core/schemas.py). Les erreurs vont sur
**stderr** avec un exit code non-zéro, encapsulées dans `ErrorEnvelope`
(`{"error": {"type", "message"}}`). Le flag `--human` bascule en sortie
terminal lisible pour le debug, mais le défaut machine est JSON.

### 2. Le pattern plan → apply

Toute mutation de la base passe par deux temps :

1. `plan` (ou `prepare`) — calcule l'effet et l'écrit comme **manifeste JSON**
   sur disque. Rien n'est modifié. Le manifeste est relisible et **patchable**
   (voir le groupe `manifest patch`).
2. `apply` — consomme le manifeste et exécute. Supporte `--dry-run`.

Concerné : `documents`, `organize`, `optimize`, `summarize`, `synthesis`,
`emails`, `scope`, `config`. Les outils MCP exposent les deux temps, ce qui
permet à Claude de présenter le plan à l'utilisateur avant d'appliquer.

### 3. Les mutations de config passent par des atomes typés

Le scoring courriels ([`config/scoring-courriels.yaml`](../src/connaissance/config/scoring-courriels.yaml))
n'est jamais réécrit avec du YAML composé par l'appelant. On passe par des
**atomes** (`config scoring-set --add-domain-marketing …`, `--set-weight …`).
`ruamel.yaml` préserve les commentaires utilisateur lors de l'écriture. Voir
[emails.md](emails.md).

## Les modules `core/`

| Module | Rôle |
|---|---|
| [`paths.py`](../src/connaissance/core/paths.py) | Détection de la racine (Mac natif / VM cowork). Voir [environments.md](environments.md). |
| [`tracking.py`](../src/connaissance/core/tracking.py) | `TrackingDB` : interface SQLite, cache JIT des hashes/SimHash, usage LLM. Voir [data-model.md](data-model.md). |
| [`schemas.py`](../src/connaissance/core/schemas.py) | ~60 TypedDict — les contrats de sortie JSON. |
| [`filtres.py`](../src/connaissance/core/filtres.py) | Scoring des courriels, chargement des filtres YAML. Voir [emails.md](emails.md). |
| [`resolution.py`](../src/connaissance/core/resolution.py) | Résolution d'une source vers son entité (personne/organisme/sujet). |
| [`dedup.py`](../src/connaissance/core/dedup.py) | SimHash texte + clustering pour les quasi-doublons. Voir [pipeline.md](pipeline.md). |
| [`model_selection.py`](../src/connaissance/core/model_selection.py) | Choix du modèle Claude (quality/economy) par type de tâche. |
| [`output_file.py`](../src/connaissance/core/output_file.py) | Écriture atomique des fichiers de sortie. |

## Le MCPB

[`mcpb/server/index.js`](../mcpb/server/index.js) enregistre chaque outil via
`server.registerTool()`. Chaque handler construit un tableau d'arguments
(`pushFlag`), shell-out vers le binaire (trouvé via la variable d'env
`CONNAISSANCE_CLI` ou auto-détecté dans `~/.local/bin/`), et renvoie le JSON.
Le manifeste [`mcpb/manifest.json`](../mcpb/manifest.json) porte la version et
la liste — **source de vérité du décompte d'outils** (48).

Voir [development.md](development.md) pour ajouter un outil et packager.
