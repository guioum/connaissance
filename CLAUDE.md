# CLAUDE.md

Notes techniques pour les futurs développeurs et pour Claude Code.

## Architecture

Ce repo reproduit le pattern de `guioum/mistral-ocr` : un package Python
installable qui fournit un binaire CLI + un wrapper MCP Node.js packagé
en MCPB. Aucune duplication de logique métier entre les deux — le MCPB
est un shell-out léger vers le CLI.

```
src/connaissance/        → package Python, CLI avec 12 groupes de commandes
  ├── cli.py             → entry point, fonction main()
  ├── core/              → paths, tracking (SQLite), filtres, résolution, schemas
  ├── commands/          → un module par groupe de sous-commandes
  ├── prompts/           → templates markdown des résumés (package_data)
  └── config/            → templates YAML de filtres et scoring (package_data)

mcpb/                    → MCPB Node.js, installé dans Claude Desktop
  ├── manifest.json      → metadata MCPB
  └── server/
      ├── index.js       → wrapper qui shell-out vers le binaire connaissance
      └── package.json   → @modelcontextprotocol/sdk + zod uniquement
```

## Contrats

- **Toute sortie CLI est JSON à stdout**. Les erreurs vont sur stderr
  avec exit code non-zéro. Le format JSON est typé via les TypedDicts
  de `core/schemas.py`.
- **Les mutations passent par un pattern plan → apply** (documents,
  organize, optimize, emails, scope, config). Le `plan` écrit un
  manifeste JSON sur disque ; `apply` le consomme. Les outils MCP
  exposent les deux.
- **Les mutations de config YAML** (`scoring-courriels.yaml`) passent
  par des atomes typés (`add_domain_marketing`, `set_weight`, etc.)
  jamais par du YAML composé par l'appelant. `ruamel.yaml` préserve
  les commentaires utilisateur lors des écritures.
- **`tracking.db`** est la propriété exclusive du CLI. Schéma SQLite
  byte-identique à la v1.9.0 du plugin cowork (aucune migration).

## Détection de la racine

`core/paths.py` détecte automatiquement l'environnement :

- **Mac natif** : `BASE_PATH = Path.home()`, `CONNAISSANCE_ROOT = ~/Connaissance`
- **VM cowork** : `BASE_PATH = Path.home() / "mnt"` si le home parent
  est `/sessions/` et que `~/mnt/` existe (VirtioFS). `CONNAISSANCE_ROOT`
  pointe alors vers `~/mnt/Connaissance`.

Le CLI refuse de démarrer si `CONNAISSANCE_ROOT` n'existe pas — il ne
le crée jamais automatiquement pour éviter de masquer un problème de
montage VirtioFS qui produirait une base fantôme.

## Développement

### Install editable

```bash
cd ~/Code/guioum/connaissance
uv tool install --force -e .
```

Utile pendant le développement — les modifications du code Python sont
immédiatement actives sans reinstall.

### Ajouter une nouvelle sous-commande

1. Ajouter une fonction publique dans le module `commands/<groupe>.py`
   qui retourne un dict typé conforme à un TypedDict de `core/schemas.py`
2. Ajouter un handler dans `cli.py` sous `_cmd_<groupe>` qui dispatch
   selon `args.verb`
3. Ajouter le sub-parser dans `build_parser()`
4. Ajouter un wrapper MCP dans `mcpb/server/index.js` via
   `server.registerTool()`
5. Mettre à jour README et le tableau des 42 outils

### Tests manuels

```bash
# CLI
connaissance pipeline detect --steps stats
connaissance audit check --steps liens_casses

# MCPB (stdio server)
cd mcpb/server
npm install
node index.js < /dev/null  # doit démarrer sans crash
```

### Packaging MCPB

```bash
cd mcpb && npx @anthropic-ai/mcpb pack . connaissance-VERSION.mcpb
```

## Dépendances externes

- [`claude-api-mcp`](https://github.com/guioum/claude-api-mcp) — pour
  les appels `mcp__claude_api__*` (Anthropic Batch API pour les résumés)
- Plugin cowork [`qmd`](../cowork-plugins/qmd) — pour la recherche
  sémantique via `mcp__qmd__query`
- Plugin cowork [`connaissance`](../cowork-plugins/connaissance) —
  shim de skills qui orchestre les workflows du pipeline en invoquant
  `mcp__connaissance__*`

## Rollback

Si une version déployée pose problème, rollback facile :

```bash
# Désinstaller et réinstaller la version précédente
uv tool uninstall connaissance
uv tool install git+https://github.com/guioum/connaissance@v2.1.0
```

Le MCPB est versionné en parallèle dans les Releases GitHub, téléchargeable
individuellement.
