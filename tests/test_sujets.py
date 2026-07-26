"""Vue virtuelle des sujets (commands/sujets) : symlinks depuis doc_classification."""
from connaissance.commands import sujets as S


def _setup(tmp_path, monkeypatch, tracking_db):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(S, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(S, "require_paths", lambda *a, **k: None)
    return docs


def _add(db, rel, sujet, etype="organismes", eslug="x"):
    db.upsert_classification(rel, {"status": "auto", "sujet": sujet,
                                   "entity_type": etype, "entity_slug": eslug})


def test_is_junk_sujet():
    from connaissance.core.tracking import _is_junk_sujet
    for junk in ("2018", "2018-02", "20180101", "archive-2024-03-03-triage",
                 "non-organisées", "sans-tag", "material", "divers", "x"):
        assert _is_junk_sujet(junk), junk
    for ok in ("impots", "cuisine", "café-crème", "voyage-cancún"):
        assert not _is_junk_sujet(ok), ok


def test_sujet_memberships_resume_supersedes_classify(tmp_path, monkeypatch, tracking_db):
    # Un doc avec sujet classify (provisoire) ET resume (contenu) → seul resume.
    tracking_db.add_doc_sujets("a.pdf", ["archive-2024-triage", "impots-2024"], "classify")
    tracking_db.add_doc_sujets("a.pdf", ["impots"], "resume")
    # Un doc avec seulement classify, dont un bruit → seul le propre survit.
    tracking_db.add_doc_sujets("b.pdf", ["2018-02", "assurance"], "classify")
    by_rel = {}
    for m in tracking_db.sujet_memberships():
        by_rel.setdefault(m["rel_path"], set()).add(m["sujet"])
    assert by_rel["a.pdf"] == {"impots"}              # resume supersède classify
    assert by_rel["b.pdf"] == {"assurance"}           # bruit "2018-02" filtré


def test_view_dry_run_lists_without_writing(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch, tracking_db)
    (docs / "a.pdf").write_bytes(b"a")
    (docs / "b.pdf").write_bytes(b"b")
    _add(tracking_db, "a.pdf", "impots")
    _add(tracking_db, "b.pdf", "impots")
    res = S.view(apply=False, db=tracking_db)
    assert res["sujets"] == {"impots": 2} and res["total"] == 2
    assert res["links_created"] == 0
    # rien écrit en dry-run (la vue vit sous VIEWS_ROOT depuis v2.64.0)
    assert not (S.VIEWS_ROOT / S.SUJETS_VIEW_NAME).exists()


def test_view_apply_creates_symlinks(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch, tracking_db)
    (docs / "a.pdf").write_bytes(b"a")
    _add(tracking_db, "a.pdf", "impots 2024")
    res = S.view(apply=True, db=tracking_db)
    assert res["applied"] and res["links_created"] == 1
    view = S.VIEWS_ROOT / S.SUJETS_VIEW_NAME / "impots 2024"
    links = list(view.iterdir())
    assert len(links) == 1 and links[0].is_symlink()
    assert links[0].resolve() == (docs / "a.pdf").resolve()


def test_view_skips_missing_source(tmp_path, monkeypatch, tracking_db):
    _setup(tmp_path, monkeypatch, tracking_db)
    _add(tracking_db, "absent.pdf", "impots")           # fichier inexistant
    res = S.view(apply=True, db=tracking_db)
    assert res["missing_source"] == 1 and res["links_created"] == 0


def test_view_clear_removes_view(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch, tracking_db)
    (docs / "a.pdf").write_bytes(b"a")
    _add(tracking_db, "a.pdf", "impots")
    S.view(apply=True, db=tracking_db)
    assert (S.VIEWS_ROOT / S.SUJETS_VIEW_NAME).exists()
    res = S.view(clear=True, db=tracking_db)
    assert res["cleared"] and res["existed"]
    assert not (S.VIEWS_ROOT / S.SUJETS_VIEW_NAME).exists()


def test_list_sujets_counts(tmp_path, monkeypatch, tracking_db):
    _setup(tmp_path, monkeypatch, tracking_db)
    _add(tracking_db, "a.pdf", "impots")
    _add(tracking_db, "b.pdf", "impots")
    _add(tracking_db, "c.pdf", "banque")
    res = S.list_sujets(db=tracking_db)
    assert res["total_sujets"] == 2 and res["total_documents"] == 3
    assert list(res["sujets"]) == ["impots", "banque"]   # trié par count desc


def test_export_copies_documents(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch, tracking_db)
    (docs / "a.pdf").write_bytes(b"aaa")
    (docs / "b.pdf").write_bytes(b"bbb")
    _add(tracking_db, "a.pdf", "comptable")
    _add(tracking_db, "b.pdf", "comptable")
    dest = tmp_path / "out"
    res = S.export("comptable", dest=str(dest), db=tracking_db)
    assert res["exported"] == 2 and not res["zip"]
    assert {p.name for p in dest.iterdir()} == {"a.pdf", "b.pdf"}
    # les sources ne bougent pas (copie)
    assert (docs / "a.pdf").exists() and (docs / "b.pdf").exists()


def test_export_zip(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch, tracking_db)
    (docs / "a.pdf").write_bytes(b"aaa")
    _add(tracking_db, "a.pdf", "comptable")
    res = S.export("comptable", dest=str(tmp_path / "arch"), as_zip=True,
                   db=tracking_db)
    assert res["zip"] and res["exported"] == 1
    assert res["dest"].endswith(".zip")
    from pathlib import Path
    assert Path(res["dest"]).exists()


def test_symlink_avec_mtime_estampille_le_lien(tmp_path):
    """Le symlink d'une vue porte le mtime de sa cible, pas « maintenant »
    (sinon un tri par date dans le Finder date tout au dernier refresh)."""
    import os
    import time
    from connaissance.core.paths import symlink_avec_mtime
    cible = tmp_path / "doc.pdf"
    cible.write_bytes(b"%PDF")
    vieux = time.time() - 86400 * 365
    os.utime(cible, (vieux, vieux))
    lien = tmp_path / "lien.pdf"
    symlink_avec_mtime(lien, cible)
    assert abs(lien.lstat().st_mtime - vieux) < 2
    # cible absente : pas d'exception, le lien existe quand même
    fantome = tmp_path / "fantome.pdf"
    symlink_avec_mtime(fantome, tmp_path / "absent.pdf")
    assert fantome.is_symlink()
