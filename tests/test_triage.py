"""Tests du triage A/B/C/D (commands/triage.py) — heuristiques + conteneurs."""
from connaissance.commands import triage as T


def test_triage_groups_and_containers(tmp_path, monkeypatch):
    root = tmp_path / "Documents"

    # Repo de code (marqueur composer.json) → unité, groupe D.
    repo = root / "Classer" / "monrepo"
    (repo / "lib").mkdir(parents=True)
    (repo / "composer.json").write_text("{}", encoding="utf-8")
    (repo / "index.php").write_text("<?php", encoding="utf-8")
    (repo / "lib" / "a.js").write_text("x", encoding="utf-8")

    # Dossier d'export (par le nom) → unité, groupe B.
    exp = root / "Takeout"
    exp.mkdir()
    (exp / "data.json").write_text("{}", encoding="utf-8")

    # Fichiers en vrac.
    (root / "facture.pdf").write_bytes(b"%PDF")
    (root / "contrat.docx").write_bytes(b"x")
    (root / "photo.jpg").write_bytes(b"x")
    (root / "inconnu.xyz").write_text("x", encoding="utf-8")

    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()

    # Le repo est compté en bloc (3 fichiers), pas déroulé.
    repos = res["containers"]["repos_code"]
    assert len(repos) == 1 and repos[0]["files"] == 3
    assert len(res["containers"]["exports"]) == 1

    g = res["groups"]
    assert g.get("A_documents") == 2   # pdf + docx
    assert g.get("C_media") == 1       # jpg
    assert g.get("autre") == 1         # xyz
    assert g.get("D_code") == 3        # contenu du repo
    assert g.get("B_exports") == 1     # contenu de Takeout
    assert res["total_files"] == 8


def test_triage_skips_already_classified_top_dirs(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    (root / "organismes" / "banque").mkdir(parents=True)
    (root / "organismes" / "banque" / "x.pdf").write_bytes(b"%PDF")
    (root / "facture.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    # organismes/ est déjà classé → ignoré ; seul le pdf racine est compté.
    assert res["total_files"] == 1
