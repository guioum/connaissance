"""Phase A du chantier de réorganisation : triage des fichiers de ~/Documents.

Classe chaque fichier en quatre groupes — SANS OCR, en lecture seule :

  A_documents : vrais documents (pdf, docx, xlsx…) à pré-classer ensuite
  B_exports   : exports d'applications (Evernote, Takeout, YNAB, Bear…)
  C_media     : images, audio, vidéo
  D_code      : code et fichiers techniques

Principe clé : on ne déroule pas 67k fichiers un par un. On détecte les
**conteneurs** et on les traite comme des **unités** (comptées en bloc, non
parcourues, et EXCLUES du décompte des groupes — ``groups`` ne compte que les
fichiers EN VRAC à classer) :

  - repos de code/projet : marqueur fichier (composer.json…) ou dossier
    marqueur (``.git``, ``.claude``…) ;
  - paquets macOS : ``.app``, ``.abbu`` (backup Contacts), ``.ynab4`` (budget),
    photothèques… — on n'organise pas l'intérieur d'un paquet.

Les **exports « en vrac »** (vieux Google Drive, etc.) ne sont PAS opaques : on
les **parcourt** pour en extraire les vrais documents (→ groupe A) ; les
fichiers d'app sans valeur documentaire (``.enex``, ``.smmx``…) restent en B
par leur extension.

Rien n'est déplacé : ``triage`` ne fait que cartographier (schema Triage).
"""
import os
from collections import Counter
from pathlib import Path

from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import DOCUMENTS_DIR

# Marqueurs FICHIERS : si un dossier en contient un, c'est un repo de code.
CODE_MARKERS = {
    "composer.json", "package.json", "package-lock.json", "yarn.lock",
    "composer.lock", "Gemfile", "requirements.txt", "pyproject.toml",
    "pom.xml", "build.gradle", "Cargo.toml", "go.mod", ".gitignore",
    "Makefile", "tsconfig.json", "webpack.config.js",
}
# Marqueurs DOSSIERS : leur présence signe un projet (même sans marqueur fichier).
MARKER_DIRS = {".git", ".claude", ".svn", ".hg", "node_modules", "vendor"}

# Paquets macOS / bundles d'app : dossiers à traiter en UNITÉS (on n'organise pas
# l'intérieur d'un backup Contacts .abbu, d'un budget .ynab4, d'une photothèque…).
BUNDLE_SUFFIXES = {".app", ".abbu", ".ynab4", ".photoslibrary", ".photolibrary",
                   ".aplibrary", ".imovielibrary", ".tvlibrary", ".fcpbundle",
                   ".logicx", ".band", ".scriv", ".rcproject", ".pkg"}

# Les EXPORTS ne sont PAS traités comme des conteneurs opaques : on les parcourt
# pour en extraire les vrais documents (ex. un vieux Google Drive contient des
# PDF à organiser). Les fichiers d'app sans valeur documentaire directe sont
# reconnus par leur EXTENSION (EXPORT_EXTS) et classés en B.

DOC_EXTS = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "csv", "txt",
            "rtf", "pages", "numbers", "key", "odt", "ods", "odp", "md",
            "markdown", "epub", "mobi", "azw3"}
MEDIA_EXTS = {"jpg", "jpeg", "png", "gif", "heic", "heif", "tiff", "tif",
              "bmp", "webp", "svg", "mp4", "mov", "avi", "mkv", "m4v", "mp3",
              "wav", "aac", "flac", "m4a", "raw", "cr2", "nef", "psd", "ai"}
CODE_EXTS = {"php", "phpt", "js", "mjs", "cjs", "ts", "jsx", "tsx", "vue",
             "tpl", "twig", "blade", "css", "scss", "sass", "less", "py",
             "rb", "go", "rs", "java", "kt", "c", "h", "cpp", "hpp", "cs",
             "swift", "sql", "sh", "bash", "pl", "lua", "coffee", "json",
             "xml", "yml", "yaml", "lock", "ydiff", "phar", "map", "html",
             "htm", "ino", "po", "sample", "ttf", "otf", "woff", "woff2"}
