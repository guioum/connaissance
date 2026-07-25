"""Diff de photos : appariement par hash quand disponible, sinon rel NFC
résolu via le ledger de la photo B — les photos historiques ont `hash` NULL
(jamais alimenté avant 2026-07-25, le diff renvoyait des zéros systématiques).
"""
import unicodedata

from connaissance.commands import snapshots as CMD
from connaissance.core import tracking


def _photo(tmp_path, monkeypatch, name, rows, ledger_ops=()):
    """Créer une photo synthétique (schéma TrackingDB réel) sous SNAPSHOTS_DIR."""
    CMD.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tracking, "require_connaissance_root", lambda: None)
    db = tracking.TrackingDB(db_path=CMD.SNAPSHOTS_DIR / f"{name}.db")
    for rel, extra in rows:
        db.upsert_classification(rel, {"status": "auto", **extra})
    for old, new in ledger_ops:
        db.ledger_record({"run_id": "r1", "op": "move", "old_path": old,
                          "new_path": new, "sha256": None, "size": 1,
                          "mtime": 1.0, "reason": "t", "status": "applied"})
    db.close()


def test_diff_matches_by_rel_and_ledger_chain(tmp_path, monkeypatch,
                                              tracking_db):
    """Sans hash (photos historiques) : un doc déplacé est apparié via la
    chaîne du ledger de B — y compris quand le ledger a journalisé le chemin
    en NFD (walk APFS) et la fiche en NFC."""
    docs = tmp_path / "Documents"
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docs)
    nfd = unicodedata.normalize("NFD", "Téléchargements")

    _photo(tmp_path, monkeypatch, "avant", rows=[
        ("Classer/vrac/a.pdf", {"entity_slug": None, "category": None}),
        ("Classer/vrac/b.pdf", {"entity_slug": None, "category": None}),
        (f"{nfd}/c.pdf", {"entity_slug": None, "category": None}),
    ])
    _photo(tmp_path, monkeypatch, "apres", rows=[
        # a déplacé + classé ; b resté ; c déplacé (ledger NFD) ; d nouveau
        ("organismes/bn/2024-01-01 a.pdf",
         {"entity_slug": "bn", "category": "banque"}),
        ("Classer/vrac/b.pdf", {"entity_slug": None, "category": None}),
        ("organismes/x/2020-01-01 c.pdf",
         {"entity_slug": "x", "category": "divers"}),
        ("organismes/bn/2024-02-02 d.pdf",
         {"entity_slug": "bn", "category": "banque"}),
    ], ledger_ops=[
        (str(docs / "Classer/vrac/a.pdf"),
         str(docs / "organismes/bn/2024-01-01 a.pdf")),
        (str(docs / nfd / "c.pdf"),
         str(docs / "organismes/x/2020-01-01 c.pdf")),
    ])

    out = CMD.diff("avant", "apres")
    assert out["moved"] == 2          # a et c suivis via la chaîne (NFD inclus)
    assert out["reclassified"] == 2   # a et c ont gagné entité/catégorie
    assert out["added"] == 1          # d
    assert out["removed"] == 0
    tos = {m["to"] for m in out["sample"]["moved"]}
    assert "organismes/bn/2024-01-01 a.pdf" in tos
    assert "organismes/x/2020-01-01 c.pdf" in tos


def test_diff_prefers_hash_when_present(tmp_path, monkeypatch, tracking_db):
    """Avec hash des deux côtés (photos futures, estampillé par relocate) :
    l'appariement se fait par contenu, sans besoin du ledger."""
    docs = tmp_path / "Documents"
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docs)
    _photo(tmp_path, monkeypatch, "h-avant", rows=[
        ("vrac/a.pdf", {"hash": "sha-AAA", "entity_slug": None,
                        "category": None}),
    ])
    _photo(tmp_path, monkeypatch, "h-apres", rows=[
        ("organismes/bn/a.pdf", {"hash": "sha-AAA", "entity_slug": "bn",
                                 "category": "banque"}),
    ])
    out = CMD.diff("h-avant", "h-apres")
    assert out["moved"] == 1 and out["removed"] == 0 and out["added"] == 0
