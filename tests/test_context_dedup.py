"""Dédup consciente du contexte : un doc cross-filé reste visible sous tous ses
sujets après déduplication (multi-sujet doc_sujets + éventail de la vue)."""
import connaissance.core.ledger as Lmod
from connaissance.commands import duplicates as D
from connaissance.commands import sujets as S
from connaissance.core import classify as C


# --- extraction dossier → sujet ---

def test_sujet_from_path_curated_rule():
    # une règle curatée l'emporte (sujet propre)
    assert C.sujet_from_path("Classer/2026/Finance/Impôts 2023/Perso/avis.pdf") == "impots"


def test_sujet_from_path_slug_fallback():
    # pas de règle → slug du dossier non générique le plus profond (générique
    # 'Preuves' sauté)
    s = C.sujet_from_path(
        "Classer/2026/Finance/BNC Contrat Marge de Crédit 2024/Preuves/x.pdf")
    assert s == "bnc-contrat-marge-de-credit-2024"


def test_sujet_from_path_none_when_all_generic():
    assert C.sujet_from_path("Classer/Documents/scan.pdf") is None


# --- doc_sujets / memberships ---

def test_add_and_membership_union(tracking_db):
    tracking_db.add_doc_sujets("a.pdf", ["impots", "bdc"], "dedup")
    tracking_db.upsert_classification("b.pdf", {"status": "auto", "sujet": "maison"})
    m = {(r["rel_path"], r["sujet"]) for r in tracking_db.sujet_memberships()}
    assert ("a.pdf", "impots") in m and ("a.pdf", "bdc") in m   # multi-sujet
    assert ("b.pdf", "maison") in m                             # compat fiche


def test_add_doc_sujets_idempotent(tracking_db):
    tracking_db.add_doc_sujets("a.pdf", ["impots"], "dedup")
    added = tracking_db.add_doc_sujets("a.pdf", ["impots"], "dedup")
    assert added == 0                                           # pas de doublon


# --- bout-en-bout : cross-filing préservé ---

def _setup(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    for mod in (D, S):
        monkeypatch.setattr(mod, "DOCUMENTS_DIR", docs)
        monkeypatch.setattr(mod, "require_paths", lambda *a, **k: None)
    monkeypatch.setattr(D, "documents_read_path", lambda p: p)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)
    return docs


def _seed(db, docs, rel, content):
    p = docs / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    db.get_or_compute_signals(p, rel, lambda _p: {"summary": {"sentences": [],
                                                              "keywords": []}})


def test_dedup_preserves_cross_filing_in_sujet_view(tmp_path, monkeypatch,
                                                    tracking_db):
    docs = _setup(tmp_path, monkeypatch)
    # même fichier (octets identiques) classé sous DEUX sujets
    _seed(tracking_db, docs, "bdc/avis.pdf", b"IDENTIQUE")
    _seed(tracking_db, docs, "impots-2025/avis.pdf", b"IDENTIQUE")

    plan = D.plan(db=tracking_db)
    assert plan["total"] == 1                      # 1 doublon (1 keeper, 1 trash)

    out = D.apply(plan["manifest_file"], dry_run=False, db=tracking_db)
    assert out["trashed"] == 1
    assert out["sujets_captured"] >= 2             # bdc + impots capturés

    # le fichier gardé porte les DEUX sujets
    sujets = {r["sujet"] for r in tracking_db.sujet_memberships()}
    assert "bdc" in sujets and "impots" in sujets

    # la vue éventaille : le fichier gardé apparaît sous bdc/ ET impots/
    S.view(apply=True, db=tracking_db)
    sv = docs / S.SUJETS_VIEW_NAME
    assert (sv / "bdc" / "avis.pdf").is_symlink()
    assert (sv / "impots" / "avis.pdf").is_symlink()
    # les deux pointent le même fichier physique (gardé)
    assert (sv / "bdc" / "avis.pdf").resolve() == (sv / "impots" / "avis.pdf").resolve()
