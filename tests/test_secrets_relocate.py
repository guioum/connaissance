"""Quarantaine physique : déplacement des secrets vers - Protégés/secrets/."""
import connaissance.core.filtres as F
import connaissance.core.ledger as Lmod
from connaissance.commands import secrets as S


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(S, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(S, "require_connaissance_root", lambda: None)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)
    monkeypatch.setattr(F, "SECRETS_QUARANTINE", tmp_path / "quar.txt")
    return docs


def test_relocate_dry_run_moves_nothing(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "creds.env").write_text("SECRET=x")
    F.write_quarantine_set({"creds.env"})
    res = S.relocate(dry_run=True, db=tracking_db)
    assert res["would_move"] == 1 and res["moved"] == 0
    assert (docs / "creds.env").exists()                 # rien bougé


def test_relocate_apply_moves_via_ledger(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "sub").mkdir()
    (docs / "sub" / "id_rsa").write_text("PRIVATE KEY")
    F.write_quarantine_set({"sub/id_rsa"})
    res = S.relocate(dry_run=False, db=tracking_db)
    assert res["moved"] == 1 and "ledger_run" in res
    assert not (docs / "sub" / "id_rsa").exists()
    moved = docs / "- Protégés/secrets/sub/id_rsa"
    assert moved.exists()
    # la liste de quarantaine pointe désormais le nouveau chemin
    q = F.load_quarantine_set()
    assert "- Protégés/secrets/sub/id_rsa" in q
    assert "sub/id_rsa" not in q


def test_relocate_skips_missing_and_already_protected(tmp_path, monkeypatch,
                                                      tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "- Protégés").mkdir()
    F.write_quarantine_set({"absent.pem", "- Protégés/secrets/x.pem"})
    res = S.relocate(dry_run=False, db=tracking_db)
    assert res["moved"] == 0
    assert any(s["reason"] == "introuvable" for s in res["skipped"])
