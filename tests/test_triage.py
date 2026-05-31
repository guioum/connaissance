"""Tests du triage A/B/C/D (commands/triage.py).

Modèle : les CONTENEURS (repos de code, paquets macOS) sont des UNITÉS, exclues
du décompte des groupes. ``groups`` ne compte que les fichiers EN VRAC.
"""
from connaissance.commands import triage as T


def test_loose_groups_exclude_container_internals(tmp_path, monkeypatch):
    root = tmp_path / "Documents"

    # Projet détecté par dossier marqueur (.git) → UNITÉ ; son README ne fuit pas.
    repo = root / "Classer" / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("x", encoding="utf-8")
    (repo / "main.py").write_text("x", encoding="utf-8")

    # Fichiers en vrac (taxonomie enrichie).
    (root / "facture.pdf").write_bytes(b"%PDF")
    (root / "livre.epub").write_bytes(b"x")    # epub → A
    (root / "page.html").write_text("x", encoding="utf-8")   # html → D
    (root / "note.enex").write_text("x", encoding="utf-8")   # export → B
    (root / "photo.jpg").write_bytes(b"x")
    (root / "inconnu.xyz").write_text("x", encoding="utf-8")

    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()

    assert len(res["containers"]["repos_code"]) == 1
    g = res["groups"]
    assert g.get("A_documents") == 2   # pdf + epub
    assert g.get("C_media") == 1
    assert g.get("B_exports") == 1
    assert g.get("D_code") == 1        # html seul (README/main.py dans le repo)
    assert g.get("autre") == 1
    assert res["loose_files"] == 6
    assert res["containers"]["files_total"] == 2   # README.md + main.py
    assert res["total_files"] == 8


def test_macos_bundle_collapsed_as_unit(tmp_path, monkeypatch):
    # Un budget YNAB (.ynab4) est un PAQUET : compté en 1 unité, pas déroulé.
    root = tmp_path / "Documents"
    ynab = root / "Classer" / "Budget 2015.ynab4" / "data" / "devices"
    ynab.mkdir(parents=True)
    (ynab / "A.ydevice").write_text("x", encoding="utf-8")
    (ynab / "B.ydevice").write_text("x", encoding="utf-8")
    (root / "facture.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    bundles = res["containers"]["bundles"]
    assert len(bundles) == 1 and bundles[0]["type"] == "ynab4"
    assert bundles[0]["files"] == 2
    assert res["groups"].get("B_exports", 0) == 0   # internes exclus du vrac
    assert res["groups"].get("A_documents") == 1     # seule la facture est en vrac


def test_density_archive_collapses_doc_poor_folder(tmp_path, monkeypatch):
    # Un dossier volumineux quasi sans documents (résidu de dump) → unité archive.
    root = tmp_path / "Documents"
    dump = root / "Classer" / "old-dump"
    dump.mkdir(parents=True)
    for i in range(120):
        (dump / f"f{i}.json").write_text("{}", encoding="utf-8")   # code, non-doc
    (dump / "egare.pdf").write_bytes(b"%PDF")                       # 1 doc égaré
    (root / "facture.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    archs = res["containers"]["archives"]
    assert len(archs) == 1
    assert archs[0]["files"] == 121 and archs[0]["docs"] == 1
    # tout le dump est avalé ; seule la facture racine reste en vrac
    assert res["groups"].get("D_code", 0) == 0
    assert res["groups"].get("A_documents") == 1


def test_small_doc_poor_folder_is_not_archived(tmp_path, monkeypatch):
    # Sous le seuil de taille : pas d'archive, les fichiers restent en vrac.
    root = tmp_path / "Documents"
    small = root / "Classer" / "petit"
    small.mkdir(parents=True)
    for i in range(5):
        (small / f"f{i}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["containers"]["archives"] == []
    assert res["groups"].get("D_code") == 5


def test_marker_dir_claude_detects_project(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    proj = root / "- Protégés" / "monach-budget"
    (proj / ".claude").mkdir(parents=True)
    (proj / "README.md").write_text("x", encoding="utf-8")
    (proj / "exports").mkdir()
    (proj / "exports" / "tx.csv").write_text("x", encoding="utf-8")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["groups"].get("A_documents", 0) == 0   # rien ne fuit
    assert len(res["containers"]["repos_code"]) == 1


def test_export_folder_is_walked_not_opaque(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    drive = root / "Classer" / "Takeout" / "Drive"
    drive.mkdir(parents=True)
    (drive / "vieux-contrat.pdf").write_bytes(b"%PDF")
    (drive / "carnet.enex").write_text("x", encoding="utf-8")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["groups"].get("A_documents") == 1   # le PDF du Drive remonte en A
    assert res["groups"].get("B_exports") == 1


def test_triage_skips_already_classified_top_dirs(tmp_path, monkeypatch):
    root = tmp_path / "Documents"
    (root / "organismes" / "banque").mkdir(parents=True)
    (root / "organismes" / "banque" / "x.pdf").write_bytes(b"%PDF")
    (root / "facture.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(T, "DOCUMENTS_DIR", root)
    res = T.triage()
    assert res["total_files"] == 1
