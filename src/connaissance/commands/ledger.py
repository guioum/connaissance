"""Module commands/ledger : consultation et rollback du journal d'opérations.

Le ledger enregistre toute modification de nom/dossier de fichier (via
``core.ledger.safe_move``). Ces verbes permettent de le consulter et de revenir
en arrière de façon vérifiée (par hash).
"""
import os
import shutil
from pathlib import Path
from typing import cast

from connaissance.core import ledger as _ledger
from connaissance.core.paths import DOCUMENTS_DIR, VIEWS_ROOT
from connaissance.core.schemas import (LedgerPurge, LedgerRevert, LedgerRun,
                                       LedgerRuns, LedgerShow, LedgerSnapshot,
                                       LedgerVerify)
from connaissance.core.tracking import TrackingDB

SNAPSHOT_VIEW = "Historique"   # sous VIEWS_ROOT (hors ~/Documents/iCloud)


def list_runs(limit: int = 20) -> LedgerRuns:
    """Lister les runs récents (schema LedgerRuns)."""
    with TrackingDB() as db:
        return {"runs": cast(list[LedgerRun], db.ledger_runs(limit))}


def show(run_id: str) -> LedgerShow:
    """Détail des opérations d'un run (schema LedgerShow)."""
    with TrackingDB() as db:
        return {"run_id": run_id, "operations": db.ledger_ops(run_id)}


def revert(run_id: str, dry_run: bool = False) -> LedgerRevert:
    """Annuler un run (rollback vérifié par hash) (schema LedgerRevert)."""
    with TrackingDB() as db:
        return _ledger.revert_run(db, run_id, dry_run=dry_run)


def verify(run_id: str) -> LedgerVerify:
    """Vérifier la cohérence ledger ↔ disque d'un run (schema LedgerVerify)."""
    with TrackingDB() as db:
        return _ledger.verify_run(db, run_id)


def snapshot(run_id: str | None = None, apply: bool = False,
             clear: bool = False, db: TrackingDB | None = None) -> LedgerSnapshot:
    """Vue ``- Historique`` : snapshots datés de l'arborescence AVANT
    déplacements (schema LedgerSnapshot).

    Un sous-dossier **par jour** (``AAAA-MM-JJ``) reconstruit les **anciens
    chemins d'origine** (sous ~/Documents) en **symlinks** pointant l'emplacement
    **actuel** du fichier (chaîne old→new suivie). Seules les **origines** sont
    incluses (pas les chemins intermédiaires d'une chaîne). Lecture seule,
    régénérable ; fichier disparu (corbeille purgée) → marqueur ``.disparu``.

    - défaut : **dry-run** (compteurs) ; ``apply`` (re)construit ; ``clear`` supprime.
    """
    view = VIEWS_ROOT / SNAPSHOT_VIEW
    if clear:
        existed = view.exists()
        if existed:
            shutil.rmtree(view)
        return {"cleared": True, "existed": existed, "view_dir": str(view)}

    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        ents = _ledger.snapshot_entries(db, run_id=run_id)
    finally:
        if owns:
            db.close()

    docs = str(DOCUMENTS_DIR)
    sel = []
    for e in ents:
        old = e["old_path"]
        if not e.get("is_origin"):
            continue                                   # exclure les intermédiaires
        if not old.startswith(docs + os.sep):
            continue                                   # hors ~/Documents
        rel = Path(old).relative_to(DOCUMENTS_DIR)
        if str(rel).startswith("-"):
            continue                                   # ne pas snapshot les vues
        day = (e.get("timestamp") or "")[:10] or "sans-date"
        sel.append((day, rel, e))

    days = {d for d, _, _ in sel}
    gone = sum(1 for _, _, e in sel if not e["exists"])
    linked = 0
    if apply:
        if view.exists():
            shutil.rmtree(view)
        for day, rel, e in sel:
            dest = view / day / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                continue
            if e["exists"]:
                dest.symlink_to(Path(e["terminal"]))
                linked += 1
            else:
                dest.with_name(dest.name + ".disparu").write_text(
                    "", encoding="utf-8")

    return {
        "days": len(days),
        "entries": len(sel),
        "linked": linked if apply else 0,
        "would_link": len(sel) - gone if not apply else 0,
        "gone": gone,
        "applied": apply,
        "view_dir": str(view),
    }


def purge(run_id: str | None = None, older_than_days: int | None = None,
          dry_run: bool = True) -> LedgerPurge:
    """Vider la corbeille ledger (schema LedgerPurge).

    Suppression **définitive** des fichiers en corbeille (``op='trash'``),
    filtrable par ``run_id`` et/ou ``older_than_days``. **Dry-run par défaut** :
    passer ``dry_run=False`` (``--apply``) pour détruire réellement.
    """
    with TrackingDB() as db:
        return _ledger.purge_run(db, run_id=run_id,
                                 older_than_days=older_than_days, dry_run=dry_run)
