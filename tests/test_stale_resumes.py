"""Péremption des résumés par hash de contenu (pas mtime seul)."""
import time

from connaissance.core.frontmatter import body_sha256
from connaissance.commands import pipeline as P


def test_body_sha256_insensible_au_frontmatter():
    a = "---\ntitle: v1\nsource_mtime: 1\n---\ncorps identique\n"
    b = "---\ntitle: v2\nsource_mtime: 999\nextra: oui\n---\ncorps identique"
    c = "---\ntitle: v1\n---\ncorps DIFFÉRENT\n"
    assert body_sha256(a) == body_sha256(b)     # métadonnées ≠ contenu
    assert body_sha256(a) != body_sha256(c)


def _seed_pair(tmp_path, tracking_db, monkeypatch, *, stamp, trans_body):
    trans = tmp_path / "Connaissance/Transcriptions/Documents/a.md"
    resume = tmp_path / "Connaissance/Résumés/Documents/a.md"
    trans.parent.mkdir(parents=True, exist_ok=True)
    resume.parent.mkdir(parents=True, exist_ok=True)
    trans.write_text(f"---\nsource: a.pdf\n---\n{trans_body}", encoding="utf-8")
    fm_stamp = f"source_content_hash: {stamp}\n" if stamp else ""
    resume.write_text(f"---\ntype: document\n{fm_stamp}---\nrésumé",
                      encoding="utf-8")
    # files : résumé plus VIEUX que la transcription → candidat mtime
    now = time.time()
    tracking_db.register_file("Connaissance/Résumés/Documents/a.md", "resume",
                              source_path="Connaissance/Transcriptions/Documents/a.md",
                              mtime=now - 1000)
    tracking_db.register_file("Connaissance/Transcriptions/Documents/a.md",
                              "transcription", mtime=now)
    from connaissance.core import tracking as T
    monkeypatch.setattr(P, "resolve_file_path", None, raising=False)
    monkeypatch.setattr(T, "BASE_PATH", tmp_path)
    return trans, resume


def test_perime_si_corps_change(tmp_path, tracking_db, monkeypatch):
    trans, _ = _seed_pair(tmp_path, tracking_db, monkeypatch,
                          stamp=body_sha256("---\nx: 1\n---\nANCIEN corps"),
                          trans_body="NOUVEAU corps")
    res = P.resumes_perimes(tracking_db)
    assert res["total"] == 1 and res["mtime_only_ignores"] == 0


def test_pas_perime_si_seul_frontmatter_a_change(tmp_path, tracking_db, monkeypatch):
    body = "corps stable"
    trans, _ = _seed_pair(tmp_path, tracking_db, monkeypatch,
                          stamp=body_sha256(f"---\nvieux: fm\n---\n{body}"),
                          trans_body=body)
    res = P.resumes_perimes(tracking_db)
    assert res["total"] == 0 and res["mtime_only_ignores"] == 1


def test_sans_estampille_repli_mtime(tmp_path, tracking_db, monkeypatch):
    _seed_pair(tmp_path, tracking_db, monkeypatch, stamp=None,
               trans_body="peu importe")
    res = P.resumes_perimes(tracking_db)
    assert res["total"] == 1   # conservateur : pas de hash → mtime fait foi
