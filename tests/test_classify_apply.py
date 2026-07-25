"""Tests de la brique 5 Phase C (commands/classify.apply).

Depuis le retrofit sur ``relocate_document``, apply déplace le graphe complet :
les chemins du module ``relocate`` (figés à l'import) sont patchés ici comme
dans test_relocate.py.
"""
import json

import connaissance.core.ledger as Lmod
import connaissance.core.relocate as R
from connaissance.commands import classify as CMD


def _manifest(tmp_path, entries):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return str(p)


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    croot = tmp_path / "Connaissance"
    tr = croot / "Transcriptions" / "Documents"
    res = croot / "Résumés" / "Documents"
    for p in (root, tr, res):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    monkeypatch.setattr(R, "DOCUMENTS_DIR", root)
    monkeypatch.setattr(R, "TRANSCR", tr)
    monkeypatch.setattr(R, "RESUMES", res)
    monkeypatch.setattr(R, "CONNAISSANCE_ROOT", croot)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", croot)
    return root, tr, res


def test_apply_dry_run_moves_nothing(tmp_path, monkeypatch, tracking_db):
    root, _, _ = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    f = root / "src" / "a.pdf"
    f.write_bytes(b"%PDF data")
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
    root, _, _ = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    f = root / "src" / "a.pdf"
    f.write_bytes(b"%PDF data")
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/banque-nationale/2024-01-01 releve.pdf",
         "category": "banque"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 1 and "ledger_run" in res
    assert not f.exists()
    assert (root / "organismes/banque-nationale/2024-01-01 releve.pdf").exists()


def test_apply_moves_full_graph(tmp_path, monkeypatch, tracking_db):
    """Retrofit relocate_document : transcription et résumé SUIVENT la source
    (fini les transcriptions orphelines du grand déplacement)."""
    root, tr, res_dir = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    (root / "src" / "a.pdf").write_bytes(b"%PDF data")
    (tr / "src").mkdir(parents=True)
    (tr / "src" / "a.md").write_text("transcription", encoding="utf-8")
    (res_dir / "src").mkdir(parents=True)
    (res_dir / "src" / "a.md").write_text(
        "---\nsource: Transcriptions/Documents/src/a.md\n---\nrésumé",
        encoding="utf-8")
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/bn/2024-01-01 releve.pdf", "category": "banque"},
    ])
    out = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert out["moved"] == 1
    assert (root / "organismes/bn/2024-01-01 releve.pdf").exists()
    new_tr = tr / "organismes/bn/2024-01-01 releve.md"
    new_res = res_dir / "organismes/bn/2024-01-01 releve.md"
    assert new_tr.exists() and not (tr / "src" / "a.md").exists()
    assert new_res.exists() and not (res_dir / "src" / "a.md").exists()
    # le `source` du résumé pointe la NOUVELLE transcription
    assert "organismes/bn/2024-01-01 releve.md" in new_res.read_text(
        encoding="utf-8")


def test_apply_noop_when_already_in_place(tmp_path, monkeypatch, tracking_db):
    """Garde old==new : un doc déjà à sa place n'est PAS renommé en « (2) »."""
    root, _, _ = _setup(tmp_path, monkeypatch)
    d = root / "organismes/x"
    d.mkdir(parents=True)
    f = d / "2024-01-01 titre.pdf"
    f.write_bytes(b"%PDF")
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "organismes/x/2024-01-01 titre.pdf",
         "dest": "organismes/x/2024-01-01 titre.pdf", "category": "banque"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 0
    assert res["skipped"] == [{"source": "organismes/x/2024-01-01 titre.pdf",
                               "reason": "deja_en_place"}]
    assert f.exists()
    assert not (d / "2024-01-01 titre (2).pdf").exists()


def test_apply_reconciles_after_crash(tmp_path, monkeypatch, tracking_db):
    """Reprise post-crash : fichier déjà à destination, fiche encore à l'ancien
    chemin (crash entre move et commit) → relink réparé ; relance → déjà
    appliqué. Idempotent."""
    root, _, _ = _setup(tmp_path, monkeypatch)
    d = root / "organismes/x"
    d.mkdir(parents=True)
    (d / "2024-01-01 t.pdf").write_bytes(b"%PDF")     # déjà déplacé (crash)
    tracking_db.upsert_classification(
        "src/a.pdf", {"status": "auto", "category": "banque"})
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/a.pdf",
         "dest": "organismes/x/2024-01-01 t.pdf", "category": "banque"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 0 and res["reconciled"] == 1
    assert res["skipped"][0]["reason"] == "relink_repare"
    assert tracking_db.get_classification("src/a.pdf") is None
    assert tracking_db.get_classification(
        "organismes/x/2024-01-01 t.pdf") is not None
    # relance : plus rien à réparer
    res2 = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res2["reconciled"] == 0
    assert res2["skipped"][0]["reason"] == "deja_applique"


def test_apply_handles_name_collision(tmp_path, monkeypatch, tracking_db):
    root, _, _ = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    (root / "src" / "a.pdf").write_bytes(b"x")
    # cible déjà occupée (par un AUTRE fichier)
    dest = root / "organismes/x"
    dest.mkdir(parents=True)
    (dest / "2024-01-01 titre.pdf").write_bytes(b"existant")
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
    root, _, _ = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    (root / "src" / "a.pdf").write_bytes(b"%PDF data")
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
    root, _, _ = _setup(tmp_path, monkeypatch)
    (root / "src").mkdir()
    (root / "src" / "a.pdf").write_bytes(b"%PDF data")

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
    root, _, _ = _setup(tmp_path, monkeypatch)
    mf = _manifest(tmp_path, [
        {"status": "auto", "source": "src/absent.pdf",
         "dest": "organismes/x/t.pdf", "category": "divers"},
    ])
    res = CMD.apply(mf, dry_run=False, db=tracking_db)
    assert res["moved"] == 0 and len(res["skipped"]) == 1
    assert res["skipped"][0]["reason"] == "source_introuvable"
