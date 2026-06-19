"""Détection de secrets dans ~/Documents (lecture seule, zéro OCR, zéro réseau).

Parcourt l'arborescence et signale les fichiers contenant des identifiants
sensibles (clés, mots de passe, jetons) pour **quarantaine** — ils ne doivent
pas être classés en clair, ni indexés (qmd), ni envoyés à un service externe
(OCR Mistral, Batch API). Voir [`core/secrets.py`](../core/secrets.py) pour la
logique de détection.

Garde-fous fidèles au reste du pipeline :

  - **Aucun téléchargement iCloud** : le contenu est lu via le miroir SSD
    (``documents_read_path``) ; si le fichier est ``dataless`` et qu'aucun
    miroir n'est disponible, on NE lit PAS le contenu (signal nom de fichier
    seulement) — compté dans ``skipped.dataless``.
  - **Seuls les formats texte sont ouverts** (``.txt``, ``.csv``, ``.json``,
    ``.env``, ``.yaml``, ``.sql``, ``.pem``…). Les PDF/Office ne sont pas
    déchiffrés ici (regex sur du binaire compressé ne trouve rien de fiable) ;
    leur **nom** reste analysé (``mots de passe.pdf`` est tout de même flagué).
  - **Lecture seule** : ``secrets scan`` ne déplace/ne supprime rien. La mise
    de côté effective (déplacement vers une zone protégée) sera un geste séparé,
    journalisé au ledger et soumis à validation.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from connaissance.commands.triage import (BUNDLE_SUFFIXES, CODE_MARKERS,
                                           MARKER_DIRS)
from connaissance.core import filtres as _filtres
from connaissance.core import ledger as _ledger
from connaissance.core import secrets as _secrets
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (DOCUMENTS_DIR, SPECIAL_TOP_DIRS,
                                      documents_read_path, is_dataless,
                                      require_connaissance_root)

# Zone physique des secrets relocalisés (préfixe « - » → hors scan pipeline).
PROTECTED_SUBDIR = "- Protégés/secrets"

# Sous-dossiers de dépendances/build : du code tiers, jamais TES secrets. On n'y
# descend pas (un `password=` dans une fixture de test n'est pas un identifiant
# réel). Complète MARKER_DIRS (node_modules, vendor, .git… déjà couverts).
_NOISE_DIRS = {"bower_components", "Pods", "site-packages", ".tox", ".venv",
               "venv", "dist", "build", ".next", ".nuxt", "__pycache__"}

# Extensions dont on lit le CONTENU (texte brut). Volontairement étroit : un
# faux .pdf/.docx ne livre rien d'exploitable en regex, mais ces formats-là si.
TEXT_SCAN_EXTS = {
    ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".ini", ".conf",
    ".cfg", ".config", ".env", ".xml", ".md", ".markdown", ".sql", ".sh",
    ".bash", ".zsh", ".html", ".htm", ".log", ".properties", ".toml",
    ".pem", ".ppk", ".asc", ".netrc", ".htpasswd", ".pgpass", ".php",
    ".py", ".rb", ".js", ".ts",
}
# Fichiers sans extension dont le contenu mérite quand même lecture.
_EXTLESS_SCAN = {".env", ".netrc", ".pgpass", ".htpasswd", ".git-credentials",
                 "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}

# Dossiers spéciaux (vues/relocations, source unique) + déjà classés.
_SKIP_TOP = set(SPECIAL_TOP_DIRS) | {"organismes", "personnes",
                                     "divers", "promus"}

_MAX_BYTES = 5_000_000   # au-delà, on ne lit pas le contenu (gros log/dump)


def _should_read_content(path: Path) -> bool:
    suf = path.suffix.lower()
    if suf in TEXT_SCAN_EXTS:
        return True
    return path.name.lower() in _EXTLESS_SCAN


def scan(scope: str | None = None, output_file: str | None = None) -> dict:
    """Scanner ~/Documents (ou un sous-dossier) à la recherche de secrets.

    Args:
        scope: sous-chemin relatif à ~/Documents pour restreindre le scan
               (ex. ``"Classer/old"``). ``None`` = tout ~/Documents.
        output_file: si fourni, écrit le rapport complet en JSON et renvoie un
               récap compact.

    Lecture seule. Retourne ``{flagged, files: [...], skipped, scanned, note}``
    (schema ``SecretsScan``).
    """
    base = DOCUMENTS_DIR if scope is None else (DOCUMENTS_DIR / scope)
    files: list[dict] = []
    scanned = 0
    containers_skipped = 0
    skipped = {"dataless": 0, "too_big": 0, "binary": 0, "read_error": 0}

    if not base.exists():
        return {
            "flagged": 0, "files": [], "scanned": 0, "skipped": skipped,
            "note": f"{base} n'existe pas.",
        }

    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        if d == DOCUMENTS_DIR:
            dirnames[:] = [n for n in dirnames if n not in _SKIP_TOP]

        # Conteneur (repo de code, bundle macOS) → unité tierce : on n'y descend
        # pas. Un secret dans `phpseclib/Crypt/RSA.php` ou une fixture npm est du
        # code, pas un identifiant réel. Même logique que `documents triage`.
        is_bundle = d.suffix.lower() in BUNDLE_SUFFIXES
        is_repo = bool(set(filenames) & CODE_MARKERS) \
            or bool(set(dirnames) & MARKER_DIRS)
        if is_bundle or is_repo:
            containers_skipped += 1
            dirnames[:] = []
            continue
        # Élaguer les dossiers de dépendances/build restants.
        dirnames[:] = [n for n in dirnames if n not in _NOISE_DIRS]

        for fname in filenames:
            if fname.startswith("."):
                # Les dotfiles sensibles (.env, id_rsa…) sont quand même couverts
                # par leur nom exact via filename_signal ; on ne saute donc pas
                # systématiquement, mais on ignore le bruit (.DS_Store…).
                if fname.lower() not in _EXTLESS_SCAN and \
                        _secrets.filename_signal(fname) is None:
                    continue
            scanned += 1
            fpath = Path(dirpath) / fname
            rel = str(fpath.relative_to(DOCUMENTS_DIR))

            name_sig = _secrets.filename_signal(fname)
            content_findings: list[dict] = []

            if _should_read_content(fpath):
                read_path = documents_read_path(fpath)
                # Pas de miroir et fichier non matérialisé → ne pas déclencher
                # un download iCloud pour scanner : nom de fichier seulement.
                if read_path == fpath and is_dataless(fpath):
                    skipped["dataless"] += 1
                else:
                    try:
                        if read_path.stat().st_size > _MAX_BYTES:
                            skipped["too_big"] += 1
                        else:
                            data = read_path.read_bytes()
                            if _secrets.is_probably_binary(data):
                                skipped["binary"] += 1
                            else:
                                text = data.decode("utf-8", errors="replace")
                                content_findings = list(_secrets.scan_text(text))
                    except OSError:
                        skipped["read_error"] += 1

            if not name_sig and not content_findings:
                continue

            severity = "high" if (
                (name_sig and name_sig[1] == "high")
                or any(f["severity"] == "high" for f in content_findings)
            ) else "medium"

            files.append({
                "rel": rel,
                "severity": severity,
                "filename_signal": name_sig[0] if name_sig else None,
                "findings": content_findings,
                "findings_count": len(content_findings),
            })

    files.sort(key=lambda f: (f["severity"] != "high", -f["findings_count"]))
    payload = {
        "flagged": len(files),
        "files": files,
        "scanned": scanned,
        "containers_skipped": containers_skipped,
        "skipped": skipped,
        "note": (
            "Lecture seule. Les fichiers listés contiennent des secrets "
            "(clés/mots de passe) probables : à mettre en quarantaine — ne pas "
            "classer en clair, ne pas indexer (qmd), ne jamais envoyer à un "
            "service externe (OCR/Batch API). Évidences caviardées."
        ),
    }

    def _summary(p: dict) -> dict:
        high = sum(1 for f in p["files"] if f["severity"] == "high")
        return {
            "flagged": p["flagged"],
            "high_severity": high,
            "scanned": p["scanned"],
            "containers_skipped": p["containers_skipped"],
            "skipped": p["skipped"],
            "sample": [f["rel"] for f in p["files"][:15]],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def quarantine_apply(scope: str | None = None,
                     include_medium: bool = False) -> dict:
    """Peupler la liste de quarantaine secrets depuis un scan (garde-fou ACTIF).

    Scanne (lecture seule), puis écrit les chemins détectés dans
    ``~/Connaissance/.config/secrets-quarantine.txt``. Les fichiers listés sont
    désormais **exclus de l'OCR, de l'index qmd et du Batch API**
    (``filtres.filter_document`` les rejette). **Ne déplace/ne supprime rien** —
    n'écrit qu'un fichier de config, fusionné avec l'existant (idempotent).

    Périmètre par défaut = ce qui pourrait réellement **fuir dans le pipeline** :
    toutes les détections **high** + les **medium OCR-éligibles** (PDF/docx/
    images — ceux-là seraient transcrits puis indexés ; ex. un
    ``Mot de passe.pdf``). Les medium non-OCR (affectations dans du .html/.txt,
    code) ne sont PAS ajoutés par défaut car le pipeline ne les transcrit pas —
    ``include_medium`` les ajoute quand même (couverture maximale).
    """
    require_connaissance_root()
    ocr_exts = set(_filtres.Filtres().docs_config.get("extensions", []))

    def _risky(f: dict) -> bool:
        if f["severity"] == "high":
            return True
        if f["severity"] != "medium":
            return False
        if include_medium:
            return True
        # medium auto-quarantiné seulement s'il serait transcrit (vrai risque).
        return Path(f["rel"]).suffix.lower() in ocr_exts

    report = scan(scope=scope)   # payload inline (read-only)
    selected = [f for f in report["files"] if _risky(f)]
    # NFC avant merge : les rels viennent du walk filesystem (NFD sur macOS),
    # l'existant est chargé en NFC → sans ça le merge dédoublerait.
    rels = {unicodedata.normalize("NFC", f["rel"]) for f in selected}

    existing = _filtres.load_quarantine_set()
    added = sorted(rels - existing)
    merged = existing | rels
    path = _filtres.write_quarantine_set(merged)

    return {
        "quarantine_file": str(path),
        "added": len(added),
        "already_present": len(rels & existing),
        "total_quarantined": len(merged),
        "high": sum(1 for f in selected if f["severity"] == "high"),
        "medium": sum(1 for f in selected if f["severity"] == "medium"),
        "added_sample": added[:20],
        "note": (
            "Garde-fou ACTIF posé : ces fichiers sont désormais exclus de "
            "l'OCR, de l'index qmd et du Batch API. RIEN n'a été déplacé ni "
            "supprimé — seule la liste de config a été écrite (éditable). "
            "Pour lever une entrée, retire sa ligne du fichier."
        ),
    }


def relocate(dry_run: bool = True, db=None) -> dict:
    """Déplacer PHYSIQUEMENT les secrets en quarantaine vers ``- Protégés/secrets/``
    (schema SecretsRelocate).

    Distinct du garde-fou actif (qui suffit à exclure du pipeline sans rien
    bouger) : ici on regroupe les fichiers listés en quarantaine sous un dossier
    dédié (préfixe « - » → hors scan), **via le ledger** (réversible). Structure
    d'origine préservée. **Dry-run par défaut.** Met à jour la liste de
    quarantaine vers les nouveaux chemins quand le déplacement est appliqué.
    """
    require_connaissance_root()
    from connaissance.core.tracking import TrackingDB
    rels = sorted(_filtres.load_quarantine_set())
    owns = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("secrets-relocate")
    moved: list[dict] = []
    skipped: list[dict] = []
    new_quarantine: set[str] = set()
    try:
        for rel in rels:
            # Déjà sous la zone protégée → ne pas re-déplacer, garder tel quel.
            if rel.startswith(PROTECTED_SUBDIR):
                new_quarantine.add(rel)
                continue
            src = DOCUMENTS_DIR / rel
            if not src.exists():
                skipped.append({"path": rel, "reason": "introuvable"})
                new_quarantine.add(rel)   # on conserve l'entrée telle quelle
                continue
            new_rel = f"{PROTECTED_SUBDIR}/{rel}"
            if dry_run:
                moved.append({"from": rel, "to": new_rel})
                new_quarantine.add(rel)
                continue
            try:
                _ledger.safe_move(db, src, DOCUMENTS_DIR / new_rel,
                                  "secret relocate", run_id)
                moved.append({"from": rel, "to": new_rel})
                new_quarantine.add(new_rel)
            except OSError as exc:
                skipped.append({"path": rel, "reason": str(exc)})
                new_quarantine.add(rel)
    finally:
        if owns:
            db.close()

    if not dry_run and moved:
        _filtres.write_quarantine_set(new_quarantine)

    result = {
        "dry_run": dry_run,
        "candidates": len(rels),
        "moved": 0 if dry_run else len(moved),
        "would_move": len(moved) if dry_run else 0,
        "skipped": skipped,
        "dest_root": str(DOCUMENTS_DIR / PROTECTED_SUBDIR),
        "sample": moved[:20],
    }
    if not dry_run and moved:
        result["ledger_run"] = run_id
    return result
