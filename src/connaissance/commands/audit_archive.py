#!/usr/bin/env python3
"""Archiver les non-documents détectés par le scanner de périmètre.

Déplace les dossiers exclus (code, photos, bundles, téléchargements) vers
`~/Documents/- Archives/{Code,Photos,Applications,Téléchargements}/` et
nettoie les dossiers parents devenus vides. Met à jour `filtres.yaml`
automatiquement : les chemins exclus qui ont été déplacés sont retirés.

API publique : `archive(dry_run, category)`.
"""
import json
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

from connaissance.core import ledger as _ledger
from connaissance.core.fsio import atomic_write_text
from connaissance.core.paths import BASE_PATH, require_paths, require_connaissance_root
from connaissance.core.schemas import AuditArchiveNonDocuments
from connaissance.core.tracking import TrackingDB

# ── Chemins ──────────────────────────────────────────────────────────────────

HOME = BASE_PATH
DOCUMENTS_LOCAL = HOME / "Documents"

ARCHIVES_DIR = DOCUMENTS_LOCAL / "- Archives"

CONFIG_DIR = HOME / "Connaissance" / ".config"
PERIMETRE_CONFIG = CONFIG_DIR / "filtres.yaml"
PERIMETRE_RAPPORT = CONFIG_DIR / "perimetre-rapport.json"

# ── Mapping catégorie → sous-dossier d'archives ─────────────────────────────

CATEGORY_DEST = {
    "bundle_app": "Applications",
    "code_repo": "Code",
    "photos_perso": "Photos",
}

# Dossiers protégés : convention, les noms commençant par "- " sont des
# dossiers de workflow ou d'organisation (- Inbox 📥, - Review 🔁, - Archives, etc.)
# ainsi que les dossiers de classement par entité.
PROTECTED_PREFIXES = ("- ",)
PROTECTED_DIRS = {"organismes", "personnes"}


# ── Fonctions utilitaires ───────────────────────────────────────────────────

def nfc(s):
    """Normaliser une chaîne en NFC (compatibilité macOS NFD)."""
    return unicodedata.normalize("NFC", s)


def load_config():
    """Charger la config de périmètre."""
    if PERIMETRE_CONFIG.exists():
        with open(PERIMETRE_CONFIG) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_config(config):
    """Sauvegarder la config de périmètre."""
    require_connaissance_root()
    CONFIG_DIR.mkdir(parents=False, exist_ok=True)
    atomic_write_text(
        PERIMETRE_CONFIG,
        yaml.dump(config, default_flow_style=False, allow_unicode=True,
                  sort_keys=False))


def load_rapport():
    """Charger le rapport de scan. Retourne None si absent."""
    if not PERIMETRE_RAPPORT.exists():
        return None
    with open(PERIMETRE_RAPPORT) as f:
        return json.load(f)


def resolve_source_path(rel_path):
    """Résoudre le chemin source dans ~/Documents/ uniquement.

    Retourne le chemin existant dans ~/Documents/, ou None si introuvable.
    """
    p = DOCUMENTS_LOCAL / rel_path
    if p.exists():
        return p
    return None


def compute_dest(rel_path, category):
    """Calculer le chemin de destination dans - Archives/.

    Retourne (dest_path, archive_subdir).
    """
    subdir = CATEGORY_DEST.get(category, "Divers")
    name = Path(rel_path).name
    dest = ARCHIVES_DIR / subdir / name
    return dest, subdir


def cleanup_empty_parents(path, stop_at):
    """Remonter et supprimer les dossiers vides jusqu'à stop_at (exclus).

    Ne supprime jamais les dossiers protégés.
    """
    removed = []
    current = path.parent
    while current != stop_at and current != current.parent:
        if current.name in PROTECTED_DIRS or any(
                current.name.startswith(p) for p in PROTECTED_PREFIXES):
            break
        try:
            if current.exists() and not any(current.iterdir()):
                current.rmdir()
                removed.append(str(current))
            else:
                break  # dossier non vide, arrêter
        except OSError:
            break
        current = current.parent
    return removed


# ── Collecte des dossiers à archiver ────────────────────────────────────────

