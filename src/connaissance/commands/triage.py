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
BUNDLE_SUFFIXES = {".app", ".abbu", ".ynab4", ".ynab3", ".bearbk",
                   ".photoslibrary", ".photolibrary", ".aplibrary",
                   ".imovielibrary", ".tvlibrary", ".fcpbundle", ".logicx",
                   ".band", ".scriv", ".rcproject", ".pkg"}

# Les EXPORTS ne sont PAS traités comme des conteneurs opaques : on les parcourt
# pour en extraire les vrais documents (ex. un vieux Google Drive contient des
# PDF à organiser). Les fichiers d'app sans valeur documentaire directe sont
# reconnus par leur EXTENSION (EXPORT_EXTS) et classés en B.

DOC_EXTS = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "csv", "txt",
            "rtf", "pages", "numbers", "key", "odt", "ods", "odp", "md",
            "markdown", "epub", "mobi", "azw3", "xlsm", "xlsb", "dotx",
            "docm", "pptm"}
MEDIA_EXTS = {"jpg", "jpeg", "png", "gif", "heic", "heif", "tiff", "tif",
              "bmp", "webp", "svg", "mp4", "mov", "avi", "mkv", "m4v", "mp3",
              "wav", "aac", "flac", "m4a", "raw", "cr2", "nef", "psd", "ai"}
CODE_EXTS = {"php", "phpt", "js", "mjs", "cjs", "ts", "jsx", "tsx", "vue",
             "tpl", "twig", "blade", "css", "scss", "sass", "less", "py",
             "pyw", "rb", "go", "rs", "java", "kt", "c", "h", "cpp", "hpp",
             "cs", "swift", "sql", "sh", "bash", "pl", "lua", "coffee",
             "json", "xml", "yml", "yaml", "lock", "ydiff", "phar", "map",
             "html", "htm", "ino", "po", "sample", "ttf", "otf", "woff",
             "woff2", "ipa"}
EXPORT_EXTS = {"enex", "nib", "abcdp", "abcdg", "ydevice", "smmx", "qfx",
               "ics", "vcf", "ynab4", "bib", "opml", "mm", "itmz", "qbo"}

# Détection « archive » par densité : un dossier d'au moins ARCHIVE_MIN_FILES
# fichiers dont ≤ ARCHIVE_MAX_DOC_RATIO sont de vrais documents = une unité à
# mettre de côté (résidu d'un dump/codebase). Le seuil protège les dossiers
# riches en documents (ex. un vieux Drive à ~14 % de docs ne se collapse PAS).
ARCHIVE_MIN_FILES = 100
ARCHIVE_MAX_DOC_RATIO = 0.03

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


def _subtree_stats(root: Path) -> tuple[dict, dict]:
    """Bottom-up : pour chaque dossier, (nb fichiers, nb vrais documents
    ORGANISABLES) de son sous-arbre.

    Les fichiers à l'intérieur d'un conteneur (repo/paquet) ne sont PAS
    organisables : un dossier qui n'est qu'un conteneur compte 0 document.
    Ainsi le ratio de documents d'une « archive » candidate reflète les vrais
    documents en vrac qu'elle contient (et pas des READMEs de repos imbriqués)."""
    total: dict[str, int] = {}
    docs: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        is_container = (Path(dirpath).suffix.lower() in BUNDLE_SUFFIXES
                        or bool(set(filenames) & CODE_MARKERS)
                        or bool(set(dirnames) & MARKER_DIRS))
        t = d = 0
        for f in filenames:
            if f.startswith("."):
                continue
            t += 1
            if _classify_ext(Path(f).suffix.lower().lstrip(".")) == "A_documents":
                d += 1
        for child in dirnames:
            cp = os.path.join(dirpath, child)
            t += total.get(cp, 0)
            d += docs.get(cp, 0)
        total[dirpath] = t
        docs[dirpath] = 0 if is_container else d   # conteneur → 0 doc organisable
    return total, docs


def triage(output_file: str | None = None) -> dict:
    """Cartographier ~/Documents en groupes A/B/C/D (lecture seule)."""
    root = DOCUMENTS_DIR
    sub_total, sub_docs = _subtree_stats(root)
    groups: Counter = Counter()        # fichiers EN VRAC (hors conteneurs)
    by_ext: Counter = Counter()
    repos: list[dict] = []
    bundles: list[dict] = []
    archive_roots: list[str] = []        # racines d'archives détectées
    arch_stats: dict[str, dict] = {}     # root → {path, docs, archived}
    container_files = 0                  # fichiers AVALÉS par conteneurs
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

        # Archive par densité : dossier volumineux quasi sans documents. On NE
        # l'enterre PAS — ses vrais documents sont toujours extraits vers le
        # groupe A ; seul son résidu NON-documentaire (médias, code…) est mis de
        # côté. Le seuil de détection protège les dossiers riches en documents.
        cur_archive = None
        for ar in archive_roots:
            if dirpath == ar or dirpath.startswith(ar + os.sep):
                cur_archive = ar
                break
        if cur_archive is None and d != root:
            st = sub_total.get(dirpath, 0)
            sd = sub_docs.get(dirpath, 0)
            if st >= ARCHIVE_MIN_FILES and (sd / st) <= ARCHIVE_MAX_DOC_RATIO:
                cur_archive = dirpath
                archive_roots.append(dirpath)
                arch_stats[dirpath] = {"path": str(d.relative_to(root)),
                                       "docs": 0, "archived": 0}

        for f in filenames:
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower().lstrip(".")
            by_ext[ext] += 1
            g = _classify_ext(ext)
            if g == "A_documents":
                # Un document est TOUJOURS extrait, où qu'il soit.
                groups["A_documents"] += 1
                documents.append(str((d / f).relative_to(root)))
                if cur_archive:
                    arch_stats[cur_archive]["docs"] += 1
            elif cur_archive:
                # Résidu non-documentaire d'une archive → mis de côté.
                container_files += 1
                arch_stats[cur_archive]["archived"] += 1
            else:
                groups[g] += 1

    archives = [
        {"path": s["path"], "files": s["docs"] + s["archived"],
         "docs_extracted": s["docs"], "archived": s["archived"]}
        for s in arch_stats.values()
    ]
    loose = sum(groups.values())
    payload = {
        "total_files": loose + container_files,
        "loose_files": loose,            # fichiers en vrac, à classer
        "groups": dict(groups.most_common()),   # décompte EN VRAC uniquement
        "containers": {
            "files_total": container_files,      # fichiers avalés par les unités
            "repos_code": sorted(repos, key=lambda r: -r["files"]),
            "bundles": sorted(bundles, key=lambda r: -r["files"]),
            "archives": sorted(archives, key=lambda r: -r["archived"]),
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
            "archives": len(p["containers"]["archives"]),
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
