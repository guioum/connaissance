"""Module commands/audit : vérifications d'intégrité déterministes.

Expose `check(steps=["all"]) -> AuditResult` qui agrège les 6 vérifications.
Les fonctions individuelles (`liens_casses`, `frontmatter_invalide`, etc.)
restent disponibles comme helpers.
"""

import re
from pathlib import Path

import yaml

from connaissance.core.paths import BASE_PATH
from connaissance.core.tracking import TrackingDB

CONNAISSANCE = BASE_PATH / "Connaissance"
RESUMES = CONNAISSANCE / "Résumés"
TRANSCRIPTIONS = CONNAISSANCE / "Transcriptions"
SYNTHESE = CONNAISSANCE / "Synthèse"
DOCUMENTS_DIR = BASE_PATH / "Documents"

# Champs requis par type de fichier
CHAMPS_REQUIS = {
    "courriel": ["type", "date", "from", "direction", "title", "category"],
    "fil": ["type", "date-start", "date-end", "from", "title", "category",
            "message-count"],
    "document": ["type", "date", "title", "category"],
    "note": ["type", "date", "title", "category"],
    "personne": ["type", "slug", "status", "first-contact", "last-contact",
                 "created", "modified"],
    "organisme": ["type", "subtype", "slug", "status", "first-contact",
                  "last-contact", "created", "modified"],
}


