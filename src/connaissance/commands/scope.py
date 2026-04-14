"""Module commands/scope : scanner et catégoriser ~/Documents/ pour le périmètre.

Expose :
- `scan(depth=None) -> ScopeScan`
- `check() -> dict`
- helpers de classification (`scan_directories`, `generate_report`, `is_excluded`)
"""
import os
from collections import Counter
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH, require_paths, require_connaissance_root

# ── Chemins ──────────────────────────────────────────────────────────────────

HOME = BASE_PATH
DOCUMENTS_DIR = HOME / "Documents"
CONFIG_DIR = HOME / "Connaissance" / ".config"
FILTRES_CONFIG = CONFIG_DIR / "filtres.yaml"
PERIMETRE_RAPPORT = CONFIG_DIR / "perimetre-rapport.json"
TEMPLATE_FILTRES = Path(__file__).resolve().parents[1] / "config" / "filtres.yaml"

# ── Extensions ───────────────────────────────────────────────────────────────

DOCUMENT_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".cs", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".pl", ".r", ".lua", ".scala", ".clj",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
}

# Marqueurs de projets de code (fichiers dont la présence signale un repo/projet)
CODE_MARKERS = {
    ".git", "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
    "Makefile", "CMakeLists.txt", "pom.xml", "build.gradle",
    "Gemfile", "composer.json", "mix.exs", "setup.py", "setup.cfg",
    "*.xcodeproj", "*.xcworkspace", "Podfile",
}

# Extensions de bundles macOS
BUNDLE_EXTENSIONS = {".app", ".framework", ".appex", ".kext", ".bundle", ".plugin"}

# ── Signaux de catégorisation ────────────────────────────────────────────────


def _has_code_marker(entries):
    """Vérifier si un ensemble d'entrées contient un marqueur de projet de code."""
    for entry in entries:
        if entry in CODE_MARKERS:
            return True
        # Patterns glob simples (*.xcodeproj)
        for marker in CODE_MARKERS:
            if marker.startswith("*") and entry.endswith(marker[1:]):
                return True
    return False


def _is_bundle_dir(name):
    """Vérifier si un nom de dossier est un bundle macOS."""
    return any(name.endswith(ext) for ext in BUNDLE_EXTENSIONS)


def _classify_dir(dir_path, entries, file_counts):
    """Classifier un dossier en catégorie.

    Args:
        dir_path: chemin du dossier
        entries: set des noms de fichiers/sous-dossiers directs
        file_counts: dict avec 'documents', 'code', 'images', 'other', 'total'

    Returns:
        (category, confidence, reason)
    """
    name = dir_path.name

    # 1. Bundles macOS
    if _is_bundle_dir(name):
        return "bundle_app", "high", f"Extension bundle ({name.split('.')[-1]})"

    # 2. Projets de code
    if _has_code_marker(entries):
        # Vérifier s'il y a aussi des documents
        if file_counts["documents"] > 0:
            return "documents_mixtes", "medium", \
                f"Repo de code avec {file_counts['documents']} documents"
        return "code_repo", "high", "Marqueurs de projet de code détectés"

    # 4. Forte densité de code (même sans marqueurs)
    total = file_counts["total"]
    if total > 10 and file_counts["code"] / total > 0.5:
        if file_counts["documents"] > 0:
            return "documents_mixtes", "medium", \
                f"{file_counts['code']} fichiers code, {file_counts['documents']} documents"
        return "code_repo", "medium", f"{file_counts['code']}/{total} fichiers sont du code"

    # 5. Photos personnelles (forte densité d'images avec noms typiques)
    if total > 10 and file_counts["images"] / total > 0.8:
        # Vérifier les noms typiques de photos
        photo_names = sum(1 for e in entries
                         if any(e.upper().startswith(p)
                                for p in ("IMG_", "DSC_", "DCIM", "DSCN", "P10", "GOPR")))
        if photo_names > 3 or name.lower() in ("souvenirs", "photos", "camera roll"):
            return "photos_perso", "high", \
                f"{file_counts['images']} images, noms de photos détectés"
        # Images mais pas clairement des photos
        return "photos_perso", "low", \
            f"{file_counts['images']}/{total} images — vérifier manuellement"

    # 6. Documents purs ou mixtes
    if file_counts["documents"] > 0:
        return "documents", "high", f"{file_counts['documents']} documents"

    # 7. Rien d'intéressant
    if total == 0:
        return "vide", "high", "Dossier vide"

    return "autre", "low", f"{total} fichiers sans documents OCR-compatibles"


