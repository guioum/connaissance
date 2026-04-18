#!/usr/bin/env python3
"""Réindexer la base de connaissance dans `tracking.db` sans relancer OCR ni transcription.

Utilisé après une réinitialisation de la DB (corruption, reset volontaire)
pour ré-enregistrer tous les fichiers déjà présents sur le disque.

Scanne :
1. `~/Connaissance/Transcriptions/` → `file_type='transcription'`
2. `~/Connaissance/Résumés/`        → `file_type='resume'`
                                     (+ entity/source/message_id depuis frontmatter)
3. `~/Connaissance/Synthèse/`       → `file_type` ∈ {fiche, chronologie, moc, digest}
4. Hashes sources des transcriptions de documents (anti re-OCR) : itère sur les
   transcriptions existantes et lit le frontmatter canonique pour retrouver
   chaque source. Fallback glob sur la convention miroir si la transcription
   n'a pas encore de frontmatter (backfill au passage).

API publique : `reindex(dry_run, skip_hashes)`.
"""

from __future__ import annotations

import re
from pathlib import Path

from connaissance.core.paths import BASE_PATH
from connaissance.core.tracking import TrackingDB

try:
    from yaml import safe_load as _yaml_safe_load
except ImportError:
    _yaml_safe_load = None

CONNAISSANCE = BASE_PATH / "Connaissance"
DOCUMENTS = BASE_PATH / "Documents"
TRANSCRIPTIONS = CONNAISSANCE / "Transcriptions"
RESUMES = CONNAISSANCE / "Résumés"
SYNTHESE = CONNAISSANCE / "Synthèse"

SOURCE_TYPE_MAP = {
    "Documents": "document",
    "Courriels": "courriel",
    "Notes": "note",
}


