"""Tests de commands/organize.py (apply) sur arborescence tmp :

- happy path : résumé + transcription déplacés vers la structure entité,
  `source:` du résumé réécrit, DB mise à jour, sync_warnings vide ;
- échec de la mise à jour DB : le run ne plante pas et remonte un
  sync_warning (step=tracking_db).
"""
import json
import textwrap

import pytest

from connaissance.commands import organize
from connaissance.core import ledger as ledger_mod


class _SharedDB:
    """Proxy vers la TrackingDB du test : close() no-op pour que
    `_apply_manifest` (qui ferme sa DB) ne ferme pas la fixture."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        pass


@pytest.fixture
def base(tmp_path, monkeypatch, tracking_db):
    """Racines organize sur tmp + TrackingDB partagée avec la fixture."""
    connaissance = tmp_path / "Connaissance"
    transcriptions = connaissance / "Transcriptions"
    resumes = connaissance / "Résumés"
    documents = tmp_path / "Documents"
    (transcriptions / "Courriels").mkdir(parents=True)
    (resumes / "Courriels").mkdir(parents=True)
    documents.mkdir()

    monkeypatch.setattr(organize, "CONNAISSANCE", connaissance)
    monkeypatch.setattr(organize, "TRANSCRIPTIONS", transcriptions)
    monkeypatch.setattr(organize, "RESUMES", resumes)
    monkeypatch.setattr(organize, "DOCUMENTS_DIR", documents)
    monkeypatch.setattr(organize, "PROTECTED_ROOTS", {
        documents,
        transcriptions / "Documents",
        transcriptions / "Courriels",
        transcriptions / "Notes",
        resumes / "Documents",
        resumes / "Courriels",
        resumes / "Notes",
        connaissance,
    })
    monkeypatch.setattr(organize, "TrackingDB",
                        lambda: _SharedDB(tracking_db))
    return connaissance


def _peupler(connaissance, tracking_db):
    """Un résumé courriel non organisé + sa transcription, indexés en DB."""
    trans = connaissance / "Transcriptions" / "Courriels" / "abc.md"
    trans.write_text("---\nfrom: alice@exemple.org\n---\nCorps.\n",
                     encoding="utf-8")
    resume = connaissance / "Résumés" / "Courriels" / "abc.md"
    resume.write_text(textwrap.dedent("""\
        ---
        source: Transcriptions/Courriels/abc.md
        entity_type: personnes
        entity_name: Jean Dupont
        ---
        Résumé.
        """), encoding="utf-8")
    tracking_db.register_file("Transcriptions/Courriels/abc.md",
                              "transcription", source_type="courriel")
    tracking_db.register_file("Résumés/Courriels/abc.md", "resume",
                              source_type="courriel")
    return resume, trans


def _manifeste(tmp_path, resume):
    entries = [{
        "source": "courriels",
        "resume_path": str(resume),
        "entity_type": "personnes",
        "entity_slug": "jean-dupont",
        "entity_name": "Jean Dupont",
        "new_name": "2026-01-15 sujet-test",
        "confidence": "high",
        "status": "auto",
    }]
    path = tmp_path / "manifeste.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def test_apply_happy_path(base, tmp_path, tracking_db):
    resume, trans = _peupler(base, tracking_db)
    manifest = _manifeste(tmp_path, resume)

    result = organize._apply_manifest(manifest, dry_run=False)

    assert result["moved"] == 1
    assert result["skipped"] == 0 and result["errors"] == 0
    assert result["sync_warnings"] == []

    # Fichiers déplacés vers la structure entité.
    dest_resume = (base / "Résumés" / "Courriels" / "personnes" /
                   "jean-dupont" / "2026-01-15 sujet-test.md")
    dest_trans = (base / "Transcriptions" / "Courriels" / "personnes" /
                  "jean-dupont" / "2026-01-15 sujet-test.md")
    assert dest_resume.exists() and dest_trans.exists()
    assert not resume.exists() and not trans.exists()

    # Le champ source: du résumé pointe vers la NOUVELLE transcription.
    contenu = dest_resume.read_text(encoding="utf-8")
    assert ("source: Transcriptions/Courriels/personnes/jean-dupont/"
            "2026-01-15 sujet-test.md") in contenu
    assert "source: Transcriptions/Courriels/abc.md" not in contenu

    # DB `files` mise à jour (move_file) : anciens chemins repointés + entité.
    assert tracking_db.get_file("Résumés/Courriels/abc.md") is None
    assert tracking_db.get_file("Transcriptions/Courriels/abc.md") is None
    new_resume = tracking_db.get_file(
        "Résumés/Courriels/personnes/jean-dupont/2026-01-15 sujet-test.md")
    assert new_resume is not None
    assert new_resume["entity_type"] == "personnes"
    assert new_resume["entity_slug"] == "jean-dupont"
    new_trans = tracking_db.get_file(
        "Transcriptions/Courriels/personnes/jean-dupont/2026-01-15 sujet-test.md")
    assert new_trans is not None and new_trans["entity_slug"] == "jean-dupont"

    # Réversible : les deux moves sont journalisés sous le run ledger.
    run_id = result["ledger_run"]
    ops = tracking_db.ledger_ops(run_id, status="applied")
    assert len(ops) == 2
    assert (ledger_mod.LEDGER_JOURNAL_DIR / f"{run_id}.jsonl").exists()


def test_apply_dry_run_ne_deplace_rien(base, tmp_path, tracking_db):
    resume, trans = _peupler(base, tracking_db)
    manifest = _manifeste(tmp_path, resume)

    result = organize._apply_manifest(manifest, dry_run=True)

    assert result["moved"] == 1 and "ledger_run" not in result
    assert resume.exists() and trans.exists()
    assert tracking_db.ledger_runs() == []


def test_apply_echec_db_produit_un_sync_warning(base, tmp_path, tracking_db,
                                                monkeypatch):
    resume, trans = _peupler(base, tracking_db)
    manifest = _manifeste(tmp_path, resume)

    def _move_file_en_panne(*args, **kwargs):
        raise RuntimeError("database is locked (simulé)")

    monkeypatch.setattr(tracking_db, "move_file", _move_file_en_panne)
    result = organize._apply_manifest(manifest, dry_run=False)

    # Le run ne plante pas : le déplacement disque a réussi…
    assert result["moved"] == 1 and result["errors"] == 0
    assert not resume.exists()
    # …et l'échec DB est VISIBLE dans le résultat.
    assert len(result["sync_warnings"]) == 1
    warning = result["sync_warnings"][0]
    assert warning["step"] == "tracking_db"
    assert "database is locked" in warning["error"]
    # La DB, elle, pointe toujours l'ancien chemin (dérive signalée).
    assert tracking_db.get_file("Résumés/Courriels/abc.md") is not None


def test_apply_public_expose_sync_warnings(base, tmp_path, tracking_db):
    # Régression corrigée le 2026-07-17 : l'API publique reconstruisait le
    # dict de retour en perdant `sync_warnings` (invisibles côté MCP/CLI).
    resume, _ = _peupler(base, tracking_db)
    manifest = _manifeste(tmp_path, resume)

    result = organize.apply(str(manifest), dry_run=False)

    assert "sync_warnings" in result