# ── Scan de l'arborescence ───────────────────────────────────────────────────


def scan_directories(docs_dir, max_depth=3):
    """Scanner l'arborescence et classifier chaque dossier.

    Scanne jusqu'à max_depth niveaux de profondeur. Les sous-dossiers de bundles
    et de repos de code ne sont pas explorés plus profondément.

    Returns:
        list de dicts avec : path, rel_path, category, confidence, reason,
                            file_counts, depth
    """
    results = []
    docs_str = str(docs_dir)

    # SKIP_DIRS techniques
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".cache", "Library", ".Trash", "Backups", "Parallels"}

    # Dossiers dont on arrête l'exploration (catégorisés au niveau parent)
    stop_categories = {"bundle_app", "code_repo"}

    for root, dirs, files in os.walk(docs_dir):
        root_path = Path(root)

        # Profondeur relative
        try:
            rel = root_path.relative_to(docs_dir)
            depth = len(rel.parts)
        except ValueError:
            continue

        if max_depth is not None and depth > max_depth:
            dirs.clear()
            continue

        # Ignorer les SKIP_DIRS
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        # Ne pas analyser la racine elle-même
        if depth == 0:
            continue

        # Compter les fichiers par type (récursif partiel : seulement les enfants directs)
        entries = set(dirs + files)
        file_counts = Counter()
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in DOCUMENT_EXTENSIONS:
                if ext in {".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff"}:
                    file_counts["images"] += 1
                file_counts["documents"] += 1
            elif ext in CODE_EXTENSIONS:
                file_counts["code"] += 1
            else:
                file_counts["other"] += 1
            file_counts["total"] += 1

        # Classifier
        category, confidence, reason = _classify_dir(
            root_path, entries, file_counts)

        rel_path = str(rel)
        results.append({
            "path": str(root_path),
            "rel_path": rel_path,
            "category": category,
            "confidence": confidence,
            "reason": reason,
            "file_counts": dict(file_counts),
            "depth": depth,
            "name": root_path.name,
        })

        # Arrêter l'exploration pour les catégories terminales
        if category in stop_categories:
            dirs.clear()

    return results


def count_recursive_files(dir_path, extensions=None):
    """Compter récursivement les fichiers (optionnellement filtrés par extension)."""
    count = 0
    try:
        for f in Path(dir_path).rglob("*"):
            if f.is_file():
                if extensions is None or f.suffix.lower() in extensions:
                    count += 1
    except (PermissionError, OSError):
        pass
    return count


# ── Chargement de la config ──────────────────────────────────────────────────


def load_config():
    """Charger la section documents de filtres.yaml (ou retourner None si absente)."""
    if FILTRES_CONFIG.exists():
        with open(FILTRES_CONFIG) as f:
            data = yaml.safe_load(f) or {}
            return data.get("documents", {})
    return None


def save_config(config):
    """Sauvegarder la section documents dans filtres.yaml."""
    require_connaissance_root()
    CONFIG_DIR.mkdir(parents=False, exist_ok=True)
    # Charger le fichier complet, mettre à jour la section documents
    full_config = {}
    if FILTRES_CONFIG.exists():
        with open(FILTRES_CONFIG) as f:
            full_config = yaml.safe_load(f) or {}
    full_config["documents"] = config
    with open(FILTRES_CONFIG, "w") as f:
        yaml.dump(full_config, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)


