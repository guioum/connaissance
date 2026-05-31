"""Détection automatique de la racine des données utilisateur.

En Claude Code natif (macOS/Linux), les dossiers de données sont sous
`Path.home()` (ex: `~/Connaissance/`, `~/Documents/`).

En Claude cowork, la VM expose `$HOME = /sessions/<session-name>/` et
les dossiers partagés du host sont montés sous `$HOME/mnt/` via VirtioFS.
Cowork lui-même documente `SESSION=$(basename $HOME)` comme méthode
officielle pour détecter la session courante.

Ce module auto-détecte l'environnement et expose la bonne racine.

Usage :
    from connaissance.core.paths import BASE_PATH, VM_HOME

    CONNAISSANCE = BASE_PATH / "Connaissance"
    DOCUMENTS_DIR = BASE_PATH / "Documents"

    # VM_HOME reste le vrai home de la VM (pour .zshenv, .zshrc, etc.)
    zshenv = VM_HOME / ".zshenv"
"""

import sys
from pathlib import Path

# Home réel de l'utilisateur (VM en cowork, Mac en natif). Utiliser pour
# les fichiers shell (.zshenv, .zshrc) qui vivent dans le home de la VM.
VM_HOME = Path.home()


def _detect_base_path() -> Path:
    """Détecter la racine des données utilisateur.

    En cowork VM, `$HOME = /sessions/<session-name>/` et les dossiers
    partagés sont montés sous `$HOME/mnt/`. Sinon, fallback sur
    `Path.home()` (Claude Code natif).
    """
    if VM_HOME.parent == Path("/sessions"):
        mnt = VM_HOME / "mnt"
        if mnt.is_dir():
            return mnt
    return VM_HOME


# Racine des données utilisateur (Connaissance/, Documents/, Archives/, etc.).
BASE_PATH = _detect_base_path()

# Racine de la base de connaissance : prérequis strict, jamais créée par le plugin.
CONNAISSANCE_ROOT = BASE_PATH / "Connaissance"

# Racine canonique des documents source (typiquement en iCloud Drive — des
# fichiers peuvent être `dataless`, leur contenu rechargé à la lecture).
DOCUMENTS_DIR = BASE_PATH / "Documents"


def _detect_documents_cache_root() -> Path | None:
    """Racine d'un miroir LOCAL de Documents servant de cache de lecture.

    Permet de lire le contenu des documents source sans déclencher de
    téléchargement iCloud. Le miroir doit préserver la MÊME arborescence
    relative que ``DOCUMENTS_DIR`` (cas du SSD « Backup Cloud »).

    Résolution :
    1. ``CONNAISSANCE_DOCUMENTS_CACHE`` (override explicite, ex. autre disque).
    2. SSD « Backup Cloud » monté — Mac natif (`/Volumes/Backup Cloud/Documents`)
       ou cowork (`<BASE_PATH>/Backup Cloud/Documents`, monté via le sélecteur).

    Retourne ``None`` si aucun miroir n'est disponible.
    """
    import os
    override = os.environ.get("CONNAISSANCE_DOCUMENTS_CACHE")
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    for c in (Path("/Volumes/Backup Cloud/Documents"),
              BASE_PATH / "Backup Cloud" / "Documents"):
        if c.is_dir():
            return c
    return None


# Mémo process-local : l'état de montage est stable pour la durée d'un run CLI.
_CACHE_ROOT_MEMO: list[Path | None] = []


def documents_cache_root() -> Path | None:
    """Racine du miroir de lecture des documents (SSD), ou ``None``."""
    if not _CACHE_ROOT_MEMO:
        _CACHE_ROOT_MEMO.append(_detect_documents_cache_root())
    return _CACHE_ROOT_MEMO[0]


def documents_read_path(path: Path) -> Path:
    """Mapper un chemin canonique sous DOCUMENTS_DIR vers son miroir local.

    Si un miroir (SSD) est disponible et contient le fichier avec la MÊME
    taille (garde anti-miroir-périmé), retourne le chemin miroir — le lire ne
    déclenche aucun téléchargement iCloud. Sinon retourne ``path`` inchangé.

    No-op pour tout chemin hors DOCUMENTS_DIR : sûr à appliquer partout. Le
    miroir reste un cache OPPORTUNISTE — l'identité (clé de cache, frontmatter,
    DB) demeure toujours le chemin canonique ``path``.
    """
    path = Path(path)
    root = documents_cache_root()
    if root is None:
        return path
    try:
        rel = path.relative_to(DOCUMENTS_DIR)
    except ValueError:
        return path
    mirror = root / rel
    try:
        if mirror.is_file() and path.stat().st_size == mirror.stat().st_size:
            return mirror
    except OSError:
        return path
    return path


def is_dataless(path: Path) -> bool:
    """True si le fichier est un placeholder iCloud non matérialisé (macOS).

    Le lire déclencherait un téléchargement. Détection best-effort via le flag
    BSD ``UF_DATALESS`` ; sur les plateformes/FS sans ce flag, retourne False.
    ``stat()`` seul (métadonnées) ne télécharge jamais.
    """
    import stat as _stat
    flag = getattr(_stat, "UF_DATALESS", 0x40000000)
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & flag)


