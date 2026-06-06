"""Module commands/ledger : consultation et rollback du journal d'opérations.

Le ledger enregistre toute modification de nom/dossier de fichier (via
``core.ledger.safe_move``). Ces verbes permettent de le consulter et de revenir
en arrière de façon vérifiée (par hash).
"""
import os
import re
import shutil
import unicodedata
from pathlib import Path

from connaissance.core import ledger as _ledger
from connaissance.core.paths import DOCUMENTS_DIR
from connaissance.core.tracking import TrackingDB

SNAPSHOT_VIEW = "- Historique"


def _folder_name(stamp: str, reason: str, run_id: str) -> str:
    """Nom de dossier daté et lisible pour un run, sans collision."""
    date = (stamp or "").replace("T", " ")[:16]            # AAAA-MM-JJ HH:MM
    rsn = unicodedata.normalize("NFC", reason or "run").strip()[:40]
    rsn = re.sub(r"[/\\:]+", "-", rsn)
    return f"{date} {rsn} [{run_id[-6:]}]".strip()


def list_runs(limit: int = 20) -> dict:
    """Lister les runs récents (schema LedgerRuns)."""
    with TrackingDB() as db:
        return {"runs": db.ledger_runs(limit)}


def show(run_id: str) -> dict:
    """Détail des opérations d'un run (schema LedgerShow)."""
    with TrackingDB() as db:
        return {"run_id": run_id, "operations": db.ledger_ops(run_id)}


def revert(run_id: str, dry_run: bool = False) -> dict:
    """Annuler un run (rollback vérifié par hash) (schema LedgerRevert)."""
    with TrackingDB() as db:
        return _ledger.revert_run(db, run_id, dry_run=dry_run)


def verify(run_id: str) -> dict:
    """Vérifier la cohérence ledger ↔ disque d'un run (schema LedgerVerify)."""
    with TrackingDB() as db:
        return _ledger.verify_run(db, run_id)


def snapshot(run_id: str | None = None, apply: bool = False,
             clear: bool = False, db: TrackingDB | None = None) -> dict:
    """Vue ``- Historique`` : snapshots datés de l'arborescence AVANT
    déplacements (schema LedgerSnapshot).

    Pour chaque run du ledger, un sous-dossier daté reconstruit les **anciens
    chemins** (sous ~/Documents) en **symlinks** pointant l'emplacement
    **actuel** du fichier (chaîne old→new suivie). Lecture seule, régénérable.
    Les fichiers disparus (corbeille purgée) → marqueur ``.disparu``.

    - défaut : **dry-run** (compteurs) ; ``apply`` (re)construit ; ``clear`` supprime.
    """
    view = DOCUMENTS_DIR / SNAPSHOT_VIEW
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
        if not old.startswith(docs + os.sep):
            continue                                   # hors ~/Documents
        rel = Path(old).relative_to(DOCUMENTS_DIR)
        if str(rel).startswith("-"):
            continue                                   # ne pas snapshot les vues
        sel.append((rel, e))

    runs = {e["run_id"] for _, e in sel}
    gone = sum(1 for _, e in sel if not e["exists"])
    linked = 0
    if apply:
        if view.exists():
            shutil.rmtree(view)
        for rel, e in sel:
            folder = _folder_name(e["timestamp"], e.get("reason"), e["run_id"])
            dest = view / folder / rel
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
        "runs": len(runs),
        "entries": len(sel),
        "linked": linked if apply else 0,
        "would_link": len(sel) - gone if not apply else 0,
        "gone": gone,
        "applied": apply,
        "view_dir": str(view),
    }


def purge(run_id: str | None = None, older_than_days: int | None = None,
          dry_run: bool = True) -> dict:
    """Vider la corbeille ledger (schema LedgerPurge).

    Suppression **définitive** des fichiers en corbeille (``op='trash'``),
    filtrable par ``run_id`` et/ou ``older_than_days``. **Dry-run par défaut** :
    passer ``dry_run=False`` (``--apply``) pour détruire réellement.
    """
    with TrackingDB() as db:
        return _ledger.purge_run(db, run_id=run_id,
                                 older_than_days=older_than_days, dry_run=dry_run)
