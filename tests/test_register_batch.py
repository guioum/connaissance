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
