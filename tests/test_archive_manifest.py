"""audit archive-non-documents --from-manifest : tri explicite via ledger."""
import json

from connaissance.commands import audit_archive as A
import connaissance.core.ledger as Lmod


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    classer = docs / "Classer" / "2020" / "Archive 2020"
    (classer / "repo").mkdir(parents=True)
    (classer / "repo" / "a.php").write_text("<?php", encoding="utf-8")
    (classer / "repo" / "sub").mkdir()
    (classer / "repo" / "sub" / "b.js").write_text("js", encoding="utf-8")
    (classer / "junk.app").mkdir()
    (classer / "junk.app" / "bin").write_bytes(b"x")
    (classer / "garde.txt").write_text("reste", encoding="utf-8")
    archives = tmp_path / "Archives"
    monkeypatch.setattr(A, "DOCUMENTS_LOCAL", docs)
    monkeypatch.setattr(A, "require_paths", lambda *a, **k: None)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path / "Connaissance")
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps({
        "archives_root": str(archives),
        "entries": [
            {"action": "ARCHIVER", "source": "Classer/2020/Archive 2020/repo",
             "dest": "Code/2020 repo"},
            {"action": "POUBELLE", "source": "Classer/2020/Archive 2020/junk.app",
             "dest": None},
            {"action": "GARDER", "source": "Classer/2020/Archive 2020/garde.txt",
             "dest": None},
        ]}), encoding="utf-8")
    return docs, archives, manifest, classer


def test_dry_run_ne_bouge_rien(tmp_path, monkeypatch, tracking_db):
    docs, archives, manifest, classer = _setup(tmp_path, monkeypatch)
    res = A.apply_manifest(str(manifest), dry_run=True, db=tracking_db)
    assert res["dry_run"] and res["archived"] == 2 and res["trashed"] == 1
    assert (classer / "repo" / "a.php").exists() and not archives.exists()
    assert tracking_db.ledger_runs() == []


def test_apply_archive_corbeille_index_elagage(tmp_path, monkeypatch, tracking_db):
    docs, archives, manifest, classer = _setup(tmp_path, monkeypatch)
    res = A.apply_manifest(str(manifest), dry_run=False, db=tracking_db)
    assert res["archived"] == 2 and res["trashed"] == 1 and res["errors"] == []
    # structure relative préservée sous la destination
    assert (archives / "Code" / "2020 repo" / "a.php").read_text() == "<?php"
    assert (archives / "Code" / "2020 repo" / "sub" / "b.js").exists()
    # corbeille ledger, pas de suppression
    assert not (classer / "junk.app").exists()
    trash = tmp_path / "Connaissance" / ".trash"
    assert list(trash.rglob("bin"))
    # GARDER intact, dossier source élagué (repo/ vide disparu)
    assert (classer / "garde.txt").exists()
    assert not (classer / "repo").exists()
    # index de provenance + un run par famille
    idx = (archives / "_index.md").read_text(encoding="utf-8")
    assert "Classer/2020/Archive 2020/repo" in idx and "Code/2020 repo" in idx
    fams = {r["famille"]: r["ledger_run"] for r in res["list"]}
    assert set(fams) == {"Code", "corbeille"} and fams["Code"] != fams["corbeille"]
    assert len(tracking_db.ledger_runs()) == 2


def test_refuse_d_ecraser(tmp_path, monkeypatch, tracking_db):
    docs, archives, manifest, classer = _setup(tmp_path, monkeypatch)
    (archives / "Code" / "2020 repo").mkdir(parents=True)
    (archives / "Code" / "2020 repo" / "a.php").write_text("déjà là", encoding="utf-8")
    res = A.apply_manifest(str(manifest), dry_run=False, db=tracking_db)
    assert any("destination existe" in e["error"] for e in res["errors"])
    assert (archives / "Code" / "2020 repo" / "a.php").read_text() == "déjà là"
    assert (classer / "repo" / "a.php").exists()    # la source n'a pas bougé
