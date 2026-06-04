"""Tests de la fiche d'identité DB (doc_classification + status + relink)."""
import json

from connaissance.commands import classify as CMD


def _files(tmp_path, prep, results):
    pf = tmp_path / "prep.json"
    pf.write_text(json.dumps(prep), encoding="utf-8")
    rf = tmp_path / "res.json"
    rf.write_text(json.dumps(results), encoding="utf-8")
    return str(rf), str(pf)


def test_register_persists_classification(tmp_path, monkeypatch, tracking_db):
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "cls_a", "_rel": "Classer/x/doc1.pdf",
                          "_hint": {}, "model": "claude-haiku-4-5-20251001"}]}
    results = {"results": [{"custom_id": "cls_a", "content":
        '{"entity":"Banque Nationale","entity_type":"organismes",'
        '"category":"banque","date":"2024-03-15","title":"Relevé",'
        '"confidence":"high"}'}]}
    rf, pf = _files(tmp_path, prep, results)
    CMD.register(rf, pf, db=tracking_db)

    rec = tracking_db.get_classification("Classer/x/doc1.pdf")
    assert rec and rec["status"] == "auto"
    assert rec["entity"] == "Banque Nationale" and rec["category"] == "banque"
    assert rec["entity_slug"] == "banque-nationale"
    assert rec["model"] == "claude-haiku-4-5-20251001"


def test_status_summary_and_card(monkeypatch, tracking_db):
    monkeypatch.setattr(CMD._filtres, "load_quarantine_set", lambda: set())
    tracking_db.upsert_classification("a/b.pdf", {
        "entity": "X", "entity_type": "organismes", "category": "banque",
        "status": "auto", "confidence": "high"})
    tracking_db.upsert_classification("c/d.pdf", {
        "entity": "Y", "entity_type": "divers", "status": "attente"})

    summary = CMD.status(db=tracking_db)
    assert summary["total"] == 2
    assert summary["by_status"] == {"auto": 1, "attente": 1}

    card = CMD.status(path="a/b.pdf", db=tracking_db)
    assert card["found"] and card["quarantined"] is False
    assert card["classification"]["entity"] == "X"


def test_apply_relinks_fiche_to_new_path(tmp_path, monkeypatch, tracking_db):
    root = tmp_path / "Documents"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    tracking_db.upsert_classification("src/a.pdf", {"entity": "X", "status": "auto"})

    mf = tmp_path / "m.json"
    mf.write_text(json.dumps({"entries": [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/x/2024-01-01 t.pdf", "category": "divers"}]}),
        encoding="utf-8")
    CMD.apply(str(mf), dry_run=False, db=tracking_db)

    assert tracking_db.get_classification("src/a.pdf") is None         # ancien parti
    moved = tracking_db.get_classification("organismes/x/2024-01-01 t.pdf")
    assert moved and moved["entity"] == "X"                            # fiche suivie
