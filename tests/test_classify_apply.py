"""Tests de la brique 5 Phase C (commands/classify.apply)."""
import json

from connaissance.commands import classify as CMD


def _manifest(tmp_path, entries):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return str(p)


def test_apply_dry_run_moves_nothing(tmp_path, monkeypatch, tracking_db):
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    f = root / "src" / "a.pdf"
    f.write_bytes(b"%PDF data")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/x/2024-01-01 titre.pdf", "category": "banque"},
        {"status": "attente", "source": "src/b.pdf", "dest": None},
    ])
    res = CMD.apply(mf, db=tracking_db)        # dry_run par défaut
    assert res["dry_run"] and res["moved"] == 0
    assert res["planned"] == 1 and res["attente"] == 1
    assert f.exists()                          # rien n'a bougé


def test_apply_executes_and_moves_via_ledger(tmp_path, monkeypatch, tracking_db):
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    f = root / "src" / "a.pdf"
    f.write_bytes(b"%PDF data")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/banque-nationale/2024-01-01 releve.pdf",
         "category": "banque"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 1 and "ledger_run" in res
    assert not f.exists()
    assert (root / "organismes/banque-nationale/2024-01-01 releve.pdf").exists()


def test_apply_handles_name_collision(tmp_path, monkeypatch, tracking_db):
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.pdf").write_bytes(b"x")
    # cible déjà occupée
    dest = root / "organismes/x"
    dest.mkdir(parents=True)
    (dest / "2024-01-01 titre.pdf").write_bytes(b"existant")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/x/2024-01-01 titre.pdf", "category": "divers"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 1
    assert (dest / "2024-01-01 titre (2).pdf").exists()   # uniquifié
    assert (dest / "2024-01-01 titre.pdf").read_bytes() == b"existant"  # intact


def test_apply_ledger_and_relink_committed_together(tmp_path, monkeypatch,
                                                    tracking_db):
    """Succès : la ligne ledger ET le relink de la fiche sont persistés."""
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.pdf").write_bytes(b"%PDF data")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    # Une fiche existante sur l'ancien rel_path doit suivre le move.
    tracking_db.upsert_classification(
        "src/a.pdf", {"status": "auto", "category": "banque"})
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/bn/2024-01-01 releve.pdf", "category": "banque"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 1
    n = tracking_db._conn.execute("SELECT COUNT(*) FROM file_ledger").fetchone()[0]
    assert n == 1                                          # ledger journalisé
    assert tracking_db.get_classification("src/a.pdf") is None       # ancien parti
    assert tracking_db.get_classification(
        "organismes/bn/2024-01-01 releve.pdf") is not None          # fiche suivie


def test_apply_atomic_rollback_on_relink_failure(tmp_path, monkeypatch,
                                                 tracking_db):
    """Si le relink échoue, l'insertion ledger est annulée avec lui : jamais
    une fiche désynchronisée d'un ledger à moitié écrit (atomicité #4)."""
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.pdf").write_bytes(b"%PDF data")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)

    def boom(*a, **k):
        raise OSError("relink simulé KO")
    monkeypatch.setattr(tracking_db, "relink_document", boom)

    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/x/2024-01-01 t.pdf", "category": "divers"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 0 and len(res["errors"]) == 1
    # Le move FS a bien eu lieu (non transactionnel)…
    assert (root / "organismes/x/2024-01-01 t.pdf").exists()
    # …mais AUCUNE ligne ledger : ledger_record a été rollback avec le relink.
    n = tracking_db._conn.execute("SELECT COUNT(*) FROM file_ledger").fetchone()[0]
    assert n == 0


def test_apply_skips_missing_source(tmp_path, monkeypatch, tracking_db):
    root = tmp_path / "Documents"
    root.mkdir()
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/absent.pdf",
         "dest": "organismes/x/t.pdf", "category": "divers"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 0 and len(res["skipped"]) == 1
    assert res["skipped"][0]["reason"] == "source_introuvable"
