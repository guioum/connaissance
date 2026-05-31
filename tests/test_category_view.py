"""Tests de documents.category_view — vue par catégorie en raccourcis (symlinks).

Portable : on monkeypatche les racines vers des dossiers tmp (la fonction lit
les globals du module), donc aucune dépendance à une vraie base ~/Connaissance.
"""
from connaissance.commands import documents as doc


def _setup(tmp_path, monkeypatch):
    docs_root = tmp_path / "Documents"
    resumes = tmp_path / "Connaissance" / "Résumés" / "Documents"
    # un document source + son résumé portant la catégorie
    (docs_root / "organismes" / "banque-nationale").mkdir(parents=True)
    src = docs_root / "organismes" / "banque-nationale" / "2025-01-10 releve.pdf"
    src.write_bytes(b"%PDF-1.4")
    (resumes / "organismes" / "banque-nationale").mkdir(parents=True)
    (resumes / "organismes" / "banque-nationale" / "2025-01-10 releve.md").write_text(
        "---\ncategory: banque\n---\nrésumé", encoding="utf-8")
    monkeypatch.setattr(doc, "DOCUMENTS_DIR", docs_root)
    monkeypatch.setattr(doc, "RESUMES_DOCS_DIR", resumes)
    return docs_root, src


def test_dry_run_reports_breakdown(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    res = doc.category_view()
    assert res["categories"] == {"banque": 1}
    assert res["total"] == 1
    assert res["applied"] is False
    assert res["no_category"] == 0 and res["missing_source"] == 0


def test_apply_creates_symlink_to_real_source(tmp_path, monkeypatch):
    docs_root, src = _setup(tmp_path, monkeypatch)
    res = doc.category_view(apply=True)
    assert res["links_created"] == 1
    link = docs_root / "- Par catégorie" / "banque" / "[banque-nationale] 2025-01-10 releve.pdf"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()   # le raccourci pointe le vrai fichier


def test_clear_removes_view_only(tmp_path, monkeypatch):
    docs_root, src = _setup(tmp_path, monkeypatch)
    doc.category_view(apply=True)
    res = doc.category_view(clear=True)
    assert res["cleared"] is True and res["existed"] is True
    assert not (docs_root / "- Par catégorie").exists()
    assert src.exists()   # l'original est intact


def test_resume_without_category_is_counted(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (doc.RESUMES_DOCS_DIR / "organismes" / "x").mkdir(parents=True)
    (doc.RESUMES_DOCS_DIR / "organismes" / "x" / "2025-02-02 note.md").write_text(
        "---\ntitle: sans catégorie\n---\n", encoding="utf-8")
    res = doc.category_view()
    assert res["no_category"] == 1
