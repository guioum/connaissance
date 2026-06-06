"""Phase D — doublons de ~/Documents (commands/duplicates)."""
import connaissance.core.ledger as Lmod
from connaissance.commands import duplicates as D


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(D, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(D, "require_paths", lambda *a, **k: None)
    monkeypatch.setattr(D, "documents_read_path", lambda p: p)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)  # corbeille
    return docs


_LONG = ("le rapport annuel détaille les revenus les dépenses et le solde "
         "net de l'exercice avec un tableau récapitulatif par trimestre")


def _seed(db, docs, rel, content, summary_text):
    p = docs / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    packet = {"summary": {"sentences": [summary_text], "keywords": []}}
    db.get_or_compute_signals(p, rel, lambda _p: packet)


def test_scan_detects_exact_and_quasi(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    # exacts : mêmes octets
    _seed(tracking_db, docs, "a.pdf", b"IDENTIQUE", "texte a")
    _seed(tracking_db, docs, "sub/b.pdf", b"IDENTIQUE", "texte a")
    # quasi : octets différents, même résumé
    _seed(tracking_db, docs, "c.pdf", b"ccccc", _LONG)
    _seed(tracking_db, docs, "d.pdf", b"ddddd", _LONG)

    res = D.scan(db=tracking_db)
    assert res["scanned"] == 4
    assert len(res["exact_clusters"]) == 1
    assert set(res["exact_clusters"][0]["rels"]) == {"a.pdf", "sub/b.pdf"}
    assert len(res["quasi_clusters"]) == 1
    assert set(res["quasi_clusters"][0]["rels"]) == {"c.pdf", "d.pdf"}
    assert res["exact_duplicates"] == 1 and res["quasi_duplicates"] == 1


def test_plan_keeps_best_filed(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    _seed(tracking_db, docs, "a.pdf", b"X", "t")          # moins profond → keeper
    _seed(tracking_db, docs, "sub/b.pdf", b"X", "t")
    res = D.plan(db=tracking_db)
    assert res["total"] == 1
    e = res["entries"][0]
    assert e["keeper"] == "a.pdf" and e["trash"] == "sub/b.pdf"
    assert e["kind"] == "exact"


def test_apply_trashes_via_ledger(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    _seed(tracking_db, docs, "a.pdf", b"X", "t")
    _seed(tracking_db, docs, "sub/b.pdf", b"X", "t")
    import json
    mf = tmp_path / "m.json"
    mf.write_text(json.dumps({"entries": [
        {"trash": "sub/b.pdf", "keeper": "a.pdf", "kind": "exact"}]}))

    dry = D.apply(str(mf), dry_run=True, db=tracking_db)
    assert dry["would_trash"] == 1 and dry["trashed"] == 0
    assert (docs / "sub" / "b.pdf").exists()             # rien bougé

    res = D.apply(str(mf), dry_run=False, db=tracking_db)
    assert res["trashed"] == 1 and "ledger_run" in res
    assert (docs / "a.pdf").exists()                     # keeper intact
    assert not (docs / "sub" / "b.pdf").exists()         # doublon en corbeille