EXPORT_EXTS = {"enex", "nib", "abcdp", "abcdg", "ydevice", "smmx", "qfx",
               "ics", "vcf", "ynab4", "bib", "opml", "mm"}

# Notre propre vue (raccourcis) + dossiers déjà classés : à ne pas triager.
SKIP_TOP = {"- Par catégorie", "organismes", "personnes", "divers", "promus"}


def _classify_ext(ext: str) -> str:
    if ext in DOC_EXTS:
        return "A_documents"
    if ext in MEDIA_EXTS:
        return "C_media"
    if ext in EXPORT_EXTS:
        return "B_exports"
    if ext in CODE_EXTS:
        return "D_code"
    return "autre"


def _count_subtree(d: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(d))


def triage(output_file: str | None = None) -> dict:
    """Cartographier ~/Documents en groupes A/B/C/D (lecture seule)."""
    root = DOCUMENTS_DIR
    groups: Counter = Counter()        # fichiers EN VRAC (hors conteneurs)
    by_ext: Counter = Counter()
    repos: list[dict] = []
    bundles: list[dict] = []
    container_files = 0                  # fichiers AVALÉS par repos + paquets
    documents: list[str] = []           # échantillon des vrais docs (groupe A)

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)

        # Ignorer la racine elle-même puis les dossiers déjà classés / notre vue.
        if d == root:
            dirnames[:] = [n for n in dirnames if n not in SKIP_TOP]

        # Conteneur → UNITÉ : compté en bloc, non parcouru, exclu des groupes.
        # Repo de code/projet (marqueur fichier ou dossier .git/.claude…) OU
        # paquet macOS (.app, .abbu, .ynab4, photothèque…).
        # (Les exports « fichiers en vrac » comme un vieux Drive sont parcourus.)
        is_bundle = d.suffix.lower() in BUNDLE_SUFFIXES
        is_repo = bool(set(filenames) & CODE_MARKERS) \
            or bool(set(dirnames) & MARKER_DIRS)
        if is_bundle or is_repo:
            cnt = _count_subtree(d)
            container_files += cnt
            rel = str(d.relative_to(root))
            if is_bundle:
                bundles.append({"path": rel, "files": cnt,
                                "type": d.suffix.lower().lstrip(".")})
            else:
                repos.append({"path": rel, "files": cnt})
            dirnames[:] = []
            continue

        for f in filenames:
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower().lstrip(".")
            by_ext[ext] += 1
            g = _classify_ext(ext)
            groups[g] += 1
            if g == "A_documents":
                documents.append(str((d / f).relative_to(root)))

    loose = sum(groups.values())
    payload = {
        "total_files": loose + container_files,
        "loose_files": loose,            # fichiers en vrac, à classer
        "groups": dict(groups.most_common()),   # décompte EN VRAC uniquement
        "containers": {
            "files_total": container_files,      # fichiers avalés par les unités
            "repos_code": sorted(repos, key=lambda r: -r["files"]),
            "bundles": sorted(bundles, key=lambda r: -r["files"]),
        },
        "by_extension": dict(by_ext.most_common(40)),
        # Échantillon ÉTALÉ sur tout l'arbre (pas seulement le début du walk),
        # pour que les dossiers profonds (vieux Drive…) y figurent aussi.
        "documents_sample": documents[::max(1, len(documents) // 200)][:200],
    }

    def _summary(p: dict) -> dict:
        return {
            "total_files": p["total_files"],
            "loose_files": p["loose_files"],
            "groups": p["groups"],
            "container_files": p["containers"]["files_total"],
            "repos_code": len(p["containers"]["repos_code"]),
            "bundles": len(p["containers"]["bundles"]),
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
