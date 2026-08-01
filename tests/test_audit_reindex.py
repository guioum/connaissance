"""Tests de commands/audit_reindex.py : reconstruction de la DB depuis le
frontmatter des .md sur disque (chemin de rebuild « DB = index dérivé »).

Arborescence tmp sans transcriptions de documents (et skip_hashes=True) :
on couvre le chemin frontmatter → lignes `files`, pas la repopulation des
hashes sources.
"""
import textwrap

import pytest

from connaissance.commands import audit_reindex


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Rediriger les racines du module vers tmp. Retourne la racine."""
    root = tmp_path / "Connaissance"
    monkeypatch.setattr(audit_reindex, "CONNAISSANCE", root)
    monkeypatch.setattr(audit_reindex, "DOCUMENTS", tmp_path / "Documents")
    monkeypatch.setattr(audit_reindex, "TRANSCRIPTIONS", root / "Transcriptions")
    monkeypatch.setattr(audit_reindex, "RESUMES", root / "Résumés")
    monkeypatch.setattr(audit_reindex, "SYNTHESE", root / "Synthèse")
    return root


def _peupler(root):
    """2 transcriptions (types différents), 1 résumé typé, synthèse complète."""
    trans_c = root / "Transcriptions" / "Courriels" / "aaa.md"
    trans_c.parent.mkdir(parents=True)
    trans_c.write_text(textwrap.dedent("""\
        ---
        from: alice@exemple.org
        message-id: <m1@exemple.org>
        created: 2026-01-10
        ---
        Corps transcrit.
        """), encoding="utf-8")

    trans_n = root / "Transcriptions" / "Notes" / "note1.md"
    trans_n.parent.mkdir(parents=True)
    trans_n.write_text("---\ncreated: 2026-02-01\n---\nUne note.\n",
                       encoding="utf-8")

    resume = root / "Résumés" / "Courriels" / "rrr.md"
    resume.parent.mkdir(parents=True)
    resume.write_text(textwrap.dedent("""\
        ---
        entity_type: personnes
        entity_slug: jean-dupont
        source: Transcriptions/Courriels/aaa.md
        message-id: <m1@exemple.org>
        created: 2026-01-10
        ---
        Résumé.
        """), encoding="utf-8")

    entite = root / "Synthèse" / "personnes" / "jean-dupont"
    entite.mkdir(parents=True)
    (entite / "fiche.md").write_text("---\nname: Jean\n---\nFiche.\n",
                                     encoding="utf-8")
    (entite / "chronologie.md").write_text("# Chronologie\n", encoding="utf-8")
    sujets = root / "Synthèse" / "sujets"
    sujets.mkdir()
    (sujets / "impots.md").write_text("# MOC Impôts\n", encoding="utf-8")
    digests = root / "Synthèse" / "rapports" / "digests"
    digests.mkdir(parents=True)
    (digests / "2026-01.md").write_text("# Digest\n", encoding="utf-8")


def _etat_files(db):
    """Photo comparable de la table files (sans les timestamps techniques)."""
    rows = db._conn.execute(
        """SELECT path, file_type, source_type, source_path, entity_type,
                  entity_slug, message_id, created FROM files ORDER BY path"""
    ).fetchall()
    return [tuple(r) for r in rows]


def test_reindex_reconstruit_les_lignes_attendues(base, tracking_db):
    _peupler(base)

    result = audit_reindex.reindex(dry_run=False, skip_hashes=True,
                                   db=tracking_db)

    assert result["rescanned"] == 7   # 2 trans + 1 résumé + fiche/chrono/moc/digest
    assert result["details"]["transcriptions"] == {
        "document": 0, "courriel": 1, "note": 1, "total": 2,
        "frontmatter_backfilled": 0}
    assert result["details"]["resumes"] == {
        "total": 1, "avec_entite": 1, "avec_source": 1}
    assert result["details"]["synthese"] == {
        "fiche": 1, "chronologie": 1, "moc": 1, "digest": 1}

    trans = tracking_db.get_file("Transcriptions/Courriels/aaa.md")
    assert trans["file_type"] == "transcription"
    assert trans["source_type"] == "courriel"
    assert trans["message_id"] == "<m1@exemple.org>"
    assert trans["created"] == "2026-01-10"

    note = tracking_db.get_file("Transcriptions/Notes/note1.md")
    assert note["file_type"] == "transcription" and note["source_type"] == "note"

    resume = tracking_db.get_file("Résumés/Courriels/rrr.md")
    assert resume["file_type"] == "resume"
    assert resume["entity_type"] == "personnes"
    assert resume["entity_slug"] == "jean-dupont"
    assert resume["source_path"] == \
        "Connaissance/Transcriptions/Courriels/aaa.md"   # forme canon

    fiche = tracking_db.get_file("Synthèse/personnes/jean-dupont/fiche.md")
    assert fiche["file_type"] == "fiche" and fiche["entity_slug"] == "jean-dupont"
    assert tracking_db.get_file(
        "Synthèse/personnes/jean-dupont/chronologie.md")["file_type"] == "chronologie"
    assert tracking_db.get_file("Synthèse/sujets/impots.md")["file_type"] == "moc"
    assert tracking_db.get_file(
        "Synthèse/rapports/digests/2026-01.md")["file_type"] == "digest"


def test_frontmatter_casse_ne_fait_pas_planter(base, tracking_db):
    _peupler(base)
    casse = base / "Résumés" / "Courriels" / "casse.md"
    casse.write_text("---\nfoo: [séquence jamais fermée\nbar: : :\n---\nCorps.\n",
                     encoding="utf-8")

    result = audit_reindex.reindex(dry_run=False, skip_hashes=True,
                                   db=tracking_db)

    # Compté dans le total, mais sans métadonnées d'entité exploitables.
    assert result["details"]["resumes"]["total"] == 2
    assert result["details"]["resumes"]["avec_entite"] == 1
    assert result["details"]["resumes"]["avec_source"] == 1
    row = tracking_db.get_file("Résumés/Courriels/casse.md")
    assert row is not None and row["file_type"] == "resume"
    assert row["entity_type"] is None and row["entity_slug"] is None


def test_reindex_purge_les_lignes_orphelines(base, tracking_db):
    _peupler(base)
    # Ligne DB pointant vers un fichier disparu du disque : le rebuild la purge.
    tracking_db.register_file("Transcriptions/Courriels/disparu.md",
                              "transcription", source_type="courriel")

    result = audit_reindex.reindex(dry_run=False, skip_hashes=True,
                                   db=tracking_db)

    assert result["details"]["orphans"]["total"] == 1
    assert tracking_db.get_file("Transcriptions/Courriels/disparu.md") is None


def test_reindex_idempotent(base, tracking_db):
    _peupler(base)

    r1 = audit_reindex.reindex(dry_run=False, skip_hashes=True, db=tracking_db)
    etat_1 = _etat_files(tracking_db)
    r2 = audit_reindex.reindex(dry_run=False, skip_hashes=True, db=tracking_db)
    etat_2 = _etat_files(tracking_db)

    assert etat_1 == etat_2
    assert r1["rescanned"] == r2["rescanned"]
    assert r1["details"] == r2["details"]


def test_dry_run_ne_modifie_pas_la_db(base, tracking_db):
    _peupler(base)

    result = audit_reindex.reindex(dry_run=True, skip_hashes=True,
                                   db=tracking_db)

    assert result["dry_run"] is True
    assert result["rescanned"] == 7
    assert _etat_files(tracking_db) == []


def test_prune_purge_les_lignes_doc_fantomes(tmp_path, monkeypatch, tracking_db):
    """Un fichier déplacé HORS ledger laisse des lignes doc_* fantômes que
    transcribe-plan re-propose indéfiniment (constaté 2026-08-01). Le reindex
    doit les purger ; les lignes des fichiers présents survivent."""
    from connaissance.commands import audit_reindex as R
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "present.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(R, "DOCUMENTS_DIR", docs)
    tracking_db.upsert_classification("present.pdf", {"status": "auto"})
    tracking_db.upsert_classification("- Inbox/fantome.pdf", {"status": "auto"})
    tracking_db.add_doc_sujets("- Inbox/fantome.pdf", ["impots"], "classify")

    counts = R.prune_orphans(tracking_db, dry_run=True)
    assert counts["doc_rels"] == 1
    assert tracking_db.get_classification("- Inbox/fantome.pdf") is not None

    counts = R.prune_orphans(tracking_db, dry_run=False)
    assert counts["doc_rels"] == 1 and counts["doc_rows_deleted"] >= 2
    assert tracking_db.get_classification("- Inbox/fantome.pdf") is None
    assert tracking_db.get_classification("present.pdf") is not None
