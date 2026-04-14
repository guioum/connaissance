# connaissance

CLI + MCP pour une base de connaissances personnelle en Markdown. Pipeline
complet en 5 étapes : **transcrire → résumer → organiser → optimiser → synthétiser**.

Successeur du serveur bundlé dans [`guioum/cowork-plugins/connaissance`](https://github.com/guioum/cowork-plugins/tree/main/connaissance) v2.0.0. À partir de v2.1.0 le
CLI et le serveur MCP vivent dans ce repo indépendant, le plugin cowork
devient un simple shim de skills qui consomme les outils MCP exposés ici.

## Features

- **Transcrire** — OCR documents (via plugin externe `ocr`), extraction
  courriels mbox avec scoring multi-signaux, copie incrémentale de notes
  Apple, détection + correction de transcriptions OCR suspectes
- **Résumer** — Prépare des requêtes prêtes pour `claude-api-mcp` à partir
  de templates dans `src/connaissance/prompts/`, post-traite les réponses,
  écrit les résumés au chemin miroir et les enregistre dans `tracking.db`
- **Organiser** — Classification déterministe par entité avec
  enrichissement qmd pour les cas à confirmer, manifestes JSON patchables
- **Optimiser** — Promotion de PJ de documents, déduplication SHA256
- **Synthétiser** — Candidats d'alias et de relations via scan
  déterministe, calibrage typé du scoring, validation des expéditeurs,
  nettoyage rétroactif, recherche sémantique qmd
- **Audit** — 6 vérifications d'intégrité déterministes, reindex de la
  base, réparation d'attachements, archivage de non-documents

## Quick start

### 1. Install the CLI

```bash
# With uv (recommended)
uv tool install git+https://github.com/guioum/connaissance

# Or with pip
pip install --user git+https://github.com/guioum/connaissance
```

This installs the `connaissance` command globally.

For local development:

```bash
git clone https://github.com/guioum/connaissance.git
cd connaissance
uv tool install --force -e .
```

### 2. Vérifier l'installation

```bash
connaissance --help
connaissance pipeline detect --steps stats
```

La base de connaissance est attendue sous `~/Connaissance/` (racine
détectée automatiquement, avec support du montage VirtioFS
`~/mnt/Connaissance/` pour les VMs cowork).

### 3. Install the MCPB in Claude Desktop

```bash
# Build the MCPB
cd mcpb/server && npm install
cd .. && npx @anthropic-ai/mcpb pack . connaissance-2.1.0.mcpb

# Install via drag-drop on Claude Desktop
# Or via setup script:
cd ..
./setup-claude-desktop.sh
```

Redémarrer Claude Desktop pour activer les 42 outils
`mcp__connaissance__*`.

## Usage

### CLI

Grammaire : `connaissance <groupe> <verbe> [--flags]`. Sortie JSON par
défaut, `--human` pour debug terminal.

```bash
# Pipeline
connaissance pipeline detect
connaissance pipeline costs --mode batch

# Documents
connaissance documents scan --since 2026-03-01
connaissance documents suspects

# Emails
connaissance emails extract --since 2026-03-01 --dry-run
connaissance emails threads
connaissance emails calibrate --sample 200

# Organize
connaissance organize plan
connaissance organize apply manifest.json --dry-run

# Summarize (prepare requests for claude-api-mcp)
connaissance summarize plan
connaissance summarize prepare --ids "Transcriptions/..." --mode direct

# Audit
connaissance audit check --steps all

# Config (scoring mutations via typed atoms)
connaissance config scoring-show
connaissance config scoring-set --add-domain-marketing exemple.fr --dry-run
```

### MCP tools (via Claude Desktop / cowork)

42 outils mappés 1:1 vers les sous-commandes CLI :

| Group | Tools |
|---|---|
| pipeline | `detect`, `costs`, `simulate` |
| documents | `scan`, `register`, `register_existing`, `suspects`, `verify_preserve` |
| emails | `stats`, `extract`, `threads`, `calibrate`, `senders`, `cleanup_obsolete` |
| notes | `scan`, `copy` |
| organize | `plan`, `enrich`, `apply`, `resolve` |
| optimize | `plan`, `apply` |
| summarize | `plan`, `prepare`, `register` |
| synthesis | `plan`, `aliases_candidates`, `relations_candidates`, `register` |
| audit | `check`, `reindex_db`, `repair_attachments`, `archive_non_documents` |
| scope | `scan`, `check`, `include`, `exclude` |
| config | `scoring_show`, `scoring_set`, `scoring_diff`, `scoring_validate` |
| manifest | `patch` |

Total : **42 outils**.

## Architecture

Ce repo contient deux choses :

- **`src/connaissance/`** — package Python installable via `uv tool` ou
  `pip`. Expose le binaire `connaissance` comme entry point.
- **`mcpb/server/index.js`** — wrapper MCP Node.js packagé en `.mcpb`
  pour Claude Desktop. Chaque outil MCP shell-out vers le binaire
  `connaissance` (trouvé via `CONNAISSANCE_CLI` env ou auto-détection
  dans `~/.local/bin/`) et parse la sortie JSON.

Les 42 outils MCP ne contiennent aucune logique métier : ils mappent
les sous-commandes CLI 1:1 et remontent le JSON tel quel.

## Prérequis

- Python ≥ 3.10 (pour le CLI)
- Node.js ≥ 18 (pour le MCPB)
- `mcp__claude_api__*` (via [`claude-api-mcp`](https://github.com/guioum/claude-api-mcp)) pour les appels API Claude
- `mcp__qmd__*` (plugin cowork) pour la recherche sémantique

## Stack

- **CLI** : Python 3.10+, `pyyaml`, `ruamel.yaml` (préserve les commentaires
  lors des mutations de `scoring-courriels.yaml`), stdlib uniquement sinon
  (sqlite3, mailbox, email, argparse, hashlib, etc.)
- **MCPB** : Node.js 18+, `@modelcontextprotocol/sdk`, `zod`, packagé
  via `@anthropic-ai/mcpb pack`

## License

MIT
