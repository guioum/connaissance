"""Primitive relocate_document : déplace le graphe complet + met à jour les refs."""
import connaissance.core.ledger as Lmod
import connaissance.core.relocate as R
from connaissance.core.relocate import relocate_document, _read_fm


def test_relocate_uniquifies_source_collision(tmp_path, monkeypatch,
                                              tracking_db):
    """Fusion d'entités : la destination SOURCE peut déjà être occupée par un
    autre fichier du même nom → uniquifier (et le graphe suit le rel « (2) »),
    ne jamais refuser (constaté en réel : 8 fusions plantées en plein dossier,
    registre déjà consolidé)."""
    docs, tr, res, croot = _setup(tmp_path, monkeypatch)
    (docs / "organismes" / "azur").mkdir(parents=True)
    (docs / "organismes" / "azur" / "paie.pdf").write_bytes(b"garde")
    (docs / "organismes" / "azur-inc").mkdir(parents=True)
    (docs / "organismes" / "azur-inc" / "paie.pdf").write_bytes(b"perdant")
    (tr / "organismes" / "azur-inc").mkdir(parents=True)
    (tr / "organismes" / "azur-inc" / "paie.md").write_text("tr perdant",
                                                            encoding="utf-8")
    out = relocate_document(tracking_db, "organismes/azur-inc/paie.pdf",
                            "organismes/azur/paie.pdf",
                            Lmod.new_run_id("test"))
    assert out["new"] == "organismes/azur/paie (2).pdf"
    assert (docs / "organismes" / "azur" / "paie.pdf").read_bytes() == b"garde"
    assert (docs / "organismes" / "azur" / "paie (2).pdf").read_bytes() == b"perdant"
    # le miroir suit le rel uniquifié
    assert (tr / "organismes" / "azur" / "paie (2).md").exists()