def parse_frontmatter(md_text: str) -> dict:
    """Extraire le frontmatter YAML d'un .md. Retourne {} si absent ou cassé."""
    if not md_text.startswith("---"):
        return {}
    end = md_text.find("\n---", 4)
    if end < 0:
        return {}
    raw = md_text[4:end].lstrip("\n")
    if _yaml_safe_load is not None:
        try:
            data = _yaml_safe_load(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return _parse_frontmatter_regex(raw)


def _parse_frontmatter_regex(raw: str) -> dict:
    """Fallback minimal sans PyYAML : champs scalaires simples uniquement."""
    result: dict = {}
    for line in raw.splitlines():
        m = re.match(r"^([\w-]+)\s*:\s*(.+?)\s*$", line)
        if m:
            key = m.group(1)
            value = m.group(2).strip().strip("'\"")
            result[key] = value
    return result


def is_trackable_md(path: Path) -> bool:
    """Filtrer les .md : exclure _*.md et Attachments/."""
    if path.name.startswith("_"):
        return False
    if "Attachments" in path.parts:
        return False
    return True


def relpath(p: Path) -> str:
    """Chemin relatif à ~/Connaissance/ pour cohérence avec les autres modules."""
    try:
        return str(p.relative_to(CONNAISSANCE))
    except ValueError:
        return str(p)


def safe_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _fm_date(fm: dict, key: str) -> str | None:
    """Normaliser une valeur de date du frontmatter en chaîne stockable en DB.

    Les frontmatters mélangent des dates YAML parsées en ``datetime.date`` /
    ``datetime.datetime`` (sérialisées via ``isoformat()``) et des chaînes
    brutes (ex. ``"2026-04-01T21:01:13"``). On retourne la forme chaîne ;
    ``None`` si absent ou non reconnu.
    """
    value = fm.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    # datetime.datetime hérite de datetime.date, .isoformat() marche sur les deux
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def reindex_transcriptions(db: TrackingDB, dry_run: bool) -> dict:
    counts = {"document": 0, "courriel": 0, "note": 0, "total": 0}
    if not TRANSCRIPTIONS.exists():
        return counts

    for dir_name, source_type in SOURCE_TYPE_MAP.items():
        src_dir = TRANSCRIPTIONS / dir_name
        if not src_dir.exists():
            continue
        for f in src_dir.rglob("*.md"):
            if not is_trackable_md(f):
                continue
            counts[source_type] += 1
            counts["total"] += 1
            if dry_run:
                continue
            fm: dict = {}
            try:
                fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
            message_id = fm.get("message-id") or fm.get("message_id") if source_type == "courriel" else None
            db.register_file(
                relpath(f),
                file_type="transcription",
                source_type=source_type,
                message_id=message_id,
                created=_fm_date(fm, "created"),
                modified=_fm_date(fm, "modified"),
                mtime=safe_mtime(f),
            )
    return counts


def reindex_resumes(db: TrackingDB, dry_run: bool) -> dict:
    counts = {"total": 0, "avec_entite": 0, "avec_source": 0}
    if not RESUMES.exists():
        return counts

    for dir_name, source_type in SOURCE_TYPE_MAP.items():
        src_dir = RESUMES / dir_name
        if not src_dir.exists():
            continue
        for f in src_dir.rglob("*.md"):
            if not is_trackable_md(f):
                continue
            counts["total"] += 1
            fm: dict = {}
            try:
                fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
            entity_type = fm.get("entity_type") or None
            entity_slug = fm.get("entity_slug") or None
            source_rel = fm.get("source") or None
            message_id = fm.get("message-id") or fm.get("message_id") or None
            if entity_type and entity_slug:
                counts["avec_entite"] += 1
            if source_rel:
                counts["avec_source"] += 1
            if dry_run:
                continue
            db.register_file(
                relpath(f),
                file_type="resume",
                source_type=source_type,
                source_path=source_rel,
                entity_type=entity_type,
                entity_slug=entity_slug,
                message_id=message_id,
                created=_fm_date(fm, "created"),
                modified=_fm_date(fm, "modified"),
                mtime=safe_mtime(f),
            )
    return counts


def reindex_synthese(db: TrackingDB, dry_run: bool) -> dict:
    counts = {"fiche": 0, "chronologie": 0, "moc": 0, "digest": 0}
    if not SYNTHESE.exists():
        return counts

    # Fiches et chronologies (personnes/, organismes/, sujets/)
    for entity_type in ("personnes", "organismes", "sujets"):
        base_dir = SYNTHESE / entity_type
        if not base_dir.exists():
            continue

        # sujets/*.md sont des MOCs au niveau racine du dossier
        if entity_type == "sujets":
            for f in base_dir.glob("*.md"):
                if not is_trackable_md(f):
                    continue
                counts["moc"] += 1
                if dry_run:
                    continue
                db.register_file(
                    relpath(f),
                    file_type="moc",
                    mtime=safe_mtime(f),
                )
            continue

        # personnes/ et organismes/ ont une sous-arbo {slug}/{fiche,chronologie}.md
        for entity_dir in base_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            slug = entity_dir.name
            for name, file_type in (("fiche.md", "fiche"), ("chronologie.md", "chronologie")):
                f = entity_dir / name
                if not f.exists():
                    continue
                counts[file_type] += 1
                if dry_run:
                    continue
                db.register_file(
                    relpath(f),
                    file_type=file_type,
                    entity_type=entity_type,
                    entity_slug=slug,
                    mtime=safe_mtime(f),
                )

    # Digests (rapports/digests/*.md)
    digests_dir = SYNTHESE / "rapports" / "digests"
    if digests_dir.exists():
        for f in digests_dir.glob("*.md"):
            if not is_trackable_md(f):
                continue
            counts["digest"] += 1
            if dry_run:
                continue
            db.register_file(
                relpath(f),
                file_type="digest",
                mtime=safe_mtime(f),
            )

    return counts


OCR_SOURCE_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def _hash_file(path: Path) -> str | None:
    """Calculer le SHA256 d'un fichier."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _find_source_candidates(trans_path: Path) -> list[Path]:
    """Trouver les fichiers sources candidats pour une transcription via glob.

    Fallback pour les transcriptions sans frontmatter : reproduit la convention
    mirror Transcriptions/Documents/<rel>/<stem>.md → Documents/<rel>/<stem>.*
    """
    try:
        rel = trans_path.relative_to(TRANSCRIPTIONS / "Documents")
    except ValueError:
        return []
    source_dir = DOCUMENTS / rel.parent
    if not source_dir.exists():
        return []
    stem = trans_path.stem
    found = []
    for ext in OCR_SOURCE_EXTENSIONS:
        candidate = source_dir / f"{stem}{ext}"
        if candidate.exists():
            found.append(candidate)
    return found


def _parse_transcription_frontmatter(content: str) -> dict | None:
    """Extraire le frontmatter d'une transcription. None si absent ou cassé."""
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    raw = content[4:end].lstrip("\n")
    if _yaml_safe_load is None:
        return None
    try:
        data = _yaml_safe_load(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def reindex_document_hashes(db: TrackingDB, dry_run: bool) -> dict:
    """Repopuler l'index de hash depuis les transcriptions existantes.

    Stratégie :
    1. Purger les hashes existants (DELETE FROM files WHERE file_type='source')
       pour éliminer les parasites d'un scan précédent annulé.
    2. Itérer sur Transcriptions/Documents/**/*.md :
       a. Lire le frontmatter. Si présent avec source+source_hash : utiliser.
       b. Sinon : fallback glob sur la convention mirror, puis backfiller le
          frontmatter de la transcription au passage (idempotent).
    3. Appeler db.register_hash() pour chaque source identifiée.
    """
    counts = {
        "transcriptions_processed": 0,
        "from_frontmatter": 0,
        "backfilled": 0,
        "registered": 0,
        "orphans": 0,
    }

    trans_dir = TRANSCRIPTIONS / "Documents"
    if not trans_dir.exists():
        return counts

    # Purger les hashes parasites (tous les file_type='source')
    if not dry_run:
        db._conn.execute("DELETE FROM files WHERE file_type = 'source'")
        db._conn.commit()

    try:
        from connaissance.commands.documents import _upsert_transcription_frontmatter as _upsert
    except ImportError:
        _upsert = None

    for trans in sorted(trans_dir.rglob("*.md")):
        if not is_trackable_md(trans):
            continue
        counts["transcriptions_processed"] += 1

        try:
            content = trans.read_text(encoding="utf-8")
        except OSError:
            counts["orphans"] += 1
            continue

        fm = _parse_transcription_frontmatter(content)
        source_path: Path | None = None
        file_hash: str | None = None
        source_size: int | None = None

        if fm and fm.get("source") and fm.get("source_hash"):
            # Chemin rapide : tout est dans le frontmatter
            source_rel = str(fm["source"])
            candidate = BASE_PATH / source_rel
            hash_str = str(fm["source_hash"])
            # Accepter "sha256:abc..." ou "abc..."
            if hash_str.startswith("sha256:"):
                file_hash = hash_str[len("sha256:"):]
            else:
                file_hash = hash_str
            source_size = fm.get("source_size")
            if candidate.exists():
                source_path = candidate
                counts["from_frontmatter"] += 1
            else:
                # Frontmatter pointe vers une source qui n'existe plus
                counts["orphans"] += 1
                continue
        else:
            # Fallback glob : convention mirror
            candidates = _find_source_candidates(trans)
            if not candidates:
                counts["orphans"] += 1
                continue
            # Enregistrer le hash de chaque candidat (cas rare de stems
            # multi-extensions). Premier candidat sert aussi au backfill.
            for source in candidates:
                h = _hash_file(source)
                if not h:
                    continue
                if not dry_run:
                    try:
                        size = source.stat().st_size
                    except OSError:
                        size = 0
                    db.register_hash(h, str(source), size)
                counts["registered"] += 1
                if source_path is None:
                    source_path = source
                    file_hash = h
                    try:
                        source_size = source.stat().st_size
                    except OSError:
                        source_size = None

            # Backfiller le frontmatter de la transcription pour accélérer
            # les runs suivants. Idempotent.
            if not dry_run and _upsert is not None and source_path is not None:
                _upsert(trans, source_path, file_hash, source_size)
                counts["backfilled"] += 1
            continue

        # Chemin rapide : enregistrer le hash du source identifié via frontmatter
        if source_path is not None and file_hash and not dry_run:
            try:
                size = source_size if source_size is not None else source_path.stat().st_size
            except OSError:
                size = 0
            db.register_hash(file_hash, str(source_path), size)
            counts["registered"] += 1

    return counts


# --- API publique ---


def prune_orphans(db: TrackingDB, dry_run: bool) -> dict:
    """Supprimer de `files` les entrées dont le chemin n'existe plus sur disque.

    Sans ce nettoyage, les transcriptions/résumés supprimés du filesystem
    restent indexés en DB et faussent `pipeline_costs`, `summarize_plan`,
    `stale_synthesis`, etc. (ex. : une note supprimée continue d'être comptée
    comme « à résumer »). La table `operations` (log historique) est
    préservée — seules les références aux fichiers inexistants sont purgées.
    """
    counts = {"total": 0}
    rows = db._conn.execute("SELECT path, file_type FROM files").fetchall()
    orphans: list[str] = []
    for row in rows:
        path = row[0]
        if not (CONNAISSANCE / path).exists():
            orphans.append(path)
            ft = row[1] or "autre"
            counts[ft] = counts.get(ft, 0) + 1
            counts["total"] += 1
    if not dry_run and orphans:
        db._conn.executemany("DELETE FROM files WHERE path = ?",
                             [(p,) for p in orphans])
        db._conn.commit()
    return counts


def reindex(dry_run: bool = False, skip_hashes: bool = False,
            db: TrackingDB | None = None) -> dict:
    """Repopuler tracking.db depuis les fichiers existants (schema AuditReindex)."""
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        if not dry_run:
            db._conn.execute(
                """UPDATE files SET message_id = TRIM(message_id)
                   WHERE message_id IS NOT NULL AND message_id != TRIM(message_id)"""
            )
            db._conn.commit()

        trans_counts = reindex_transcriptions(db, dry_run)
        resume_counts = reindex_resumes(db, dry_run)
        synth_counts = reindex_synthese(db, dry_run)
        orphan_counts = prune_orphans(db, dry_run)
        hash_counts = None
        if not skip_hashes:
            hash_counts = reindex_document_hashes(db, dry_run)

        if not dry_run:
            db.log("connaissance", "reindex_base",
                   details={
                       "transcriptions": trans_counts["total"],
                       "resumes": resume_counts["total"],
                       "syntheses": sum(synth_counts.values()),
                       "orphans_pruned": orphan_counts["total"],
                       "skip_hashes": skip_hashes,
                   })

        return {
            "rescanned": trans_counts["total"] + resume_counts["total"] + sum(synth_counts.values()),
            "reinserted": trans_counts["total"] + resume_counts["total"] + sum(synth_counts.values()),
            "details": {
                "transcriptions": trans_counts,
                "resumes": resume_counts,
                "synthese": synth_counts,
                "orphans": orphan_counts,
                "hashes": hash_counts,
            },
            "dry_run": dry_run,
        }
    finally:
        if owns_db:
            db.close()
