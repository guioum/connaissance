#!/usr/bin/env python3
"""Archiver les non-documents détectés par le scanner de périmètre.

Déplace les dossiers exclus (code, photos, bundles, téléchargements) vers
`~/Documents/- Archives/{Code,Photos,Applications,Téléchargements}/` et
nettoie les dossiers parents devenus vides. Met à jour `filtres.yaml`
automatiquement : les chemins exclus qui ont été déplacés sont retirés.

API publique : `archive(dry_run, category)`.
"""
import sys
import json
import os
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH, require_paths, require_connaissance_root
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
    with open(PERIMETRE_CONFIG, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)


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
        for item in data.get("items", []):
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
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            moved.append(m)

            # Tracking
            try:
                db = TrackingDB()
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

    return moved, skipped, errors, cleaned_dirs


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


# ── Affichage ───────────────────────────────────────────────────────────────

def print_plan(moves):
    """Afficher le plan de déplacement."""
    by_subdir = defaultdict(list)
    for m in moves:
        by_subdir[m["subdir"]].append(m)

    total = len(moves)
    print(f"\n{'='*60}", file=sys.stderr)

    print(f"  Plan d'archivage — {total} dossiers", file=sys.stderr)

    print(f"{'='*60}", file=sys.stderr)


    for subdir in ["Applications", "Code", "Photos", "Divers"]:
        items = by_subdir.get(subdir, [])
        if not items:
            continue
        print(f"\n  - Archives/{subdir}/ ({len(items)} dossiers)", file=sys.stderr)

        print(f"  {'─'*50}", file=sys.stderr)

        for m in items:
            name = Path(m["rel_path"]).name
            print(f"    {name}", file=sys.stderr)

            print(f"      ← {m['rel_path'][:70]}", file=sys.stderr)



def print_results(moved, errors, cleaned_dirs, dry_run=False):
    """Afficher les résultats."""
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n  {prefix}{len(moved)} dossiers {'à déplacer' if dry_run else 'déplacés'}", file=sys.stderr)

    if cleaned_dirs:
        print(f"  {len(cleaned_dirs)} dossiers vides nettoyés", file=sys.stderr)

    if errors:
        print(f"\n  ⚠ {len(errors)} erreurs :", file=sys.stderr)

        for e in errors:
            print(f"    {Path(e['rel_path']).name} : {e.get('error', '?')}", file=sys.stderr)



# --- API publique ---


def archive(dry_run: bool = True, category: str | None = None) -> dict:
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

    moved, _skipped, errors, _cleaned_dirs = execute_moves(moves)
    if moved:
        config, removed_count = update_config_after_moves(config, moved)
        if removed_count > 0:
            save_config(config)

    return {
        "archived": len(moved),
        "list": [{"source": str(m.get("source")), "dest": str(m.get("dest"))}
                 for m in moved],
        "errors": errors,
        "dry_run": False,
    }