def _lire_frontmatter(path: Path) -> dict | None:
    """Lire le frontmatter YAML d'un fichier Markdown.

    Cherche la fin du frontmatter via ``\\n---`` (newline + tirets) pour
    ne pas se faire tromper par des ``---`` présents dans des valeurs de
    champs (ex: chemins de résumés organisés de la forme
    ``date---entité---titre.md``).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    try:
        return yaml.safe_load(content[4:end]) or {}
    except yaml.YAMLError:
        return None


# --- Vérification 1 : Liens cassés ---

def verifier_liens_casses() -> list[dict]:
    """Vérifier que les relations des fiches pointent vers des entités existantes."""
    problemes = []
    for type_dir in ("personnes", "organismes"):
        fiches_dir = SYNTHESE / type_dir
        if not fiches_dir.exists():
            continue
        for fiche in fiches_dir.rglob("fiche.md"):
            fm = _lire_frontmatter(fiche)
            if not fm:
                continue
            for rel in fm.get("relations", []):
                entity_ref = rel.get("entity", "")
                if not entity_ref:
                    continue
                target = SYNTHESE / entity_ref
                if not target.exists():
                    problemes.append({
                        "fichier": str(fiche.relative_to(CONNAISSANCE)),
                        "relation": entity_ref,
                        "probleme": "entité cible inexistante",
                    })
    return problemes


# --- Vérification 2 : Frontmatter invalide ---

def verifier_frontmatter() -> list[dict]:
    """Vérifier les champs requis dans le frontmatter de chaque fichier."""
    problemes = []
    dirs_a_scanner = [RESUMES, SYNTHESE]

    for base_dir in dirs_a_scanner:
        if not base_dir.exists():
            continue
        for md_file in base_dir.rglob("*.md"):
            fm = _lire_frontmatter(md_file)
            if fm is None:
                continue
            file_type = fm.get("type")
            if file_type not in CHAMPS_REQUIS:
                continue
            manquants = [
                champ for champ in CHAMPS_REQUIS[file_type]
                if champ not in fm or fm[champ] is None
            ]
            if manquants:
                problemes.append({
                    "fichier": str(md_file.relative_to(CONNAISSANCE)),
                    "type": file_type,
                    "champs_manquants": manquants,
                })
    return problemes


# --- Vérification 3 : Triplets désynchronisés ---

def verifier_triplets() -> list[dict]:
    """Vérifier la synchronisation original/transcription/résumé pour les documents."""
    problemes = []
    resumes_docs = RESUMES / "Documents"
    if not resumes_docs.exists():
        return problemes

    for resume_file in resumes_docs.rglob("*.md"):
        rel_path = resume_file.relative_to(resumes_docs)

        # Transcription correspondante
        trans_path = TRANSCRIPTIONS / "Documents" / rel_path
        if not trans_path.exists():
            problemes.append({
                "fichier": str(resume_file.relative_to(CONNAISSANCE)),
                "manquant": "transcription",
                "attendu": str(trans_path.relative_to(CONNAISSANCE)),
            })

        # Original — chercher avec différentes extensions
        fm = _lire_frontmatter(resume_file)
        if fm and fm.get("source"):
            source_trans = CONNAISSANCE / fm["source"]
            source_fm = _lire_frontmatter(source_trans) if source_trans.exists() else None
            if source_fm and source_fm.get("source_path"):
                original = Path(source_fm["source_path"]).expanduser()
                if not original.exists():
                    problemes.append({
                        "fichier": str(resume_file.relative_to(CONNAISSANCE)),
                        "manquant": "original",
                        "attendu": str(original),
                    })

    return problemes


# --- Vérification 4 : Attachements manquants ---

def verifier_attachements() -> list[dict]:
    """Vérifier que les fichiers référencés dans Attachments/ existent."""
    problemes = []
    if not TRANSCRIPTIONS.exists():
        return problemes

    pattern = re.compile(r'(?:!\[.*?\]|)\(Attachments/([^)]+)\)')

    for md_file in TRANSCRIPTIONS.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(content):
            att_name = match.group(1)
            att_path = md_file.parent / "Attachments" / att_name
            if not att_path.exists():
                problemes.append({
                    "fichier": str(md_file.relative_to(CONNAISSANCE)),
                    "attachement": att_name,
                    "attendu": str(att_path.relative_to(CONNAISSANCE)),
                })

    return problemes


# --- Vérification 5 : Doublons courriels ---

def verifier_doublons() -> list[dict]:
    """Détecter les doublons de courriels par message-id.

    Un vrai doublon = plusieurs fichiers du MÊME file_type partageant le
    même message_id. Une transcription et son résumé correspondant ne
    comptent PAS comme doublons même s'ils partagent le même message_id
    (c'est le couplage normal).
    """
    problemes = []
    db = TrackingDB()

    # Scanner les message-ids dupliqués PAR file_type
    rows = db._conn.execute(
        """SELECT message_id, file_type, GROUP_CONCAT(path, '|') as paths, COUNT(*) as n
           FROM files
           WHERE message_id IS NOT NULL AND message_id != '' AND message_id != '<unknown>'
           GROUP BY message_id, file_type
           HAVING n > 1"""
    ).fetchall()

    for row in rows:
        problemes.append({
            "message_id": row["message_id"],
            "file_type": row["file_type"],
            "fichiers": row["paths"].split("|"),
            "count": row["n"],
        })

    db.close()
    return problemes


# --- API publique ---


_AUDIT_STEPS = {
    "liens_casses": verifier_liens_casses,
    "frontmatter_invalide": verifier_frontmatter,
    "triplets_desynchronises": verifier_triplets,
    "attachements_manquants": verifier_attachements,
    "doublons": verifier_doublons,
}


def check(steps: list[str] | None = None) -> dict:
    """Exécuter les vérifications d'intégrité (schema AuditResult).

    `steps` = sous-ensemble des 6 vérifications ou `["all"]`.
    """
    if not steps or "all" in steps:
        active = list(_AUDIT_STEPS.keys())
    else:
        active = [s for s in steps if s in _AUDIT_STEPS]

    checks: list[dict] = []
    total_issues = 0
    for name in active:
        issues = _AUDIT_STEPS[name]() or []
        status = "ok" if not issues else "issues"
        total_issues += len(issues)
        checks.append({
            "name": name,
            "status": status,
            "issues": issues,
        })

    return {
        "checks": checks,
        "status": "ok" if total_issues == 0 else "issues",
        "total_issues": total_issues,
    }


def reindex_db(dry_run: bool = False) -> dict:
    """Repopuler tracking.db depuis les fichiers existants (wrapper audit_reindex)."""
    from connaissance.commands import audit_reindex
    return audit_reindex.reindex(dry_run=dry_run)


def repair_attachments(dry_run: bool = False) -> dict:
    """Réparer les références d'attachements cassées (wrapper audit_attachments)."""
    from connaissance.commands import audit_attachments
    return audit_attachments.repair(dry_run=dry_run)


def archive_non_documents(dry_run: bool = False) -> dict:
    """Archiver les non-documents hors du périmètre (wrapper audit_archive)."""
    from connaissance.commands import audit_archive
    return audit_archive.archive(dry_run=dry_run)
