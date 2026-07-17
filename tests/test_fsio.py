"""Tests de core/fsio.py : écriture atomique (tmp + os.replace)."""
import os
import stat

import pytest

from connaissance.core.fsio import atomic_write_text


def _tmp_residues(directory):
    """Fichiers temporaires ``.{nom}.*.tmp`` restants dans un dossier."""
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_ecrit_le_contenu(tmp_path):
    target = tmp_path / "note.md"
    atomic_write_text(target, "contenu final\n")
    assert target.read_text(encoding="utf-8") == "contenu final\n"


def test_cree_les_parents_manquants(tmp_path):
    target = tmp_path / "a" / "b" / "note.md"
    atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_remplace_un_fichier_existant(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("ancien", encoding="utf-8")
    atomic_write_text(target, "nouveau")
    assert target.read_text(encoding="utf-8") == "nouveau"


def test_preserve_le_mode_existant(tmp_path):
    target = tmp_path / "prive.md"
    target.write_text("secret", encoding="utf-8")
    os.chmod(target, 0o600)
    atomic_write_text(target, "secret v2")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "secret v2"


def test_pas_de_tmp_residuel_apres_succes(tmp_path):
    target = tmp_path / "note.md"
    atomic_write_text(target, "ok")
    assert _tmp_residues(tmp_path) == []


def test_pas_de_tmp_residuel_apres_exception(tmp_path, monkeypatch):
    """Un échec en cours d'écriture (fsync simulé) nettoie le .tmp et laisse
    le fichier cible intact."""
    target = tmp_path / "note.md"
    target.write_text("version intacte", encoding="utf-8")

    def _fsync_rate(fd):
        raise OSError("disque plein (simulé)")

    monkeypatch.setattr(os, "fsync", _fsync_rate)
    with pytest.raises(OSError, match="disque plein"):
        atomic_write_text(target, "version jamais visible")
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == "version intacte"
    assert _tmp_residues(tmp_path) == []
