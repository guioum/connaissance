"""Tests du ledger réversible (core/ledger.py) : move journalisé + rollback vérifié."""
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
