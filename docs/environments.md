# Environnements & stockage

Le CLI tourne dans trois contextes, et lit des données qui peuvent être en
iCloud ou sur un SSD externe. [`core/paths.py`](../src/connaissance/core/paths.py)
gère la détection.

## Détection de la racine

```python
VM_HOME   = Path.home()          # home réel (Mac ou VM)
BASE_PATH = _detect_base_path()  # racine des données
CONNAISSANCE_ROOT = BASE_PATH / "Connaissance"
```

| Contexte | `VM_HOME` | `BASE_PATH` | Connaissance |
|---|---|---|---|
| **Mac natif** | `/Users/<user>` | `~` | `~/Connaissance` |
| **VM cowork** | `/sessions/<nom>` | `~/mnt` (VirtioFS) | `~/mnt/Connaissance` |

La VM cowork est détectée quand `VM_HOME.parent == /sessions` et que `~/mnt/`
existe. `is_cowork()` expose le test. Les fichiers shell (`.zshenv`, etc.)
restent sous `VM_HOME` ; seules les **données** passent par `BASE_PATH`.

## Pourquoi le CLI refuse de créer la racine

Si `~/Connaissance/` n'existe pas, le CLI **échoue** (`require_connaissance_root`)
au lieu de créer le dossier. Raison : en VM cowork, un home sans montage
VirtioFS ferait croire à tort que la base est vide → on créerait une base
fantôme au mauvais endroit, invisible du Mac. Mieux vaut échouer fort avec un
message clair (`require_paths` / exit code 2) que masquer un problème de montage.

## iCloud : la source `Documents` est rechargée à la demande

La source documentaire (`~/Documents`) est sur **iCloud Drive**. Avec
« Optimiser le stockage du Mac », des fichiers sont **évincés** (dataless) :
présents en métadonnées, contenu rechargé seulement à la lecture.

- Détecter sans déclencher de téléchargement : `find ~/Documents -flags dataless`
  ou `stat -f %Sf` ne lisent que les métadonnées.
- **Lire** un fichier dataless déclenche son téléchargement. Donc toute passe
  qui doit lire le **contenu** de beaucoup de documents (OCR, perceptual hash)
  ne doit pas balayer iCloud en masse.

## Le SSD « Backup Cloud » : un miroir local exploitable

Le SSD externe `/Volumes/Backup Cloud` (APFS) contient un **miroir exact et
matérialisé** de `~/Documents` (mêmes chemins, tailles, mtimes ; ~48 Go). C'est
une source de lecture **insensible à l'éviction iCloud** : idéale pour les
passes lourdes (lire le miroir plutôt que de matérialiser tout iCloud).

Statut : la détection du miroir n'est **pas encore câblée** dans `paths.py`
(prévu : un `documents_read_root()` env-aware qui préfère le SSD s'il est monté,
sinon iCloud). Voir [roadmap.md](roadmap.md).

## Accès au SSD depuis cowork

La VM cowork ne voit que des `bindfs` montés par l'hôte sous `~/mnt/`. Un
volume externe `/Volumes/...` n'est **pas** visible par défaut. Vérifié cette
session :

- Le **sélecteur de dossier cowork** (`request_directory`) accepte un chemin
  **hors du home, y compris sous `/Volumes`** : l'hôte crée alors un bind-mount
  visible en `~/mnt/Backup Cloud`.
- La VM ne peut **jamais** s'auto-monter quoi que ce soit — ça passe toujours
  par l'hôte.

Reco : faire tourner les passes documentaires lourdes en **natif** (APFS
direct, débit max) plutôt que via `bindfs → VirtioFS`. Détails et arbitrage
dans la note mémoire `ssd-backup-cloud-et-acces-cowork`.

## Le dossier de transit

Les échanges multi-étapes entre outils MCP (ex. `summarize_prepare` →
batch → `summarize_register`) passent par un **transit dir persistant**
(`TRANSIT_DIR`), pas par `/tmp` (remis à zéro entre sessions Claude Desktop, ce
qui cassait les batchs de plusieurs heures) :

- macOS : `~/Library/Application Support/connaissance/transit/`
- Linux / VM : `$XDG_DATA_HOME/connaissance/transit/` ou `~/.local/share/...`

Distinct de `~/Connaissance/.config/` (couplé à la base : DB, scoring, secrets
— part avec une sauvegarde).