def test_relocate_uniquifies_mirror_collision(tmp_path, monkeypatch,
                                              tracking_db):
    """Deux sources d'extensions différentes (scan .jpg + .pdf du même doc)
    visant le même stem → leurs transcriptions partagent le même `.md`. La
    cible miroir doit être uniquifiée, pas refusée (constaté en réel tranche 2 :
    3 sources parties, transcriptions orphelines)."""
    docs, tr, res, croot = _setup(tmp_path, monkeypatch)
    # doc A déjà en place avec sa transcription
    (tr / "organismes" / "caa").mkdir(parents=True)
    (tr / "organismes" / "caa" / "2014-08-15 carte.md").write_text(
        "transcription A", encoding="utf-8")
    # doc B (autre extension, même stem cible) + sa transcription au miroir vrac
    (docs / "vrac").mkdir(parents=True)
    (docs / "vrac" / "scan.pdf").write_bytes(b"%PDF B")
    (tr / "vrac").mkdir(parents=True)
    (tr / "vrac" / "scan.md").write_text("transcription B", encoding="utf-8")

    out = relocate_document(tracking_db, "vrac/scan.pdf",
                            "organismes/caa/2014-08-15 carte.pdf",
                            Lmod.new_run_id("test"))
    assert "transcription" in out["moved"]
    # A intacte, B uniquifiée à côté
    assert (tr / "organismes" / "caa" / "2014-08-15 carte.md").read_text(
        encoding="utf-8") == "transcription A"
    assert (tr / "organismes" / "caa" / "2014-08-15 carte (2).md").read_text(
        encoding="utf-8") == "transcription B"


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    tr = tmp_path / "Connaissance" / "Transcriptions" / "Documents"
    res = tmp_path / "Connaissance" / "Résumés" / "Documents"
    croot = tmp_path / "Connaissance"
    for p in (docs, tr, res):
        p.mkdir(parents=True)
    monkeypatch.setattr(R, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(R, "TRANSCR", tr)
    monkeypatch.setattr(R, "RESUMES", res)
    monkeypatch.setattr(R, "CONNAISSANCE_ROOT", croot)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", croot)
    return docs, tr, res, croot


def test_relocate_moves_full_graph_and_updates_refs(tmp_path, monkeypatch,
                                                    tracking_db):
    docs, tr, res, croot = _setup(tmp_path, monkeypatch)
    # triplet : source + résumé (newslug) + transcription ORPHELINE (oldslug)
    (docs / "organismes" / "bn").mkdir(parents=True)
    (docs / "organismes" / "bn" / "x-developpement.pdf").write_bytes(b"%PDF")
    (res / "organismes" / "bn").mkdir(parents=True)
    (res / "organismes" / "bn" / "x-developpement.md").write_text(
        "---\nsource: Transcriptions/Documents/organismes/vieux/x-developpement.md\n"
        "title: X développement\n---\nrésumé", encoding="utf-8")
    (tr / "organismes" / "vieux").mkdir(parents=True)         # orpheline
    (tr / "organismes" / "vieux" / "x-developpement.md").write_text(
        "transcription", encoding="utf-8")
    tracking_db.upsert_classification(
        "organismes/bn/x-developpement.pdf",
        {"status": "auto", "entity_slug": "bn", "entity_type": "organismes"})
    tracking_db._conn.execute(
        "INSERT INTO text_simhash (rel_path, simhash) VALUES (?, ?)",
        ("Transcriptions/Documents/organismes/vieux/x-developpement.md", "abc"))
    tracking_db._conn.commit()

    out = relocate_document(
        tracking_db,
        "organismes/bn/x-developpement.pdf",
        "organismes/bn/x-développement.pdf",
        Lmod.new_run_id("test"))
    assert set(out["moved"]) == {"source", "transcription", "resume"}

    # 1) source déplacé (accentué)
    assert (docs / "organismes" / "bn" / "x-développement.pdf").exists()
    assert not (docs / "organismes" / "bn" / "x-developpement.pdf").exists()
    # 2) transcription RÉALIGNÉE (de vieux/ orphelin → bn/, accentuée)
    assert (tr / "organismes" / "bn" / "x-développement.md").exists()
    assert not (tr / "organismes" / "vieux" / "x-developpement.md").exists()
    # 3) résumé déplacé + son `source` mis à jour vers la nouvelle transcription
    new_res = res / "organismes" / "bn" / "x-développement.md"
    assert new_res.exists()
    assert _read_fm(new_res)["source"] == \
        "Transcriptions/Documents/organismes/bn/x-développement.md"
    # 4) DB : doc_classification + text_simhash repointés
    assert tracking_db.get_classification("organismes/bn/x-développement.pdf")
    rows = {r[0] for r in tracking_db._conn.execute(
        "SELECT rel_path FROM text_simhash")}
    assert "Transcriptions/Documents/organismes/bn/x-développement.md" in rows


def test_relocate_realign_orphan_transcription_same_rel(tmp_path, monkeypatch,
                                                        tracking_db):
    # old_rel == new_rel : doit réaligner la transcription orpheline SANS
    # toucher source/résumé ni vider la DB.
    docs, tr, res, croot = _setup(tmp_path, monkeypatch)
    (docs / "organismes" / "bn").mkdir(parents=True)
    (docs / "organismes" / "bn" / "x.pdf").write_bytes(b"%PDF")
    (res / "organismes" / "bn").mkdir(parents=True)
    (res / "organismes" / "bn" / "x.md").write_text(
        "---\nsource: Transcriptions/Documents/organismes/vieux/x.md\n---\nr",
        encoding="utf-8")
    (tr / "organismes" / "vieux").mkdir(parents=True)
    (tr / "organismes" / "vieux" / "x.md").write_text("t", encoding="utf-8")
    tracking_db.upsert_classification(
        "organismes/bn/x.pdf", {"status": "auto", "entity_slug": "bn",
                                "entity_type": "organismes"})

    out = relocate_document(tracking_db, "organismes/bn/x.pdf",
                            "organismes/bn/x.pdf", Lmod.new_run_id("realign"))
    assert out["moved"] == ["transcription"]              # seule la transcription
    assert (tr / "organismes" / "bn" / "x.md").exists()   # réalignée
    assert not (tr / "organismes" / "vieux" / "x.md").exists()
    # source/résumé/doc_classification intacts
    assert (res / "organismes" / "bn" / "x.md").exists()
    assert tracking_db.get_classification("organismes/bn/x.pdf")  # PAS vidée
    assert _read_fm(res / "organismes" / "bn" / "x.md")["source"] == \
        "Transcriptions/Documents/organismes/bn/x.md"


def test_relocate_dry_run(tmp_path, monkeypatch, tracking_db):
    docs, tr, res, croot = _setup(tmp_path, monkeypatch)
    (docs / "organismes" / "bn").mkdir(parents=True)
    (docs / "organismes" / "bn" / "a.pdf").write_bytes(b"x")
    out = relocate_document(tracking_db, "organismes/bn/a.pdf",
                            "organismes/bn/à.pdf", Lmod.new_run_id("t"),
                            dry_run=True)
    assert out["dry_run"] and "source" in out["moves"]
    assert (docs / "organismes" / "bn" / "a.pdf").exists()   # rien bougé
