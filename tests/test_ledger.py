"""Tests du ledger réversible (core/ledger.py) : move journalisé + rollback vérifié."""
import connaissance.core.ledger as L
from connaissance.core.ledger import new_run_id, revert_run, safe_move, verify_run


def test_safe_move_records_and_moves(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "sub" / "b.txt"
    run = new_run_id()
    entry = safe_move(tracking_db, src, dst, "test", run)
    assert entry["status"] == "applied"
    assert dst.exists() and not src.exists()
    ops = tracking_db.ledger_ops(run, status="applied")
    assert len(ops) == 1
    assert ops[0]["new_path"] == str(dst) and ops[0]["old_path"] == str(src)
    assert ops[0]["sha256"]  # hash enregistré


def test_dry_run_touches_nothing(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "b.txt"
    entry = safe_move(tracking_db, src, dst, "test", new_run_id(), dry_run=True)
    assert entry["status"] == "planned"
    assert src.exists() and not dst.exists()
    assert tracking_db.ledger_runs() == []   # rien journalisé en dry-run


def test_revert_roundtrip(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "moved" / "a.txt"
    run = new_run_id()
    safe_move(tracking_db, src, dst, "test", run)
    res = revert_run(tracking_db, run)
    assert res["reverted"] == 1 and res["skipped"] == []
    assert src.exists() and not dst.exists()
    assert tracking_db.ledger_ops(run, status="applied") == []  # marqué reverted


def test_revert_skips_modified_content(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "moved" / "a.txt"
    run = new_run_id()
    safe_move(tracking_db, src, dst, "test", run)
    dst.write_text("contenu modifié depuis le déplacement", encoding="utf-8")
    res = revert_run(tracking_db, run)
    assert res["reverted"] == 0
    assert res["skipped"][0]["reason"] == "contenu_modifie"
    assert dst.exists() and not src.exists()   # rien restauré : on ne perd pas la version récente


def test_revert_dry_run_reports_without_moving(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "moved" / "a.txt"
    run = new_run_id()
    safe_move(tracking_db, src, dst, "test", run)
    res = revert_run(tracking_db, run, dry_run=True)
    assert res["reverted"] == 1 and res["dry_run"] is True
    assert dst.exists() and not src.exists()   # rien bougé
    assert tracking_db.ledger_ops(run, status="applied")  # toujours 'applied'


def test_revert_relinks_references(tracking_db, tmp_path, monkeypatch):
    """Le revert fait SUIVRE les références DB : fiche relinkée à l'inverse,
    simhash de transcription renommé, `source` du résumé restauré."""
    docs = tmp_path / "Documents"
    croot = tmp_path / "Connaissance"
    monkeypatch.setattr(L, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(L, "CONNAISSANCE_ROOT", croot)
    (docs / "a").mkdir(parents=True)
    f = docs / "a" / "x.pdf"
    f.write_bytes(b"%PDF")
    tr = croot / "Transcriptions" / "Documents" / "a" / "x.md"
    tr.parent.mkdir(parents=True)
    tr.write_text("transcription", encoding="utf-8")
    res_md = croot / "Résumés" / "Documents" / "a" / "x.md"
    res_md.parent.mkdir(parents=True)
    res_md.write_text("---\nsource: Transcriptions/Documents/b/x.md\n---\nr",
                      encoding="utf-8")
    tracking_db.upsert_classification("a/x.pdf", {"status": "auto"})
    tracking_db._conn.execute(
        "INSERT INTO text_simhash (rel_path, simhash) VALUES (?, ?)",
        ("Transcriptions/Documents/a/x.md", "abc"))
    tracking_db._conn.commit()

    # Aller : source + transcription déplacées, refs relinkées (comme relocate).
    run = new_run_id()
    safe_move(tracking_db, f, docs / "b" / "x.pdf", "test", run)
    safe_move(tracking_db, tr,
              croot / "Transcriptions" / "Documents" / "b" / "x.md", "test", run)
    tracking_db.relink_document("a/x.pdf", "b/x.pdf")
    tracking_db.rename_text_simhash("Transcriptions/Documents/a/x.md",
                                    "Transcriptions/Documents/b/x.md")
    # Le résumé n'a pas bougé mais son move est simulé par le run précédent :
    # on le déplace aussi pour tester la restauration de son champ `source`.
    safe_move(tracking_db, res_md,
              croot / "Résumés" / "Documents" / "b" / "x.md", "test", run)

    out = revert_run(tracking_db, run)
    assert out["reverted"] == 3 and out["skipped"] == []
    # fiche revenue à l'ancien rel
    assert tracking_db.get_classification("a/x.pdf") is not None
    assert tracking_db.get_classification("b/x.pdf") is None
    # simhash revenu à l'ancien rel de transcription
    row = tracking_db._conn.execute(
        "SELECT rel_path FROM text_simhash").fetchone()
    assert row[0] == "Transcriptions/Documents/a/x.md"
    # `source` du résumé restauré vers la transcription co-localisée
    assert "Transcriptions/Documents/a/x.md" in res_md.read_text(
        encoding="utf-8")


def test_verify_detects_missing(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "moved" / "a.txt"
    run = new_run_id()
    safe_move(tracking_db, src, dst, "test", run)
    assert verify_run(tracking_db, run) == {"run_id": run, "checked": 1, "ok": 1, "issues": []}
    dst.unlink()
    v = verify_run(tracking_db, run)
    assert v["ok"] == 0 and v["issues"][0]["reason"] == "disparu"