def init_config_if_needed():
    """Copier le template filtres.yaml si la config n'existe pas encore."""
    if not FILTRES_CONFIG.exists() and TEMPLATE_FILTRES.exists():
        require_connaissance_root()
        CONFIG_DIR.mkdir(parents=False, exist_ok=True)
        import shutil
        shutil.copy2(TEMPLATE_FILTRES, FILTRES_CONFIG)
        print(f"  Config initialisée depuis le template → {FILTRES_CONFIG}")
        return True
    return False


def is_excluded(rel_path, config):
    """Vérifier si un chemin relatif est exclu par la config.

    Normalise en NFC pour comparer : macOS renvoie du NFD (accents décomposés)
    tandis que le YAML stocke du NFC (accents composés).
    """
    import unicodedata
    if config is None:
        return False

    rel_path = unicodedata.normalize("NFC", rel_path)
    rel_parts = Path(rel_path).parts

    # Vérifier dossiers_inclus (prioritaire)
    for inc in config.get("dossiers_inclus", []):
        if rel_path.startswith(unicodedata.normalize("NFC", inc)):
            return False

    # Vérifier dossiers_exclus
    for exc in config.get("dossiers_exclus", []):
        if rel_path.startswith(unicodedata.normalize("NFC", exc)):
            return True

    # Vérifier patterns_exclus
    for pattern in config.get("patterns_exclus", []):
        # Patterns simples : *.app, *.framework
        if pattern.startswith("*"):
            suffix = pattern[1:]
            if any(part.endswith(suffix) for part in rel_parts):
                return True

    return False


def _is_explicitly_included(rel_path, config):
    """Vérifier si un chemin est dans dossiers_inclus."""
    import unicodedata
    if config is None:
        return False
    rel_path = unicodedata.normalize("NFC", rel_path)
    for inc in config.get("dossiers_inclus", []):
        if rel_path.startswith(unicodedata.normalize("NFC", inc)):
            return True
    return False


# ── Rapport ──────────────────────────────────────────────────────────────────


