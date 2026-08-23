"""Tests de commands/notes : source = export anotes, sonde de fraîcheur."""
import os
import time
from pathlib import Path

import pytest

from connaissance.commands import notes
from connaissance.core import paths


def _note(path: Path, created="2026-08-01") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncreated: {created}\nmodified: {created}\n---\n# {path.stem}\n",
        encoding="utf-8",
    )


@pytest.fixture
def export(tmp_path, monkeypatch):
    """Un export anotes minimal : marqueur frais + deux notes, miroir vide."""
    src = tmp_path / "Archives" / "Notes"
    dest = tmp_path / "Connaissance" / "Transcriptions" / "Notes"
    src.mkdir(parents=True)
    dest.mkdir(parents=True)
    (src / notes.EXPORT_STATE_FILE).write_text("{}", encoding="utf-8")
    _note(src / "Journal" / "a.md")
    _note(src / "Archives" / "vieille.md")  # dossier ignoré par filtres.yaml
    monkeypatch.setattr(notes, "NOTES_DIR", src)
    monkeypatch.setattr(notes, "TRANSCRIPTIONS_DIR", dest)
    return src, dest


def test_source_est_l_export_mac_automations():
    """La source d'ingestion est l'export quotidien, plus l'ancien ~/Notes."""
    assert paths.NOTES_EXPORT_DIR == paths.ARCHIVES_ROOT / "Notes"
    assert paths.NOTES_EXPORT_DIR == paths.BASE_PATH / "Archives" / "Notes"
    assert notes.NOTES_DIR == paths.NOTES_EXPORT_DIR


def test_export_status_frais(export):
    src, _ = export
    st = notes.export_status(src)
    assert st["stale"] is False
    assert st["age_days"] == 0
    assert st["last_export"] is not None
    assert st["source"] == str(src)


def test_export_status_perime(export):
    src, _ = export
    vieux = time.time() - (notes.EXPORT_STALE_DAYS + 2) * 86400
    os.utime(src / notes.EXPORT_STATE_FILE, (vieux, vieux))
    st = notes.export_status(src)
    assert st["stale"] is True
    assert st["age_days"] >= notes.EXPORT_STALE_DAYS + 1


def test_export_status_sans_marqueur(tmp_path):
    """Un dossier sans `.export_state.json` n'est pas un export garanti : stale."""
    st = notes.export_status(tmp_path)
    assert st == {"last_export": None, "age_days": None, "stale": True,
                  "source": str(tmp_path)}


def test_backlog_count_rapporte_la_sonde(export):
    src, _ = export
    out = notes.backlog_count()
    assert out["export"]["stale"] is False
    assert out["export"]["source"] == str(src)
    # backlog_count ne lit pas filtres.yaml : les deux notes sont comptées
    assert out["total_to_copy"] == 2


def test_scan_rapporte_la_sonde_et_filtre_les_dossiers_ignores(export):
    out = notes.scan()
    assert out["export"]["stale"] is False
    rels = {it["rel"] for it in out["to_copy"]}
    assert rels == {"Journal/a.md"}
    assert any(s["reason"] for s in out["skipped"])


def test_scan_sans_export_echoue_proprement(tmp_path, monkeypatch):
    """Sans dossier d'export, `scan` refuse (require_paths) plutôt que de
    retourner un backlog vide trompeur."""
    monkeypatch.setattr(notes, "NOTES_DIR", tmp_path / "absent")
    with pytest.raises(SystemExit):
        notes.scan()


def test_dossiers_ignores_sur_chemin_relatif_pas_absolu(tmp_path):
    """Régression : l'export vit sous `~/Archives/Notes/` et `Archives` est un
    dossier ignoré. Le filtre doit regarder les segments relatifs à la racine
    de l'export, sinon toutes les notes sont rejetées."""
    from connaissance.core.filtres import Filtres

    root = tmp_path / "Archives" / "Notes"
    note = root / "Journal" / "a.md"
    _note(note)
    f = Filtres()
    assert "Archives" in f.notes_config.get("dossiers_ignores", [])
    # Sans root : le chemin absolu contient « Archives » → rejeté (comportement
    # hérité, documenté).
    ok, reason = f.filter_note(note, content=note.read_text())
    assert (ok, reason) == (False, "dossier_ignore:Archives")
    # Avec root : seul « Journal/a.md » est examiné → accepté.
    ok, reason = f.filter_note(note, content=note.read_text(), root=root)
    assert ok is True
    # Et un vrai sous-dossier ignoré de l'export reste rejeté.
    vieille = root / "Archives" / "b.md"
    _note(vieille)
    ok, reason = f.filter_note(vieille, content=vieille.read_text(), root=root)
    assert (ok, reason) == (False, "dossier_ignore:Archives")