def is_cowork() -> bool:
    """True si on tourne dans une VM cowork (home sous /sessions/)."""
    return VM_HOME.parent == Path("/sessions")


def _app_data_root() -> Path:
    """Répertoire standard pour les données d'application utilisateur.

    - macOS : ``~/Library/Application Support/connaissance/``
    - Linux / cowork VM : ``~/.local/share/connaissance/`` (XDG_DATA_HOME)
    """
    import os
    if sys.platform == "darwin":
        return VM_HOME / "Library" / "Application Support" / "connaissance"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else VM_HOME / ".local" / "share"
    return root / "connaissance"


# Dossier de transit persistant pour les échanges entre outils MCP
# (summarize_prepare → submit_batch → wait_for_batch → summarize_register).
# /tmp/ était remis à zéro entre les sessions Claude Desktop, ce qui cassait
# la chaîne dès qu'un batch prenait plusieurs heures. Ce dossier survit aux
# redémarrages système, et suit les conventions standard par plateforme.
#
# Distinct de ~/Connaissance/.config/ qui reste couplé à la base (tracking
# DB, filtres, scoring, secrets — partent ensemble lors d'une sauvegarde).
TRANSIT_DIR = _app_data_root() / "transit"


def transit_file(kind: str) -> Path:
    """Générer un chemin unique dans le transit dir persistant.

    Le dossier est créé au besoin. Format du nom :
    ``<kind>_<timestamp>_<pid>.json`` pour éviter les collisions entre
    processus concurrents.
    """
    import os
    import time
    TRANSIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return TRANSIT_DIR / f"{kind}_{stamp}_{os.getpid()}.json"


def require_paths(*paths: Path, context: str = "") -> None:
    """Vérifier que des chemins requis (dossiers ou fichiers) existent.

    Utilisé en tête de main() pour échouer rapidement avec un message clair
    si un dossier source nécessaire n'est pas monté ou introuvable. Typique-
    ment un cas cowork VM où tous les dossiers du home Mac ne sont pas
    exposés via VirtioFS sous $HOME/mnt/.

    En cas d'absence, affiche la liste des chemins manquants, le BASE_PATH
    détecté, l'environnement (cowork/natif) et une suggestion, puis sort
    avec le code 2 (distinct du code 1 réservé aux erreurs métier).
    """
    missing = [p for p in paths if not p.exists()]
    if not missing:
        return

    label = f" ({context})" if context else ""
    print(f"✗ Chemins requis introuvables{label} :", file=sys.stderr)
    for p in missing:
        print(f"  - {p}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Environnement : {'cowork VM' if is_cowork() else 'macOS natif'}",
          file=sys.stderr)
    print(f"BASE_PATH     : {BASE_PATH}", file=sys.stderr)

    if is_cowork():
        print(file=sys.stderr)
        print("En cowork, les dossiers de ton home Mac sont exposés via", file=sys.stderr)
        print("VirtioFS sous $HOME/mnt/. Si un dossier requis n'est pas", file=sys.stderr)
        print("monté, vérifie la configuration cowork ou lance cette", file=sys.stderr)
        print("commande en Claude Code Mac natif.", file=sys.stderr)

    sys.exit(2)


def require_connaissance_root() -> None:
    """Vérifier que ~/Connaissance/ existe comme prérequis strict du plugin.

    Le plugin ne doit JAMAIS créer cette racine automatiquement : en cowork
    VM, un home sans montage VirtioFS ferait croire à tort que le dossier
    n'existe pas et produirait une base de connaissance fantôme dans le
    mauvais emplacement, invisible de ton Mac.

    Si le dossier n'existe pas, le script doit échouer tôt avec un message
    clair expliquant qu'il s'agit d'un prérequis utilisateur, pas quelque
    chose que le plugin a le droit de créer.
    """
    if not CONNAISSANCE_ROOT.exists():
        print(f"✗ Racine de la base de connaissance introuvable :", file=sys.stderr)
        print(f"  {CONNAISSANCE_ROOT}", file=sys.stderr)
        print(file=sys.stderr)
        print(f"Environnement : {'cowork VM' if is_cowork() else 'macOS natif'}",
              file=sys.stderr)
        print(f"BASE_PATH     : {BASE_PATH}", file=sys.stderr)
        print(file=sys.stderr)
        print("Cette racine est un PRÉREQUIS du plugin connaissance.", file=sys.stderr)
        print("Le plugin ne la crée jamais automatiquement pour éviter", file=sys.stderr)
        print("de masquer un problème de montage (ex: cowork VM sans", file=sys.stderr)
        print("VirtioFS qui expose ton home Mac).", file=sys.stderr)
        print(file=sys.stderr)
        if is_cowork():
            print("Vérifie que ~/Connaissance/ est monté dans ta VM cowork,", file=sys.stderr)
            print("ou lance cette commande en Claude Code Mac natif.", file=sys.stderr)
        else:
            print("Crée le dossier manuellement avec :", file=sys.stderr)
            print(f"  mkdir -p {CONNAISSANCE_ROOT}", file=sys.stderr)
        sys.exit(2)
    if not CONNAISSANCE_ROOT.is_dir():
        print(f"✗ {CONNAISSANCE_ROOT} existe mais n'est pas un dossier.", file=sys.stderr)
        sys.exit(2)
