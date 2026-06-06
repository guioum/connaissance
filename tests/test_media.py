"""Rangement des médias par date (commands/media)."""
import json

import connaissance.core.ledger as Lmod
from connaissance.commands import media as M


def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(M, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(M, "require_paths", lambda *a, **k: None)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)
    return docs


def test_plan_groups_by_date_in_name(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "2023-07-15 plage.jpg").write_bytes(b"img")
    (docs / "doc.pdf").write_bytes(b"notmedia")        # ignoré (pas un média)
    res = M.plan()
    assert res["total"] == 1
    e = res["entries"][0]
    assert e["dest"] == "- Médias/2023/07/2023-07-15 plage.jpg"


def test_plan_skips_view_and_container_dirs(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "- Médias" / "2020" / "01").mkdir(parents=True)
    (docs / "- Médias" / "2020" / "01" / "x.jpg").write_bytes(b"a")  # déjà rangé
    (docs / "node_modules").mkdir()
    (docs / "node_modules" / "logo.png").write_bytes(b"b")           # conteneur
    res = M.plan()
    assert res["total"] == 0


def test_apply_moves_via_ledger(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    (docs / "2022-03-09 photo.png").write_bytes(b"img")
    mf = tmp_path / "m.json"
    mf.write_text(json.dumps({"entries": [
        {"source": "2022-03-09 photo.png",
         "dest": "- Médias/2022/03/2022-03-09 photo.png"}]}))
    dry = M.apply(str(mf), dry_run=True, db=tracking_db)
    assert dry["would_move"] == 1 and (docs / "2022-03-09 photo.png").exists()
    res = M.apply(str(mf), dry_run=False, db=tracking_db)
    assert res["moved"] == 1 and "ledger_run" in res
    assert (docs / "- Médias/2022/03/2022-03-09 photo.png").exists()
    assert not (docs / "2022-03-09 photo.png").exists()


def test_undated_media_go_to_zero_bucket(tmp_path, monkeypatch, tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    f = docs / "sansdate.jpg"
    f.write_bytes(b"img")
    res = M.plan()
    # un média sans date dans le nom retombe sur la date filesystem (année courante
    # du fichier tmp) — le dest commence par '- Médias/' avec une année plausible.
    assert res["total"] == 1
    assert res["entries"][0]["dest"].startswith("- Médias/")
