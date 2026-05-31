"""Phase A du chantier de réorganisation : triage des fichiers de ~/Documents.

Classe chaque fichier en quatre groupes — SANS OCR, en lecture seule :

  A_documents : vrais documents (pdf, docx, xlsx…) à pré-classer ensuite
  B_exports   : exports d'applications (Evernote, Takeout, YNAB, Bear…)
  C_media     : images, audio, vidéo
  D_code      : code et fichiers techniques

Principe clé : on ne déroule pas 67k fichiers un par un. On détecte les
**conteneurs de code/projet** — repo (marqueur fichier type composer.json, ou
dossier marqueur ``.git``/``.claude``…) et bundles ``.app`` — et on les traite
comme des **unités** (comptés en bloc, non parcourus). Un repo reste groupé.

Les **exports** (vieux Google Drive, etc.) ne sont PAS opaques : on les
**parcourt** pour en extraire les vrais documents (→ groupe A) ; les fichiers
d'app sans valeur documentaire (``.enex``, ``.smmx``…) restent en B par leur
extension.

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
    groups: Counter = Counter()
    by_ext: Counter = Counter()
    repos: list[dict] = []
    bundles: list[dict] = []
    documents: list[str] = []   # échantillon des vrais docs (groupe A)

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)

        # Ignorer la racine elle-même puis les dossiers déjà classés / notre vue.
        if d == root:
            dirnames[:] = [n for n in dirnames if n not in SKIP_TOP]

        # Conteneur de CODE/PROJET → unité, compté en bloc, non parcouru.
        # (Les exports ne sont PAS opaques : on les parcourt — voir plus bas.)
        is_bundle = d.suffix == ".app"
        is_repo = bool(set(filenames) & CODE_MARKERS) \
            or bool(set(dirnames) & MARKER_DIRS)
        if is_bundle or is_repo:
            cnt = _count_subtree(d)
            rel = str(d.relative_to(root))
            (bundles if is_bundle else repos).append({"path": rel, "files": cnt})
            groups["D_code"] += cnt
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

    total = sum(groups.values())
    payload = {
        "total_files": total,
        "groups": dict(groups.most_common()),
        "containers": {
            "repos_code": sorted(repos, key=lambda r: -r["files"]),
            "app_bundles": sorted(bundles, key=lambda r: -r["files"]),
        },
        "by_extension": dict(by_ext.most_common(40)),
        # Échantillon ÉTALÉ sur tout l'arbre (pas seulement le début du walk),
        # pour que les dossiers profonds (vieux Drive…) y figurent aussi.
        "documents_sample": documents[::max(1, len(documents) // 200)][:200],
    }

    def _summary(p: dict) -> dict:
        return {
            "total_files": p["total_files"],
            "groups": p["groups"],
            "repos_code": len(p["containers"]["repos_code"]),
            "app_bundles": len(p["containers"]["app_bundles"]),
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
