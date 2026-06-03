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
