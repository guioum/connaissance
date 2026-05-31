"""Phase A du chantier de réorganisation : triage des fichiers de ~/Documents.

Classe chaque fichier en quatre groupes — SANS OCR, en lecture seule :

  A_documents : vrais documents (pdf, docx, xlsx…) à pré-classer ensuite
  B_exports   : exports d'applications (Evernote, Takeout, YNAB, Bear…)
  C_media     : images, audio, vidéo
  D_code      : code et fichiers techniques

Principe clé : on ne déroule pas 67k fichiers un par un. On détecte d'abord les
**conteneurs** — un repo de code (présence d'un marqueur type composer.json),
un bundle ``.app``, un dossier d'export — et on les traite comme des **unités**
(comptés en bloc, non parcourus). Un repo reste ainsi groupé.

Rien n'est déplacé : ``triage`` ne fait que cartographier (schema Triage).
"""
import os
from collections import Counter
from pathlib import Path

from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import DOCUMENTS_DIR

# Marqueurs : si un dossier contient l'un de ces fichiers, c'est un repo de code.
CODE_MARKERS = {
    "composer.json", "package.json", "package-lock.json", "yarn.lock",
    "composer.lock", "Gemfile", "requirements.txt", "pyproject.toml",
    "pom.xml", "build.gradle", "Cargo.toml", "go.mod", ".gitignore",
    "Makefile", "tsconfig.json", "webpack.config.js",
}
# Dossiers dont le NOM trahit un export d'application.
EXPORT_DIR_NAMES = {"takeout", "google drive", "evernote", "bear", "ynab",
                    "address book", "carnet d'adresses"}

DOC_EXTS = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "csv", "txt",
            "rtf", "pages", "numbers", "key", "odt", "ods", "odp", "md"}
MEDIA_EXTS = {"jpg", "jpeg", "png", "gif", "heic", "heif", "tiff", "tif",
              "bmp", "webp", "svg", "mp4", "mov", "avi", "mkv", "m4v", "mp3",
              "wav", "aac", "flac", "m4a", "raw", "cr2", "nef", "psd", "ai"}
CODE_EXTS = {"php", "phpt", "js", "mjs", "cjs", "ts", "jsx", "tsx", "vue",
             "tpl", "twig", "blade", "css", "scss", "sass", "less", "py",
             "rb", "go", "rs", "java", "kt", "c", "h", "cpp", "hpp", "cs",
             "swift", "sql", "sh", "bash", "pl", "lua", "coffee", "json",
             "xml", "yml", "yaml", "lock", "ydiff", "phar", "map"}
EXPORT_EXTS = {"enex", "nib", "abcdp", "ics", "vcf", "ynab4", "bib", "opml"}

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
    groups: Counter = Counter()
    by_ext: Counter = Counter()
    repos: list[dict] = []
    bundles: list[dict] = []
    exports: list[dict] = []
    documents: list[str] = []   # échantillon des vrais docs (groupe A)

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)

        # Ignorer la racine elle-même puis les dossiers déjà classés / notre vue.
        if d == root:
            dirnames[:] = [n for n in dirnames if n not in SKIP_TOP]
            # ne pas tomber dans les fichiers de la racine non plus ? on les traite.

        names = set(filenames)
        is_bundle = d.suffix == ".app"
        is_repo = bool(names & CODE_MARKERS)
        is_export = d.name.lower() in EXPORT_DIR_NAMES

        # Conteneur → unité, on compte en bloc et on n'y descend pas.
        if is_bundle or is_repo or is_export:
            cnt = _count_subtree(d)
            rel = str(d.relative_to(root))
            if is_bundle:
                bundles.append({"path": rel, "files": cnt})
                groups["D_code"] += cnt
            elif is_repo:
                repos.append({"path": rel, "files": cnt})
                groups["D_code"] += cnt
            else:
                exports.append({"path": rel, "files": cnt})
                groups["B_exports"] += cnt
            dirnames[:] = []
            continue

        for f in filenames:
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower().lstrip(".")
            by_ext[ext] += 1
            g = _classify_ext(ext)
            groups[g] += 1
            if g == "A_documents" and len(documents) < 200:
                documents.append(str((d / f).relative_to(root)))

    total = sum(groups.values())
    payload = {
        "total_files": total,
        "groups": dict(groups.most_common()),
        "containers": {
            "repos_code": sorted(repos, key=lambda r: -r["files"]),
            "app_bundles": sorted(bundles, key=lambda r: -r["files"]),
            "exports": sorted(exports, key=lambda r: -r["files"]),
        },
        "by_extension": dict(by_ext.most_common(40)),
        "documents_sample": documents,
    }

    def _summary(p: dict) -> dict:
        return {
            "total_files": p["total_files"],
            "groups": p["groups"],
            "repos_code": len(p["containers"]["repos_code"]),
            "app_bundles": len(p["containers"]["app_bundles"]),
            "exports": len(p["containers"]["exports"]),
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