def collect_moves(config, rapport, category_filter=None):
    """Collecter les déplacements à effectuer.

    Utilise les dossiers_exclus de la config (pas les patterns — les bundles
    matchés par *.app sont déjà exclus par pattern, on les détecte via le rapport).

    Retourne une liste de dicts : {rel_path, source, dest, category, subdir, reason}
    """
    moves = []
    seen_paths = set()

    # Chemins explicitement inclus (ne jamais archiver)
    inclus_nfc = {nfc(i) for i in config.get("dossiers_inclus", [])}

    # 1. Depuis les dossiers_exclus de la config
    for exc in config.get("dossiers_exclus", []):
        exc_nfc = nfc(exc)
        if exc_nfc in seen_paths or exc_nfc in inclus_nfc:
            continue

        source = resolve_source_path(exc)
        if source is None:
            continue

        # Trouver la catégorie dans le rapport, sinon deviner
        category = _find_category(exc_nfc, rapport)
        if category is None:
            category = _guess_category(source)
        # Dernier recours : deviner par le commentaire YAML (code vs photos)
        if category is None:
            category = _guess_from_config_context(exc, config)

        if category_filter and category != category_filter:
            continue

        if category not in CATEGORY_DEST:
            continue

        dest, subdir = compute_dest(exc, category)
        moves.append({
            "rel_path": exc,
            "source": str(source),
            "dest": str(dest),
            "category": category,
            "subdir": subdir,
            "reason": "exclu dans filtres.yaml",
        })
        seen_paths.add(exc_nfc)

    return moves


def _find_category(rel_path_nfc, rapport):
    """Trouver la catégorie d'un chemin dans le rapport."""
    for cat, data in rapport.get("by_category", {}).items():
        for item in (data.get("items") or []):
            if nfc(item["rel_path"]) == rel_path_nfc:
                return cat
    # Chercher dans le summary complet n'aide pas, essayer les heuristiques
    return None


def _guess_category(path):
    """Deviner la catégorie d'un dossier par son contenu."""
    name = path.name.lower()
    # Bundles
    for ext in (".app", ".framework", ".appex", ".kext", ".bundle", ".plugin"):
        if name.endswith(ext):
            return "bundle_app"
    # Code markers
    code_markers = {".git", "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
                    "Makefile", "CMakeLists.txt", "pom.xml", "composer.json", "setup.py"}
    code_extensions = {".py", ".js", ".ts", ".php", ".java", ".c", ".cpp", ".go",
                       ".rb", ".swift", ".rs", ".h", ".css", ".html"}
    try:
        entries = {e.name for e in path.iterdir()}
        if entries & code_markers:
            return "code_repo"
        # Forte densité de code
        exts = [Path(e).suffix.lower() for e in entries]
        code_count = sum(1 for e in exts if e in code_extensions)
        if len(exts) > 3 and code_count / len(exts) > 0.5:
            return "code_repo"
    except OSError:
        pass
    return None


def _guess_from_config_context(rel_path, config):
    """Deviner la catégorie par la position dans la liste dossiers_exclus.

    La config YAML a des commentaires de section (# Code, # Photos).
    On utilise l'index dans la liste pour approximer.
    """
    exclus = config.get("dossiers_exclus", [])
    try:
        idx = exclus.index(rel_path)
    except ValueError:
        return None

    # Lire le fichier YAML brut pour trouver les commentaires de section
    try:
        with open(PERIMETRE_CONFIG) as f:
            lines = f.readlines()
        # Trouver la ligne de l'entrée et remonter au commentaire le plus proche
        entry_count = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("- ") and "dossiers_exclus" not in stripped:
                entry_count += 1
                if entry_count == idx:
                    # Remonter pour trouver un commentaire
                    for j in range(i - 1, max(i - 5, 0), -1):
                        comment = lines[j].strip().lower()
                        if comment.startswith("#"):
                            if "code" in comment:
                                return "code_repo"
                            if "photo" in comment:
                                return "photos_perso"
                            if "app" in comment or "bundle" in comment:
                                return "bundle_app"
                    break
    except OSError:
        pass
    return None


# ── Exécution ───────────────────────────────────────────────────────────────

def execute_moves(moves, dry_run=False):
    """Exécuter les déplacements.

    Retourne (moved, skipped, errors, cleaned_dirs).
    """
    moved = []
    skipped = []
    errors = []
    cleaned_dirs = []
    # Un run ledger pour le lot : chaque déplacement est journalisé et réversible.
    db = TrackingDB()
    run_id = _ledger.new_run_id("archive")

    for m in moves:
        source = Path(m["source"])
        dest = Path(m["dest"])

        # Anti-collision : suffixe numérique si la destination existe
        if dest.exists():
            base = dest
            i = 2
            while dest.exists():
                dest = base.parent / f"{base.name} ({i})"
                i += 1
            m["dest"] = str(dest)

        if dry_run:
            moved.append(m)
            continue

        try:
            # safe_move : journalise (ancien/nouveau chemin + hash) puis déplace.
            _ledger.safe_move(db, source, dest,
                              f"archive non-document ({m.get('category', '')})",
                              run_id)
            moved.append(m)

            # Tracking (journal d'opérations, en plus du ledger)
            try:
                db.log("connaissance", "archive",
                       source_type="document",
                       source_path=str(source),
                       dest_path=str(dest),
                       details={"category": m.get("category", "")})
            except Exception:
                pass

            # Nettoyer les parents vides
            stop_at = DOCUMENTS_LOCAL

            removed = cleanup_empty_parents(source, stop_at)
            cleaned_dirs.extend(removed)

        except (OSError, shutil.Error) as e:
            m["error"] = str(e)
            errors.append(m)

    return moved, skipped, errors, cleaned_dirs, run_id


