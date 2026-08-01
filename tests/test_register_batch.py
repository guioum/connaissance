"""Tests de documents.register_batch — register en lot depuis un manifeste de scan.

On teste en `dry_run` : la partition présent/manquant et le comptage sont la
nouveauté ; `register_document` (sous-jacent) est couplé à TRANSCRIPTIONS_DIR et
déjà exercé ailleurs.
"""
import json

import pytest

from connaissance.commands import documents


def _manifest(tmp_path, items):
    p = tmp_path / "scan.json"
    p.write_text(json.dumps({"to_transcribe": items, "skipped": []}),
                 encoding="utf-8")
    return str(p)


def test_partitions_present_and_missing(tmp_path, tracking_db):
    present = tmp_path / "present.md"
    present.write_text("transcription", encoding="utf-8")
    manifest = _manifest(tmp_path, [
        {"source": "/Documents/a.pdf", "transcription": str(present),
         "rel": "a.md", "hash": "h1"},
        {"source": "/Documents/b.pdf", "transcription": str(tmp_path / "absent.md"),
         "rel": "b.md", "hash": "h2"},
    ])
    res = documents.register_batch(manifest, dry_run=True, db=tracking_db)
    assert res["registered"] == 1   # seule la transcription existante
    assert res["total"] == 2
    assert [m["source"] for m in res["missing"]] == ["/Documents/b.pdf"]
    assert res["dry_run"] is True


def test_empty_manifest(tmp_path, tracking_db):
    res = documents.register_batch(_manifest(tmp_path, []), dry_run=True,
                                   db=tracking_db)
    assert res == {"registered": 0, "missing": [], "total": 0, "dry_run": True,
                   "content_dupes_propagated": 0, "content_dupes_missing": []}


def test_unreadable_manifest_raises(tmp_path, tracking_db):
    with pytest.raises(ValueError):
        documents.register_batch(str(tmp_path / "nope.json"), dry_run=True,
                                 db=tracking_db)


def test_usage_pas_de_double_comptage_meme_jour(tmp_path, tracking_db,
                                                monkeypatch):
    """Garde anti-double-comptage (incident 2026-08-01) : re-register le même
    manifeste le même jour (reprise, passe finale) ne re-journalise pas le
    coût OCR ; le modèle passé est journalisé tel quel."""
    # register_document est couplé à TRANSCRIPTIONS_DIR (testé ailleurs) —
    # on le neutralise, l'objet du test est la journalisation d'usage.
    monkeypatch.setattr(documents, "register_document", lambda *a, **k: None)
    present = tmp_path / "t.md"
    present.write_text("---\nsource: a.pdf\n---\ncorps", encoding="utf-8")
    manifest = _manifest(tmp_path, [
        {"source": str(tmp_path / "a.pdf"), "transcription": str(present),
         "rel": "a.md", "hash": "h1", "pages": 3},
    ])
    documents.register_batch(manifest, ocr_engine="mistral",
                             ocr_model="mistral-ocr-4-0", db=tracking_db)
    documents.register_batch(manifest, ocr_engine="mistral",
                             ocr_model="mistral-ocr-4-0", db=tracking_db)
    rows = tracking_db._conn.execute(
        "SELECT model, units FROM llm_usage WHERE operation='ocr_mistral'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "mistral-ocr-4-0" and rows[0]["units"] == 3
