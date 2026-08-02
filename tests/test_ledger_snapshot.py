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


def test_snapshot_chain_survives_mixed_normalization(tmp_path, monkeypatch,
                                                     tracking_db):
    """Une chaîne dont les maillons mêlent NFC et NFD (ledger = chemins tels
    que fournis) doit quand même se résoudre — constaté en réel le 2026-07-25 :
    914 « introuvables » dans la vue snapshot après l'apply tranche 1."""
    import unicodedata

    import pytest

    # Le scénario suppose un filesystem insensible à la normalisation (APFS —
    # Mac natif comme VM cowork via VirtioFS). Sur ext4 (CI Linux), NFD et NFC
    # sont deux noms distincts : le mélange testé n'existe pas en prod là-bas.
    probe = tmp_path / unicodedata.normalize("NFD", "é.probe")
    probe.write_text("x", encoding="utf-8")
    if not (tmp_path / unicodedata.normalize("NFC", "é.probe")).exists():
        pytest.skip("filesystem sensible à la normalisation (ext4) — "
                    "scénario APFS uniquement")
    docs = _setup(tmp_path, monkeypatch)
    nfd = unicodedata.normalize("NFD", "Téléchargés")
    nfc = unicodedata.normalize("NFC", "Téléchargés")
    (docs / nfd).mkdir()
    f = docs / nfd / "doc.pdf"
    f.write_bytes(b"data")
    r1 = new_run_id("t")
    # move journalisé en NFD (chemin brut du walk)…
    safe_move(tracking_db, docs / nfd / "doc.pdf",
              docs / nfd / "doc2.pdf", "m1", r1)
    # …puis en NFC (chemin recomposé depuis une clé DB)
    safe_move(tracking_db, str(docs / nfc / "doc2.pdf"),
              docs / "final" / "doc.pdf", "m2", new_run_id("t"))
    entries = Lmod.snapshot_entries(tracking_db)
    origins = [e for e in entries if e["is_origin"]]
    assert len(origins) == 1
    assert origins[0]["terminal"].endswith("final/doc.pdf")
    assert origins[0]["exists"]


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