def update_config_after_moves(config, moved):
    """Retirer les dossiers déplacés de dossiers_exclus."""
    moved_nfc = {nfc(m["rel_path"]) for m in moved}
    old_exclus = config.get("dossiers_exclus", [])
    new_exclus = [e for e in old_exclus if nfc(e) not in moved_nfc]

    removed_count = len(old_exclus) - len(new_exclus)
    if removed_count > 0:
        config["dossiers_exclus"] = new_exclus

        # Ajouter à l'historique
        hist = config.get("historique", [])
        hist.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "resume": f"Archivage : {len(moved)} dossiers déplacés vers - Archives/, "
                      f"{removed_count} exclusions retirées",
        })
        config["historique"] = hist

    return config, removed_count


# --- API publique ---


def archive(dry_run: bool = True,
            category: str | None = None) -> AuditArchiveNonDocuments:
    """Archiver les non-documents (schema AuditArchiveNonDocuments).

    Appel sans confirmation — le caller (skill, utilisateur) valide avant
    d'appeler avec dry_run=False.
    """
    require_paths(DOCUMENTS_LOCAL, context="archive non-documents")
    config = load_config()
    rapport = load_rapport()
    if rapport is None:
        return {
            "archived": 0,
            "list": [],
            "error": "rapport de périmètre introuvable — lancer scope scan d'abord",
            "dry_run": dry_run,
        }

    moves = collect_moves(config, rapport, category_filter=category)
    if not moves:
        return {"archived": 0, "list": [], "dry_run": dry_run}

    if dry_run:
        return {
            "archived": 0,
            "list": [{"source": str(m.get("source")), "dest": str(m.get("dest"))}
                     for m in moves],
            "dry_run": True,
        }

    moved, _skipped, errors, _cleaned_dirs, run_id = execute_moves(moves)
    if moved:
        config, removed_count = update_config_after_moves(config, moved)
        if removed_count > 0:
            save_config(config)

    result: AuditArchiveNonDocuments = {
        "archived": len(moved),
        "list": [{"source": str(m.get("source")), "dest": str(m.get("dest"))}
                 for m in moved],
        "errors": errors,
        "dry_run": False,
    }
    if moved:
        result["ledger_run"] = run_id   # pour `ledger revert`
    return result


# ── Manifeste de tri explicite (décisions utilisateur) ──────────────────────

def _expand_unit(src: Path):
    """Fichiers d'une unité du manifeste (un fichier, ou tout un dossier)."""
    if src.is_file():
        return [src]
    if src.is_dir():
        return sorted(p for p in src.rglob("*") if p.is_file() and p.name != ".DS_Store")
    return []


