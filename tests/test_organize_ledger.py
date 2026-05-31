"""Retrofit : les déplacements d'organize passent par le ledger (réversible)."""
from connaissance.commands import organize
from connaissance.core.ledger import new_run_id


def test_move_with_attachments_records_to_ledger(tracking_db, tmp_path):
    src = tmp_path / "src" / "a.md"
    src.parent.mkdir()
    src.write_text("---\ntitle: x\n---\ncontenu", encoding="utf-8")
    dst = tmp_path / "dst" / "b.md"
    run = new_run_id("organize")

    ok = organize._move_with_attachments(
        src, dst, "documents", db=tracking_db, run_id=run, reason="test")

    assert ok is True
    assert dst.exists() and not src.exists()
    ops = tracking_db.ledger_ops(run, status="applied")
    assert len(ops) == 1
    assert ops[0]["new_path"] == str(dst) and ops[0]["sha256"]


def test_fallback_without_ledger(tracking_db, tmp_path):
    # Sans db/run_id : repli sur shutil.move, rien n'est journalisé.
    src = tmp_path / "a.md"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "b.md"
    organize._move_with_attachments(src, dst, "documents")
    assert dst.exists() and not src.exists()
    assert tracking_db.ledger_runs() == []
