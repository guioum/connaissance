"""Vue - Historique : snapshots datés en symlinks (chaîne de déplacements)."""
import connaissance.core.ledger as Lmod
from connaissance.commands import ledger as CL
from connaissance.core.ledger import new_run_id, safe_move


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(CL, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)
    return docs


def test_snapshot_chains_to_current_location(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    # fichier déplacé DEUX fois (chaîne) : a → b → c
    (docs / "vieux").mkdir()
    f = docs / "vieux" / "doc.pdf"
    f.write_bytes(b"data")
    r1 = new_run_id("organize")
    safe_move(tracking_db, f, docs / "milieu" / "doc.pdf", "move1", r1)
    r2 = new_run_id("organize")
    safe_move(tracking_db, docs / "milieu" / "doc.pdf",
              docs / "final" / "doc.pdf", "move2", r2)

    res = CL.snapshot(apply=True, db=tracking_db)
    assert res["linked"] == 1          # SEULE l'origine (a), pas l'intermédiaire (b)
    # la vue vit sous VIEWS_ROOT depuis v2.64.0 (hors ~/Documents)
    view = CL.VIEWS_ROOT / CL.SNAPSHOT_VIEW
    links = [l for l in view.rglob("doc.pdf") if l.is_symlink()]
    assert len(links) == 1
    assert "vieux" in links[0].parts                 # à l'emplacement d'ORIGINE
    assert links[0].resolve() == (docs / "final" / "doc.pdf").resolve()


def test_snapshot_dry_run_and_clear(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "a").mkdir()
    (docs / "a" / "x.pdf").write_bytes(b"x")
    safe_move(tracking_db, docs / "a" / "x.pdf", docs / "b" / "x.pdf",
              "m", new_run_id("organize"))
    view = CL.VIEWS_ROOT / CL.SNAPSHOT_VIEW
    dry = CL.snapshot(apply=False, db=tracking_db)
    assert dry["would_link"] >= 1 and not view.exists()
    CL.snapshot(apply=True, db=tracking_db)
    assert view.exists()
    res = CL.snapshot(clear=True, db=tracking_db)
    assert res["cleared"] and not view.exists()


def test_snapshot_gone_file_marker(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "a").mkdir()
    (docs / "a" / "x.pdf").write_bytes(b"x")
    safe_move(tracking_db, docs / "a" / "x.pdf", docs / "b" / "x.pdf",
              "m", new_run_id("organize"))
    (docs / "b" / "x.pdf").unlink()           # simuler fichier disparu (purgé)
    res = CL.snapshot(apply=True, db=tracking_db)
    assert res["gone"] >= 1
    markers = list((CL.VIEWS_ROOT / CL.SNAPSHOT_VIEW).rglob("*.disparu"))
    assert markers
