"""Module commands/optimize : promotion PJ documents et déduplication.

Expose :
- `plan() -> OptimizePlan` : liste promotable + duplicates.
- `apply(dry_run=False) -> OptimizeApply` : applique promotion + dédup.
"""
from __future__ import annotations
import sys

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
    """Promouvoir les PJ documents vers ~/Documents/promus/.

    Hash paresseux : on ne hashe la PJ que s'il existe déjà une entrée en DB
    de même taille (collision possible). Sans collision, la PJ est unique —
    on la copie et on enregistre son hash à la volée.
    """
    items = scan_promotable()
    if not items:
        print("Aucune PJ document à promouvoir.", file=sys.stderr)

        return 0

    print(f"PJ documents à promouvoir : {len(items)}", file=sys.stderr)


    promoted = 0
    skipped = 0

    for item in items:
        src = item["path"]
        dest = PROMOTED_DIR / src.name

        # Préfiltre taille : pas de collision possible → pas de hash au scan.
        existing = None
        file_hash: str | None = None
        if db.has_size(item["size"]):
            file_hash = db.get_or_compute_hash(src)
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

        # Hash calculé à l'enregistrement (pour la dest), paresseux :
        # get_or_compute_hash bénéficie du cache (path, size, mtime) sur la
        # source si déjà hashée au préfiltre.
        if file_hash is None:
            file_hash = db.get_or_compute_hash(dest)
        if file_hash:
            try:
                st = dest.stat()
                db.register_hash(file_hash, str(dest),
                                 size=st.st_size, mtime=st.st_mtime)
            except OSError:
                db.register_hash(file_hash, str(dest), size=item["size"])
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

    Hash paresseux : on ne hashe une PJ que si une entrée DB partage sa
    taille (collision possible). Sans collision, aucun doublon n'est possible,
    pas de hash calculé.
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

                try:
                    size = f.stat().st_size
                except OSError:
                    continue

                # Préfiltre taille : pas de ligne DB de même taille → pas
                # de doublon possible, aucun hash à calculer.
                if not db.has_size(size):
                    continue

                file_hash = db.get_or_compute_hash(f)
                if not file_hash:
                    continue

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

    # Remonter récursivement les dossiers vides jusqu'à un parent non-vide
    # (sans dépasser TRANSCRIPTIONS).
    for d in dirs_touched:
        _prune_empty_upwards(d)

    print(f"\n  ✓ {removed} orphelins supprimés (~{freed // 1024} Ko)",
          file=sys.stderr)
    return removed, freed


def _prune_empty_upwards(path: Path) -> int:
    """Supprimer ``path`` puis ses parents tant qu'ils sont vides.

    S'arrête à ``TRANSCRIPTIONS`` (ne remonte jamais au-dessus). Retourne le
    nombre de dossiers supprimés.
    """
    removed = 0
    current = path
    while current != TRANSCRIPTIONS and current.is_dir():
        try:
            # any(iterdir()) compte aussi les .DS_Store — on les tolère
            # pour éviter de garder des dossiers "vides" en vrai.
            entries = [e for e in current.iterdir() if e.name != ".DS_Store"]
            if entries:
                return removed
            # Supprimer les .DS_Store qui ne font pas de mal
            for e in current.iterdir():
                try:
                    e.unlink()
                except OSError:
                    pass
            current.rmdir()
            removed += 1
            current = current.parent
        except OSError:
            return removed
    return removed


def remove_empty_dirs() -> int:
    """Balayer ``Transcriptions/`` pour supprimer tous les dossiers vides.

    Itère jusqu'à stabilisation (un dossier peut devenir vide après que ses
    enfants aient été supprimés). Tolère les ``.DS_Store`` — les supprime
    au passage pour ne pas garder de dossier "vide" en vrai.

    Retourne le nombre total de dossiers supprimés.
    """
    if not TRANSCRIPTIONS.exists():
        return 0
    total_removed = 0
    while True:
        removed_this_pass = 0
        # Parcours bottom-up (reversed dans rglob) pour supprimer les feuilles
        # avant leurs parents.
        dirs = [d for d in TRANSCRIPTIONS.rglob("*") if d.is_dir()]
        for d in sorted(dirs, key=lambda p: -len(p.parts)):
            try:
                entries = [e for e in d.iterdir() if e.name != ".DS_Store"]
                if entries:
                    continue
                for e in d.iterdir():
                    try:
                        e.unlink()
                    except OSError:
                        pass
                d.rmdir()
                removed_this_pass += 1
            except OSError:
                pass
        total_removed += removed_this_pass
        if removed_this_pass == 0:
            break
    return total_removed


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
        # Passe finale : supprimer tous les dossiers vides restants sous
        # Transcriptions/ (effet cumulé de promote + dedup + cleanup_orphans,
        # qui peuvent laisser des hiérarchies vidées).
        empty_dirs_removed = 0
        if not dry_run:
            empty_dirs_removed = remove_empty_dirs()
            if empty_dirs_removed:
                print(f"  ✓ {empty_dirs_removed} dossier(s) vide(s) supprimé(s)",
                      file=sys.stderr)
        return {
            "promoted": promoted,
            "deduped": deduped,
            "freed_bytes": freed + orphans_freed,
            "orphans_removed": orphans_removed,
            "empty_dirs_removed": empty_dirs_removed,
            "dry_run": dry_run,
        }
    finally:
        if owns_db:
            db.close()
