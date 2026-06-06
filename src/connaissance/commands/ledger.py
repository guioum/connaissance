"""Module commands/ledger : consultation et rollback du journal d'opérations.

Le ledger enregistre toute modification de nom/dossier de fichier (via
``core.ledger.safe_move``). Ces verbes permettent de le consulter et de revenir
en arrière de façon vérifiée (par hash).
"""
from connaissance.core import ledger as _ledger
from connaissance.core.tracking import TrackingDB


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
