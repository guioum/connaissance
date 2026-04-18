"""Module commands/optimize : promotion PJ documents et déduplication.

Expose :
- `plan() -> OptimizePlan` : liste promotable + duplicates.
- `apply(dry_run=False) -> OptimizeApply` : applique promotion + dédup.
"""
from __future__ import annotations
import sys

import hashlib
import json
import re
import shutil
from pathlib import Path

from connaissance.core.paths import BASE_PATH
from connaissance.core.tracking import TrackingDB

CONNAISSANCE = BASE_PATH / "Connaissance"
TRANSCRIPTIONS = CONNAISSANCE / "Transcriptions"
DOCUMENTS_DIR = BASE_PATH / "Documents"
PROMOTED_DIR = DOCUMENTS_DIR / "promus"

DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff", ".gif", ".bmp"}


def hash_file(path):
    """Calculer le SHA256 d'un fichier."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# --- Scan des PJ à promouvoir ---

def scan_promotable():
    """Trouver les PJ documents dans Attachments/ courriels/notes."""
    promotable = []

    for source in ("Courriels", "Notes"):
        base = TRANSCRIPTIONS / source
        if not base.exists():
            continue

        for att_dir in base.rglob("Attachments"):
            if not att_dir.is_dir():
                continue
            for f in sorted(att_dir.iterdir()):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext not in DOCUMENT_EXTENSIONS:
                    continue

                promotable.append({
                    "path": f,
                    "source": source.lower(),
                    "size": f.stat().st_size,
                    "ext": ext,
                })

    return promotable


def promote(db, dry_run=False):
    """Promouvoir les PJ documents vers ~/Documents/promus/."""
    items = scan_promotable()
    if not items:
        print("Aucune PJ document à promouvoir.", file=sys.stderr)

        return 0

    print(f"PJ documents à promouvoir : {len(items)}", file=sys.stderr)


    promoted = 0
    skipped = 0

    for item in items:
        src = item["path"]
        file_hash = hash_file(src)
        dest = PROMOTED_DIR / src.name

        # Vérifier si déjà dans la DB (document déjà connu par hash)
        existing = db.has_hash(file_hash) if file_hash else None
        if existing:
            print(f"  ○ {src.name} — déjà connu ({Path(existing).name})", file=sys.stderr)

            skipped += 1
            continue

        print(f"  → {src.name} → ~/Documents/promus/", file=sys.stderr)


        if dry_run:
            promoted += 1
            continue

        PROMOTED_DIR.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(str(src), str(dest))

        if file_hash:
            db.register_hash(file_hash, str(dest), item["size"])
        db.log("connaissance", "promote_attachment",
               source_type=item["source"],
               source_path=str(src),
               dest_path=str(dest),
               details={"hash": file_hash})
        promoted += 1

    print(f"\n  ✓ {promoted} promus, {skipped} déjà existants", file=sys.stderr)

    return promoted


# --- Déduplication ---

def scan_duplicates(db):
    """Trouver les doublons entre Attachments/ et les documents tracés dans la DB.

    Ne scanne PAS tout ~/Documents/ — compare uniquement les fichiers
    dans Attachments/ des transcriptions avec les hashes déjà en DB.
    """
    duplicates = []

    for source in ("Courriels", "Notes", "Documents"):
        base = TRANSCRIPTIONS / source
        if not base.exists():
            continue

        for att_dir in base.rglob("Attachments"):
            if not att_dir.is_dir():
                continue
            for f in sorted(att_dir.iterdir()):
                if not f.is_file():
                    continue

                file_hash = hash_file(f)
                if not file_hash:
                    continue

                # Vérifier si ce hash existe déjà comme document dans la DB
                existing = db.has_hash(file_hash)
                if existing and str(f) != existing:
                    duplicates.append({
                        "path": f,
                        "hash": file_hash,
                        "existing": existing,
                        "source": source.lower(),
                    })

    return duplicates


def _find_referencing_mds(att_path):
    """Trouver les .md qui référencent un fichier Attachments/."""
    filename = att_path.name
    parent = att_path.parent.parent
    results = []
    for md in parent.rglob("*.md"):
        if "Attachments" in str(md):
            continue
        try:
            content = md.read_text(encoding="utf-8")
            if filename in content:
                results.append(md)
        except OSError:
            continue
    return results


def dedup(db, dry_run=False):
    """Dédupliquer les fichiers identiques entre Attachments/ et documents."""
    duplicates = scan_duplicates(db)
    if not duplicates:
        print("Aucun doublon détecté.", file=sys.stderr)

        return 0

    print(f"Doublons détectés : {len(duplicates)}", file=sys.stderr)


    removed = 0
    updated = 0

    for dup in duplicates:
        dup_path = dup["path"]
        keeper = dup["existing"]
        print(f"  ✗ {dup_path.name} → doublon de {Path(keeper).name}", file=sys.stderr)


        if dry_run:
            removed += 1
            continue

        # Mettre à jour les .md qui référencent ce fichier
        referencing = _find_referencing_mds(dup_path)
        for md_path in referencing:
            try:
                content = md_path.read_text(encoding="utf-8")
                old_link = f"Attachments/{dup_path.name}"
                new_ref = f"{dup_path.stem}{dup_path.suffix} (SHA256: {dup['hash'][:12]}...) → voir {keeper}"
                content = content.replace(f"[{dup_path.stem}{dup_path.suffix}]({old_link})", new_ref)
                content = content.replace(f"({old_link})", f"— {new_ref}")
                md_path.write_text(content, encoding="utf-8")
                updated += 1
                db.log("connaissance", "dedup_reference",
                       source_path=str(md_path),
                       details={"hash": dup["hash"], "removed": str(dup_path), "keeper": keeper})
            except OSError:
                pass

        # Supprimer le doublon
        try:
            dup_path.unlink()
            removed += 1
            db.log("connaissance", "dedup_remove",
                   source_path=str(dup_path),
                   details={"hash": dup["hash"], "keeper": keeper})
        except OSError as e:
            print(f"    ⚠ Erreur suppression : {e}", file=sys.stderr)


    action = "à supprimer" if dry_run else "supprimés"
    print(f"\n  ✓ {removed} doublons {action}, {updated} transcriptions mises à jour", file=sys.stderr)

    return removed


# --- Scan des attachements orphelins (sans .md référent) ---

_ATT_REF_PATTERN = re.compile(r'\(\.?/?Attachments/([^)]+)\)')


def _attachments_referenced_in_md(md_path: Path) -> set[str]:
    """Noms de fichiers référencés sous la forme ``(Attachments/xxx)`` dans un .md."""
    try:
        content = md_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return {m.group(1) for m in _ATT_REF_PATTERN.finditer(content)}


def scan_orphan_attachments() -> list[dict]:
    """Lister les fichiers dans ``*/Attachments/`` qui ne sont référencés par
    aucun ``.md`` du même dossier parent.

    Héritage du bug historique où les transcriptions de documents étaient
    déplacées via ``organize_apply`` avec ``copy2`` de leurs Attachments —
    les fichiers source se retrouvaient sans .md frère pour les référencer.

    La nouvelle logique dans ``_move_with_attachments`` (uniforme ``move``)
    empêche ce cas à la génération. Cet outil nettoie l'état existant.
    """
    orphans: list[dict] = []
    if not TRANSCRIPTIONS.exists():
        return orphans

    for att_dir in TRANSCRIPTIONS.rglob("Attachments"):
        if not att_dir.is_dir():
            continue
        parent = att_dir.parent
        # Agréger toutes les références de tous les .md du même dossier parent
        all_refs: set[str] = set()
        for md in parent.glob("*.md"):
            all_refs |= _attachments_referenced_in_md(md)
        for f in sorted(att_dir.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.name in all_refs:
                continue
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            orphans.append({
                "path": f,
                "size": size,
                "dir": att_dir,
            })
    return orphans


def cleanup_orphans(db, dry_run=False) -> tuple[int, int]:
    """Supprimer les attachements orphelins (sans .md référent frère).

    Retourne ``(removed, freed_bytes)``.
    """
    orphans = scan_orphan_attachments()
    if not orphans:
        print("Aucun attachement orphelin.", file=sys.stderr)
        return 0, 0

    total_bytes = sum(o["size"] for o in orphans)
    print(
        f"Attachements orphelins : {len(orphans)} "
        f"(~{total_bytes // 1024} Ko)",
        file=sys.stderr,
    )

    if dry_run:
        for o in orphans[:10]:
            rel = o["path"].relative_to(CONNAISSANCE)
            print(f"  → supprimer {rel}", file=sys.stderr)
        if len(orphans) > 10:
            print(f"  … (+{len(orphans) - 10})", file=sys.stderr)
        return len(orphans), total_bytes

    removed = 0
    freed = 0
    # Grouper par dossier pour pouvoir supprimer les Attachments/ vides après
    dirs_touched: set[Path] = set()
    for o in orphans:
        try:
            o["path"].unlink()
            removed += 1
            freed += o["size"]
            dirs_touched.add(o["dir"])
            db.log("connaissance", "orphan_attachment_remove",
                   source_path=str(o["path"]),
                   details={"size": o["size"]})
        except OSError as e:
            print(f"  ⚠ {o['path'].name}: {e}", file=sys.stderr)

    # Supprimer les dossiers Attachments/ maintenant vides, et les dossiers
    # parents s'ils ne contiennent plus rien.
    for d in dirs_touched:
        try:
            if not any(d.iterdir()):
                d.rmdir()
                parent = d.parent
                if parent != TRANSCRIPTIONS and not any(parent.iterdir()):
                    parent.rmdir()
        except OSError:
            pass

    print(f"\n  ✓ {removed} orphelins supprimés (~{freed // 1024} Ko)",
          file=sys.stderr)
    return removed, freed


# --- API publique ---


def _serialize_entry(entry: dict) -> dict:
    """Convertir les Path en str pour JSON."""
    out = {}
    for k, v in entry.items():
        if isinstance(v, Path):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def plan(db: TrackingDB | None = None) -> dict:
    """Lister les PJ à promouvoir, les doublons et les orphelins (schema OptimizePlan)."""
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        promotable = scan_promotable()
        duplicates = scan_duplicates(db)
        orphans = scan_orphan_attachments()
        return {
            "promotable": [_serialize_entry(p) for p in promotable],
            "duplicates": [_serialize_entry(d) for d in duplicates],
            "orphan_attachments": [_serialize_entry(o) for o in orphans],
        }
    finally:
        if owns_db:
            db.close()


def apply(dry_run: bool = False, promote_docs: bool = True,
          dedup_attachments: bool = True, cleanup_orphans_flag: bool = True,
          db: TrackingDB | None = None) -> dict:
    """Appliquer promotion + déduplication + nettoyage orphelins (schema OptimizeApply)."""
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        promoted = 0
        deduped = 0
        freed = 0
        orphans_removed = 0
        orphans_freed = 0
        if promote_docs:
            promoted = promote(db, dry_run=dry_run) or 0
        if dedup_attachments:
            dedup_result = dedup(db, dry_run=dry_run)
            if isinstance(dedup_result, tuple):
                deduped, freed = dedup_result
            elif isinstance(dedup_result, int):
                deduped = dedup_result
        if cleanup_orphans_flag:
            orphans_removed, orphans_freed = cleanup_orphans(db, dry_run=dry_run)
        return {
            "promoted": promoted,
            "deduped": deduped,
            "freed_bytes": freed + orphans_freed,
            "orphans_removed": orphans_removed,
            "dry_run": dry_run,
        }
    finally:
        if owns_db:
            db.close()