def generate_report(results, config):
    """Générer le rapport de scan avec les dossiers à décider."""
    # Séparer les dossiers déjà décidés vs à présenter
    decided = []
    to_present = []

    for r in results:
        if is_excluded(r["rel_path"], config):
            r["status"] = "exclu"
            decided.append(r)
        elif _is_explicitly_included(r["rel_path"], config):
            r["status"] = "inclus"
            decided.append(r)
        elif r["category"] in ("documents", "vide", "autre"):
            r["status"] = "inclus"
            decided.append(r)
        else:
            r["status"] = "a_decider"
            to_present.append(r)

    # Grouper par catégorie pour la présentation
    by_category = {}
    for r in to_present:
        cat = r["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(r)

    # Compter les fichiers OCR-compatibles pour chaque dossier à décider
    for r in to_present:
        r["recursive_docs"] = count_recursive_files(
            r["path"], DOCUMENT_EXTENSIONS)

    report = {
        "source": str(DOCUMENTS_DIR),
        "total_dirs_scanned": len(results),
        "already_decided": len(decided),
        "to_present": len(to_present),
        "by_category": {
            cat: {
                "count": len(items),
                "total_files": sum(i.get("recursive_docs", 0) for i in items),
                "items": sorted(items, key=lambda x: -x.get("recursive_docs", 0)),
            }
            for cat, items in by_category.items()
        },
        "summary": {
            cat: len([r for r in results if r["category"] == cat])
            for cat in sorted(set(r["category"] for r in results))
        },
    }

    return report


# ── Affichage ────────────────────────────────────────────────────────────────


CATEGORY_LABELS = {
    "bundle_app": "Bundles d'applications macOS",
    "code_repo": "Projets de code",
    "photos_perso": "Photos personnelles",
    "documents_mixtes": "Documents mélangés avec du code",
    "documents": "Documents",
    "vide": "Dossiers vides",
    "autre": "Autres",
}

CATEGORY_ORDER = [
    "bundle_app", "code_repo",
    "photos_perso", "documents_mixtes",
]


def print_stats(results, config):
    """Afficher un résumé rapide du scan."""
    print(f"\n{'='*60}")
    print(f"  Scan du périmètre — {DOCUMENTS_DIR}")
    print(f"{'='*60}")

    total = len(results)
    by_cat = Counter(r["category"] for r in results)
    by_status = Counter()
    for r in results:
        if is_excluded(r["rel_path"], config):
            by_status["exclu"] += 1
        elif _is_explicitly_included(r["rel_path"], config):
            by_status["inclus"] += 1
        elif r["category"] in ("documents", "vide", "autre"):
            by_status["inclus"] += 1
        else:
            by_status["a_decider"] += 1

    print(f"\n  {total} dossiers analysés\n")
    print(f"  {'Catégorie':<30} {'Dossiers':>8}")
    print(f"  {'─'*30} {'─'*8}")
    for cat in CATEGORY_ORDER:
        if cat in by_cat:
            label = CATEGORY_LABELS.get(cat, cat)
            print(f"  {label:<30} {by_cat[cat]:>8}")
    for cat in sorted(by_cat):
        if cat not in CATEGORY_ORDER:
            label = CATEGORY_LABELS.get(cat, cat)
            print(f"  {label:<30} {by_cat[cat]:>8}")

    print(f"\n  Statut :")
    print(f"    Déjà exclus (config)  : {by_status.get('exclu', 0)}")
    print(f"    Inclus (documents)    : {by_status.get('inclus', 0)}")
    print(f"    À décider             : {by_status.get('a_decider', 0)}")


# ── Main ─────────────────────────────────────────────────────────────────────


# --- API publique ---


def scan(depth: int | None = 3) -> dict:
    """Scanner ~/Documents/ et produire le rapport (schema ScopeScan).

    Parameters
    ----------
    depth : int | None
        Profondeur max du scan. None = illimitée.
    """
    require_paths(DOCUMENTS_DIR, context="scope scan")

    config = load_config()
    if config is None:
        init_config_if_needed()
        config = load_config()

    results = scan_directories(DOCUMENTS_DIR, max_depth=depth)
    report = generate_report(results, config)

    import json as _json
    require_connaissance_root()
    CONFIG_DIR.mkdir(parents=False, exist_ok=True)
    with open(PERIMETRE_RAPPORT, "w") as f:
        _json.dump(report, f, indent=2, ensure_ascii=False)

    return {
        "root": str(DOCUMENTS_DIR),
        "total_dirs_scanned": report["total_dirs_scanned"],
        "already_decided": report["already_decided"],
        "to_present": report["to_present"],
        "by_category": report["by_category"],
        "summary": report["summary"],
        "report_path": str(PERIMETRE_RAPPORT),
    }


def check() -> dict:
    """Vérifier la config de périmètre actuelle."""
    config = load_config()
    if config is None:
        return {
            "has_config": False,
            "config_path": str(FILTRES_CONFIG),
            "dossiers_exclus": 0,
            "patterns_exclus": 0,
            "dossiers_inclus": 0,
        }
    return {
        "has_config": True,
        "config_path": str(FILTRES_CONFIG),
        "dossiers_exclus": len(config.get("dossiers_exclus", [])),
        "patterns_exclus": len(config.get("patterns_exclus", [])),
        "dossiers_inclus": len(config.get("dossiers_inclus", [])),
    }


def include(folder: str) -> dict:
    """Ajouter un dossier à dossiers_inclus (mutation filtres.yaml)."""
    config = load_config() or {}
    included = list(config.get("dossiers_inclus", []))
    if folder in included:
        return {"added": [], "filtres_yaml_mutated": False}
    included.append(folder)
    config["dossiers_inclus"] = included
    save_config(config)
    return {"added": [folder], "filtres_yaml_mutated": True}


def exclude(folder: str) -> dict:
    """Ajouter un dossier à dossiers_exclus (mutation filtres.yaml)."""
    config = load_config() or {}
    excluded = list(config.get("dossiers_exclus", []))
    if folder in excluded:
        return {"added": [], "filtres_yaml_mutated": False}
    excluded.append(folder)
    config["dossiers_exclus"] = excluded
    save_config(config)
    return {"added": [folder], "filtres_yaml_mutated": True}