def apply_manifest(manifest_path: str, dry_run: bool = True,
                   archives_root: str | None = None,
                   db: TrackingDB | None = None) -> AuditArchiveNonDocuments:
    """Appliquer un manifeste de tri **explicite** (décisions utilisateur).

    Format : ``{"archives_root": "...", "entries": [{"action", "source", "dest"}]}``
    — ``source`` relatif à ``~/Documents``, ``dest`` relatif à ``archives_root``.
    ``ARCHIVER`` → ``safe_move`` (ledger, réversible) ; ``POUBELLE`` → corbeille
    ledger (``safe_trash``) ; toute autre action est ignorée. Une unité de
    manifeste peut être un fichier ou un dossier entier (déplacé fichier par
    fichier, structure relative préservée — le ledger reste au grain fichier).

    Un run ledger **par famille** (1ᵉʳ niveau de destination, ou ``corbeille``)
    pour que ``ledger revert`` puisse annuler une famille sans les autres.
    Écrit ``<archives_root>/_index.md`` (provenance : quoi, d'où, quand, run).
    Élague ensuite les dossiers devenus vides sous ``Classer`` (jamais les
    dossiers protégés). Refuse d'écraser : une destination existante est une
    erreur remontée, pas un écrasement.
    """
    require_paths(DOCUMENTS_LOCAL, context="archive --from-manifest")
    path = Path(manifest_path).expanduser()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"archived": 0, "list": [], "dry_run": dry_run,
                "error": f"manifeste illisible : {exc}"}
    root = Path(archives_root or manifest.get("archives_root")
                or (BASE_PATH / "Archives")).expanduser()

    owns_db = db is None
    if db is None:
        db = TrackingDB()
    stamp = datetime.now().strftime("%Y-%m-%d")
    families: dict[str, dict] = {}
    errors: list[dict] = []
    touched_dirs: set[Path] = set()
    archived = trashed = 0
    try:
        for e in manifest.get("entries") or []:
            action = e.get("action")
            if action not in ("ARCHIVER", "POUBELLE"):
                continue
            src = DOCUMENTS_LOCAL / e["source"]
            files = _expand_unit(src)
            if not files:
                errors.append({"source": e["source"], "error": "source introuvable"})
                continue
            fam = (e["dest"].split("/")[0] if action == "ARCHIVER" else "corbeille")
            f = families.setdefault(fam, {
                "run_id": _ledger.new_run_id("classer-tri"), "files": 0,
                "bytes": 0, "units": []})
            f["units"].append({"source": e["source"], "dest": e.get("dest"),
                               "files": len(files)})
            for fp in files:
                try:
                    size = fp.stat().st_size
                except OSError:
                    size = 0
                if action == "ARCHIVER":
                    rel_in_unit = fp.relative_to(src) if src.is_dir() else None
                    dest = root / e["dest"]
                    if rel_in_unit is not None:
                        dest = dest / rel_in_unit
                    if dest.exists():
                        errors.append({"source": str(fp.relative_to(DOCUMENTS_LOCAL)),
                                       "error": f"destination existe : {dest}"})
                        continue
                    if not dry_run:
                        _ledger.safe_move(db, fp, dest, f"classer-tri archiver → {fam}",
                                          f["run_id"], commit=False)
                    archived += 1
                else:
                    if not dry_run:
                        _ledger.safe_trash(db, fp, "classer-tri poubelle",
                                           f["run_id"], commit=False)
                    trashed += 1
                f["files"] += 1
                f["bytes"] += size
                touched_dirs.add(fp.parent)
            if not dry_run:
                db.commit()

        index_path = root / "_index.md"
        if not dry_run and families:
            root.mkdir(parents=True, exist_ok=True)
            lines = [f"\n## Tri de Classer — {stamp}\n",
                     "| famille | unité source (~/Documents) | destination | fichiers | run ledger |",
                     "|---|---|---|---|---|"]
            esc = lambda s: str(s).replace("|", "\\|")
            for fam, f in families.items():
                # Unités « dossier » : une ligne chacune. Unités « fichier »
                # (vrac déplacé fichier par fichier) : agrégées par
                # (source niveau 3, destination niveau 2) — sinon l'index
                # compte des milliers de lignes (constaté : 20 000).
                agg: dict[tuple, int] = {}
                for u in f["units"]:
                    if u["files"] > 1 or not (DOCUMENTS_LOCAL / u["source"]).suffix:
                        dest = f"{root}/{u['dest']}" if u["dest"] else "corbeille ledger"
                        lines.append(f"| {fam} | `{esc(u['source'])}` | `{esc(dest)}` | {u['files']} | `{f['run_id']}` |")
                    else:
                        src_top = "/".join(u["source"].split("/")[:3]) + "/…"
                        dest_top = (f"{root}/" + "/".join(u["dest"].split("/")[:2]) + "/…"
                                    if u["dest"] else "corbeille ledger")
                        agg[(src_top, dest_top)] = agg.get((src_top, dest_top), 0) + 1
                for (s, d), n in agg.items():
                    lines.append(f"| {fam} | `{esc(s)}` | `{esc(d)}` | {n} | `{f['run_id']}` |")
            with open(index_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            # Élagage des dossiers vidés (jamais au-dessus de Classer, jamais protégés).
            classer = DOCUMENTS_LOCAL / "Classer"
            for d in sorted(touched_dirs, key=lambda p: -len(p.parts)):
                if d.exists() and classer in d.parents:
                    cleanup_empty_parents(d / "x", classer)
    finally:
        if owns_db:
            db.close()

    return {
        "archived": archived,
        "trashed": trashed,
        "list": [{"famille": fam, "files": f["files"], "bytes": f["bytes"],
                  "units": len(f["units"]), "ledger_run": f["run_id"]}
                 for fam, f in families.items()],
        "dry_run": dry_run,
        "errors": errors,
        "archives_root": str(root),
        "index": str(root / "_index.md"),
    }
