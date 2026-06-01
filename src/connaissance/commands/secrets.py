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
from pathlib import Path

from connaissance.core import secrets as _secrets
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import (DOCUMENTS_DIR, documents_read_path,
                                      is_dataless)

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

# Notre propre vue + dossiers déjà classés : ignorés (comme le triage).
_SKIP_TOP = {"- Par catégorie", "- Sujets", "organismes", "personnes",
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
    skipped = {"dataless": 0, "too_big": 0, "binary": 0, "read_error": 0}

    if not base.exists():
        return {
            "flagged": 0, "files": [], "scanned": 0, "skipped": skipped,
            "note": f"{base} n'existe pas.",
        }

    for dirpath, dirnames, filenames in os.walk(base):
        if Path(dirpath) == DOCUMENTS_DIR:
            dirnames[:] = [n for n in dirnames if n not in _SKIP_TOP]

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
            "skipped": p["skipped"],
            "sample": [f["rel"] for f in p["files"][:15]],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
