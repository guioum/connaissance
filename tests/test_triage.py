"""Tests du triage A/B/C/D (commands/triage.py) — heuristiques + conteneurs."""
from connaissance.commands import triage as T


def test_groups_markers_and_extensions(tmp_path, monkeypatch):
    root = tmp_path / "Documents"

    # Projet détecté par un dossier marqueur (.git) — SANS marqueur fichier.
    # Son README.md ne doit PAS fuir dans le groupe A.
    repo = root / "Classer" / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    (repo / "main.py").write_text("x", encoding="utf-8")

    # Fichiers en vrac : taxonomie enrichie.
    (root / "facture.pdf").write_bytes(b"%PDF")
    (root / "livre.epub").write_bytes(b"x")    # epub → document (A)
    (root / "page.html").write_text("x", encoding="utf-8")   # html → code (D)
    (root / "note.enex").write_text("x", encoding="utf-8")   # export → B
    (root / "photo.jpg").write_bytes(b"x")
    (root / "inconnu.xyz").write_text("x", encoding="utf-8")

    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()

    assert len(res["containers"]["repos_code"]) == 1   # détecté via .git
    g = res["groups"]
    assert g.get("A_documents") == 2   # pdf + epub (README.md absorbé dans le repo)
    assert g.get("C_media") == 1       # jpg
    assert g.get("B_exports") == 1     # enex
    assert g.get("D_code") == 3        # repo (README.md + main.py) + html
    assert g.get("autre") == 1         # xyz


def test_marker_dir_claude_detects_project(tmp_path, monkeypatch):
    # Cas monach-budget : un dossier-projet avec .claude (pas de marqueur fichier).
    root = tmp_path / "Documents"
    proj = root / "- Protégés" / "monach-budget"
    (proj / ".claude").mkdir(parents=True)
    (proj / "README.md").write_text("x", encoding="utf-8")
    (proj / "exports").mkdir()
    (proj / "exports" / "tx.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    # Tout le projet est en bloc dans D ; rien ne fuit en A.
    assert res["groups"].get("A_documents", 0) == 0
    assert len(res["containers"]["repos_code"]) == 1


def test_export_folder_is_walked_not_opaque(tmp_path, monkeypatch):
    # Un vieux Google Drive : on le PARCOURT, ses vrais docs remontent en A.
    root = tmp_path / "Documents"
    drive = root / "Classer" / "Takeout" / "Drive"
    drive.mkdir(parents=True)
    (drive / "vieux-contrat.pdf").write_bytes(b"%PDF")
    (drive / "carnet.enex").write_text("x", encoding="utf-8")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["groups"].get("A_documents") == 1   # le PDF du Drive remonte en A
    assert res["groups"].get("B_exports") == 1     # le .enex reste B


def test_triage_skips_already_classified_top_dirs(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    (root / "organismes" / "banque").mkdir(parents=True)
    (root / "organismes" / "banque" / "x.pdf").write_bytes(b"%PDF")
    (root / "facture.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["total_files"] == 1   # organismes/ déjà classé → ignoré
