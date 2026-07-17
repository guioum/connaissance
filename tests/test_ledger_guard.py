"""Tests des garde-fous de safe_move (core/ledger.py) :

- refus d'écraser une destination existante (FileExistsError) ;
- journal JSONL écrit AVANT l'enregistrement DB (survit à un échec DB) ;
- déplacement normal toujours fonctionnel (entry applied + JSONL présent).
"""
import json

import pytest

from connaissance.core import ledger as ledger_mod
from connaissance.core.ledger import new_run_id, safe_move


def _jsonl_lines(run_id):
    path = ledger_mod.LEDGER_JOURNAL_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return None
    return [json.loads(l) for l in
            path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_refuse_ecraser_destination_existante(tracking_db, tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("contenu source", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    dst.write_text("contenu cible à protéger", encoding="utf-8")
    run = new_run_id()

    with pytest.raises(FileExistsError):
        safe_move(tracking_db, src, dst, "test collision", run)

    # Le fichier cible est intact, la source n'a pas bougé.
    assert dst.read_text(encoding="utf-8") == "contenu cible à protéger"
    assert src.read_text(encoding="utf-8") == "contenu source"
    # Rien de journalisé : ni ledger DB, ni JSONL disque.
    assert tracking_db.ledger_ops(run, status="applied") == []
    assert _jsonl_lines(run) is None


def test_renommage_de_casse_autorise(tracking_db, tmp_path):
    """Sur APFS insensible à la casse, old et new désignent le même fichier :
    le garde anti-écrasement laisse passer. Sur un FS sensible à la casse, la
    destination n'existe simplement pas — le rename passe aussi."""
    src = tmp_path / "Fichier.txt"
    src.write_text("contenu", encoding="utf-8")
    dst = tmp_path / "fichier.txt"
    run = new_run_id()

    entry = safe_move(tracking_db, src, dst, "renommage casse", run)

    assert entry["status"] == "applied"
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "contenu"


def test_jsonl_ecrit_meme_si_db_echoue(tracking_db, tmp_path, monkeypatch):
    """Le JSONL disque est écrit avant ledger_record : si la DB échoue, la
    trace du déplacement survit sur disque (rejouable par restore-journals)."""
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "sub" / "a.txt"
    run = new_run_id()

    def _db_en_panne(entry, *, commit=True):
        raise RuntimeError("database is locked (simulé)")

    monkeypatch.setattr(tracking_db, "ledger_record", _db_en_panne)
    with pytest.raises(RuntimeError, match="database is locked"):
        safe_move(tracking_db, src, dst, "test panne DB", run)

    # Le fichier a bien bougé et le journal disque garde la trace.
    assert dst.exists() and not src.exists()
    lines = _jsonl_lines(run)
    assert lines is not None and len(lines) == 1
    assert lines[0]["old_path"] == str(src)
    assert lines[0]["new_path"] == str(dst)
    assert lines[0]["status"] == "applied"
    assert lines[0]["sha256"]


def test_deplacement_normal_journalise_partout(tracking_db, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "sub" / "b.txt"
    run = new_run_id()

    entry = safe_move(tracking_db, src, dst, "test nominal", run)

    assert entry["status"] == "applied"
    assert dst.exists() and not src.exists()
    ops = tracking_db.ledger_ops(run, status="applied")
    assert len(ops) == 1
    assert ops[0]["old_path"] == str(src) and ops[0]["new_path"] == str(dst)
    lines = _jsonl_lines(run)
    assert lines is not None and len(lines) == 1
    assert lines[0]["new_path"] == str(dst)
    # DB et JSONL racontent la même histoire (même hash).
    assert lines[0]["sha256"] == ops[0]["sha256"]
