"""Corbeille ledger (core/ledger.safe_trash + purge_run) : suppression
différée réversible, vidée seulement par purge."""
import connaissance.core.ledger as Lmod
from connaissance.core.ledger import (new_run_id, purge_run, revert_run,
                                       safe_trash)


def _trash_root(monkeypatch, tmp_path):
    """Rediriger CONNAISSANCE_ROOT du module ledger vers tmp (la corbeille y vit)."""
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)


def test_safe_trash_moves_to_trash_with_op(tracking_db, tmp_path, monkeypatch):
    _trash_root(monkeypatch, tmp_path)
    f = tmp_path / "Transcriptions" / "x" / "Attachments" / "dup.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF dup")
    run = new_run_id("optimize")
    e = safe_trash(tracking_db, f, "dedup", run)
    assert e["op"] == "trash" and e["status"] == "applied"
    assert not f.exists()                                    # déplacé
    trashed = tmp_path / ".trash" / run / "Transcriptions/x/Attachments/dup.pdf"
    assert trashed.exists()                                  # structure préservée
    assert len(tracking_db.ledger_trash_ops()) == 1


def test_trash_is_revertible(tracking_db, tmp_path, monkeypatch):
    _trash_root(monkeypatch, tmp_path)
    f = tmp_path / "Attachments" / "a.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data")
    run = new_run_id("optimize")
    safe_trash(tracking_db, f, "orphan", run)
    res = revert_run(tracking_db, run)
    assert res["reverted"] == 1
    assert f.exists()                                        # restauré à l'origine


def test_purge_dry_run_keeps_files(tracking_db, tmp_path, monkeypatch):
    _trash_root(monkeypatch, tmp_path)
    f = tmp_path / "Attachments" / "a.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data")
    run = new_run_id("optimize")
    safe_trash(tracking_db, f, "orphan", run)
    res = purge_run(tracking_db, run_id=run, dry_run=True)
    assert res["purged"] == 1 and res["dry_run"]
    trashed = tmp_path / ".trash" / run / "Attachments/a.pdf"
    assert trashed.exists()                                  # rien détruit
    assert len(tracking_db.ledger_trash_ops()) == 1         # toujours en attente


def test_purge_destroys_and_blocks_revert(tracking_db, tmp_path, monkeypatch):
    _trash_root(monkeypatch, tmp_path)
    f = tmp_path / "Attachments" / "a.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"data!!")
    run = new_run_id("optimize")
    safe_trash(tracking_db, f, "orphan", run)
    res = purge_run(tracking_db, run_id=run, dry_run=False)
    assert res["purged"] == 1 and res["freed_bytes"] == 6
    trashed = tmp_path / ".trash" / run / "Attachments/a.pdf"
    assert not trashed.exists()                              # détruit
    assert tracking_db.ledger_trash_ops() == []             # plus en attente
    # revert après purge ne restaure rien (statut 'purged' exclu du revert)
    rv = revert_run(tracking_db, run)
    assert rv["reverted"] == 0


def test_purge_older_than_filter(tracking_db, tmp_path, monkeypatch):
    _trash_root(monkeypatch, tmp_path)
    f = tmp_path / "Attachments" / "fresh.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    run = new_run_id("optimize")
    safe_trash(tracking_db, f, "orphan", run)
    # Une entrée fraîche n'est pas purgée par un filtre « > 30 jours ».
    res = purge_run(tracking_db, older_than_days=30, dry_run=False)
    assert res["purged"] == 0
    assert (tmp_path / ".trash" / run / "Attachments/fresh.pdf").exists()
