"""Tests de commands/emails_cleanup.py sur arborescence tmp :

- scan des obsolètes piloté par les seuils du scoring ;
- dry_run par défaut : aucun move, DB intacte ;
- apply : transcriptions ET résumés archivés, lignes `files` retirées ;
- réversibilité : moves journalisés au ledger sous un run_id.
"""
import textwrap

import pytest
import yaml

from connaissance.commands import emails_cleanup
from connaissance.core import filtres as filtres_mod
from connaissance.core import ledger as ledger_mod
from connaissance.core.filtres import Filtres


def _scoring_yaml(seuil_ignorer: int) -> str:
    return yaml.safe_dump({
        "seuils": {"ignorer": seuil_ignorer},
        "poids": {"adresse_marketing": -2, "domaine_personnel": 2},
        "domaines_marketing": ["spam.example"],
        "domaines_personnels": ["perso.example"],
        # corps_min: 0 → jamais de malus « corps quasi-vide » dans ces tests
        "seuils_numeriques": {"corps_min": 0},
    }, allow_unicode=True)


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Base de connaissances tmp + configs filtres/scoring isolées.

    Retourne (root, transcriptions_courriels, resumes_courriels, archive_root).
    """
    root = tmp_path / "Connaissance"
    trans = root / "Transcriptions" / "Courriels"
    resumes = root / "Résumés" / "Courriels"
    archive = root / ".archive" / "courriels-depublies"
    trans.mkdir(parents=True)
    resumes.mkdir(parents=True)

    monkeypatch.setattr(emails_cleanup, "CONNAISSANCE_ROOT", root)
    monkeypatch.setattr(emails_cleanup, "TRANSCRIPTIONS_COURRIELS", trans)
    monkeypatch.setattr(emails_cleanup, "RESUMES_COURRIELS", resumes)
    monkeypatch.setattr(emails_cleanup, "ARCHIVE_ROOT", archive)
    monkeypatch.setattr(emails_cleanup, "require_connaissance_root",
                        lambda: None)

    # Filtres() lit USER_FILTRES/USER_SCORING (constantes module) : on les
    # détourne vers tmp pour ne jamais toucher au vrai ~/Connaissance/.config.
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    filtres_yaml = cfg / "filtres.yaml"
    scoring_yaml = cfg / "scoring-courriels.yaml"
    filtres_yaml.write_text("{}\n", encoding="utf-8")
    scoring_yaml.write_text(_scoring_yaml(-1), encoding="utf-8")
    monkeypatch.setattr(filtres_mod, "USER_FILTRES", filtres_yaml)
    monkeypatch.setattr(filtres_mod, "USER_SCORING", scoring_yaml)
    monkeypatch.setattr(filtres_mod, "require_connaissance_root", lambda: None)

    return root, trans, resumes, archive


def _ecrire_courriel(trans_dir, nom, from_addr, sujet="Un sujet"):
    p = trans_dir / f"{nom}.md"
    p.write_text(textwrap.dedent(f"""\
        ---
        from: {from_addr}
        subject: {sujet}
        date: 2026-01-15
        folder: INBOX
        ---
        Corps du message, suffisamment long pour rester neutre.
        """), encoding="utf-8")
    return p


def _ecrire_resume(resumes_dir, nom, source_rel):
    p = resumes_dir / f"{nom}.md"
    p.write_text(textwrap.dedent(f"""\
        ---
        source: {source_rel}
        title: Résumé test
        ---
        Résumé du courriel.
        """), encoding="utf-8")
    return p


def _base_peuplee(base, tracking_db):
    """Un courriel marketing (obsolète) avec résumé + un courriel personnel."""
    root, trans, resumes, archive = base
    obsolete = _ecrire_courriel(trans, "promo", "promo@spam.example", "Promo")
    garde = _ecrire_courriel(trans, "perso", "alice@perso.example", "Souper")
    resume = _ecrire_resume(resumes, "promo-resume",
                            "Transcriptions/Courriels/promo.md")
    for p, ftype in ((obsolete, "transcription"), (garde, "transcription"),
                     (resume, "resume")):
        tracking_db.register_file(str(p.relative_to(root)), ftype,
                                  source_type="courriel")
    return obsolete, garde, resume


# --- (a) scan piloté par les seuils de la config ---


def test_scan_respecte_le_seuil_ignorer(base, tracking_db):
    _base_peuplee(base, tracking_db)

    # seuil ignorer = -1 : le marketing (-2) tombe dessous, le perso (+2) non.
    obsoletes = emails_cleanup.scan_obsoletes(Filtres())
    assert [o["transcription_rel"] for o in obsoletes] == \
        ["Transcriptions/Courriels/promo.md"]
    assert obsoletes[0]["score"] == -2
    assert obsoletes[0]["resume"] is not None    # reverse-map source → résumé

    # Seuil abaissé à -3 : plus rien n'est obsolète (relire la config).
    filtres_mod.USER_SCORING.write_text(_scoring_yaml(-3), encoding="utf-8")
    assert emails_cleanup.scan_obsoletes(Filtres()) == []


# --- (b) dry_run par défaut ---


def test_dry_run_par_defaut_ne_touche_rien(base, tracking_db):
    root, _, _, archive = base
    obsolete, garde, resume = _base_peuplee(base, tracking_db)

    result = emails_cleanup.cleanup_obsolete(db=tracking_db)

    assert result["dry_run"] is True
    assert result["total_scanned"] == 2
    assert [w["transcription_rel"] for w in result["would_archive"]] == \
        ["Transcriptions/Courriels/promo.md"]
    assert result["archived_to"] == "" and result["ledger_run"] == ""
    # Rien déplacé, rien archivé, DB intacte.
    assert obsolete.exists() and garde.exists() and resume.exists()
    assert not archive.exists()
    for p in (obsolete, garde, resume):
        assert tracking_db.get_file(str(p.relative_to(root))) is not None
    assert tracking_db.ledger_runs() == []


# --- (c) apply : archive transcriptions + résumés, retire les lignes files ---


def test_apply_archive_et_purge_la_db(base, tracking_db):
    root, _, _, archive = base
    obsolete, garde, resume = _base_peuplee(base, tracking_db)

    result = emails_cleanup.cleanup_obsolete(dry_run=False, db=tracking_db)

    assert result["dry_run"] is False
    archive_dir = result["archived_to"]
    assert archive_dir.startswith(str(archive))
    # Transcription ET résumé déplacés vers l'archive (structure préservée).
    assert not obsolete.exists() and not resume.exists()
    from pathlib import Path
    assert (Path(archive_dir) / "Transcriptions/Courriels/promo.md").exists()
    assert (Path(archive_dir) / "Résumés/Courriels/promo-resume.md").exists()
    # Le manifest décrit l'opération.
    manifest = Path(result["manifest_path"])
    assert manifest.exists()
    # Lignes `files` retirées pour les archivés, conservées pour le gardé.
    assert tracking_db.get_file("Transcriptions/Courriels/promo.md") is None
    assert tracking_db.get_file("Résumés/Courriels/promo-resume.md") is None
    assert tracking_db.get_file("Transcriptions/Courriels/perso.md") is not None
    assert garde.exists()


# --- (d) réversibilité : les moves passent par safe_move + run_id ---


def test_apply_est_journalise_au_ledger(base, tracking_db):
    _base_peuplee(base, tracking_db)

    result = emails_cleanup.cleanup_obsolete(dry_run=False, db=tracking_db)

    run_id = result["ledger_run"]
    assert run_id.startswith("cleanup-courriel-")
    ops = tracking_db.ledger_ops(run_id, status="applied")
    # Une opération par fichier déplacé : transcription + résumé.
    assert len(ops) == 2
    moved = {op["old_path"].rsplit("/", 1)[-1] for op in ops}
    assert moved == {"promo.md", "promo-resume.md"}
    for op in ops:
        assert op["sha256"]                       # revert vérifiable par hash
    # Le journal disque du run existe (rejouable par restore-journals).
    assert (ledger_mod.LEDGER_JOURNAL_DIR / f"{run_id}.jsonl").exists()
