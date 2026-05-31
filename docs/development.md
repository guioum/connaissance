# Développement

## Install editable

```bash
cd ~/Code/guioum/connaissance
uv tool install --force -e .
```

Les modifications du code Python sont actives immédiatement, sans reinstall.

## Ajouter une sous-commande

Le flux complet (du Python à l'outil MCP) :

1. **Logique** — fonction publique dans `commands/<groupe>.py` retournant un
   `dict` conforme à un TypedDict de [`core/schemas.py`](../src/connaissance/core/schemas.py).
   Si la forme de sortie est nouvelle, ajouter le TypedDict d'abord.
2. **Dispatch** — handler dans [`cli.py`](../src/connaissance/cli.py) sous
   `_cmd_<groupe>` qui dispatche selon `args.verb`.
3. **Parser** — sub-parser dans `build_parser()`.
4. **MCP** — wrapper dans [`mcpb/server/index.js`](../mcpb/server/index.js) via
   `server.registerTool()` ; construire les flags avec `pushFlag`, marquer
   `readOnlyHint: true` si la commande ne mute rien.
5. **Doc** — mettre à jour le README (tableau des outils + décompte) et la doc
   concernée sous `docs/`.

Si la commande **mute** la base : respecter le pattern plan → apply (un verbe
`plan`/`prepare` qui écrit un manifeste, un verbe `apply` qui le consomme avec
`--dry-run`). Voir [architecture.md](architecture.md).

## Tests manuels

```bash
# CLI — toute sortie est du JSON
connaissance pipeline detect --steps stats
connaissance audit check --steps liens_casses

# MCPB — doit démarrer sans crash
cd mcpb/server && npm install && node index.js < /dev/null
```

> Il n'existe pas encore de suite `pytest` automatisée. Les composants `core/`
> purs et déterministes (`dedup`, `filtres` scoring, le cache de `tracking`)
> sont les premiers candidats à des tests unitaires — voir [roadmap.md](roadmap.md).

## Scripts de calibration jetables

Le dossier `scratch/` (gitignoré) accueille les scripts d'exploration ponctuels
— par ex. la calibration qui a tranché le choix SimHash texte vs perceptual hash
image pour les quasi-doublons (`calibrate_simhash.py`, `calibrate_phash.py`).
Ils ne font partie d'aucun package et peuvent utiliser des dépendances
éphémères :

```bash
uv run --with imagehash --with pypdfium2 --with pillow \
    python scratch/calibrate_phash.py --sample 50
```

## Packaging du MCPB

```bash
cd mcpb/server && npm install
cd .. && npx @anthropic-ai/mcpb pack . connaissance-<VERSION>.mcpb
```

Versionner en parallèle : la version vit dans `pyproject.toml` **et**
`mcpb/manifest.json` (à garder synchrones ; `mcpb/server/package.json` a
historiquement dérivé — cf. [roadmap.md](roadmap.md)). Convention de commit :
`vX.Y.Z — <résumé>`, bump mineur pour une feature, patch pour un correctif.

## Rollback

```bash
uv tool uninstall connaissance
uv tool install git+https://github.com/guioum/connaissance@vX.Y.Z
```

Le MCPB est versionné en parallèle dans les Releases GitHub, téléchargeable
individuellement.

## Dépendances externes

- [`claude-api-mcp`](https://github.com/guioum/claude-api-mcp) — appels Claude
  (Batch API pour les résumés/synthèses).
- Plugin cowork [`qmd`](https://github.com/guioum/cowork-plugins) — recherche
  sémantique (`mcp__qmd__query`), enrichissement du classement.
- Plugin cowork `connaissance` — shim de skills qui orchestre les workflows en
  invoquant les outils `mcp__connaissance__*`.
- Plugin `ocr` / `mistral-ocr` — OCR des documents.
