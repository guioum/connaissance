"""Dédup du registre d'entités : détection pure (core) + merge (commands)."""
import connaissance.commands.entities as CE
import connaissance.core.ledger as Lmod
from connaissance.core import entities as E


# --- détection pure (core/entities.py) ---

def test_pair_containment():
    sig = E.pair_signal("monteillet-conseil", "monteillet-conseil-inc")
    assert sig and "containment" in sig["reasons"]


def test_pair_token_overlap():
    sig = E.pair_signal("ville-de-montreal", "ville-montreal")
    assert sig and sig["score"] >= 0.5


def test_pair_edit_distance_typo():
    sig = E.pair_signal("desjardins", "desjardin")
    assert sig and any(r.startswith("edit=") for r in sig["reasons"])


def test_pair_acronym():
    sig = E.pair_signal("banque-nationale", "bnc")
    assert sig and "acronym" in sig["reasons"]


def test_pair_unrelated_none():
    assert E.pair_signal("orange", "hydro-quebec") is None


def test_pair_year_variants_not_merged():
    # Variantes annuelles = entités distinctes, jamais fusionnées.
    assert E.pair_signal("impots-2023", "impots-2024") is None
    assert E.pair_signal("objectifs-2025", "objectifs-2026") is None
    # mais une vraie différence + année reste détectable
    assert E.pair_signal("rapport-2024", "raport-2024") is not None


def test_find_candidates_groups_same_type_only():
    inv = [
        {"entity_type": "organismes", "entity_slug": "ville-de-montreal"},
        {"entity_type": "organismes", "entity_slug": "ville-montreal"},
        {"entity_type": "personnes", "entity_slug": "ville-montreal"},  # autre type
    ]
    pairs = E.find_candidates(inv)
    assert len(pairs) == 1
    assert pairs[0]["type"] == "organismes"


# --- merge (commands/entities.py) ---

def _setup(tmp_path, monkeypatch):
    syn = tmp_path / "Synthèse"
    res = tmp_path / "Résumés"
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(CE, "SYNTHESE", syn)
    monkeypatch.setattr(CE, "RESUMES", res)
    monkeypatch.setattr(CE, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(CE, "require_connaissance_root", lambda: None)
    monkeypatch.setattr(Lmod, "CONNAISSANCE_ROOT", tmp_path)
    return syn, res, docs


def _fiche(syn, etype, slug, name, aliases=None):
    d = syn / etype / slug
    d.mkdir(parents=True)
    al = "\n".join(f"  - {a}" for a in (aliases or []))
    fm = f"name: {name}\nslug: {slug}\n"
    if al:
        fm += f"aliases:\n{al}\n"
    (d / "fiche.md").write_text(f"---\n{fm}---\n\n# {name}\n", encoding="utf-8")


def test_merge_dry_run_reports(tmp_path, monkeypatch, tracking_db):
    syn, res, docs = _setup(tmp_path, monkeypatch)
    _fiche(syn, "organismes", "banque-nationale", "Banque Nationale")
    _fiche(syn, "organismes", "bnc", "BNC")
    tracking_db.upsert_classification("x.pdf", {
        "status": "auto", "entity": "BNC",
        "entity_type": "organismes", "entity_slug": "bnc"})
    res_d = CE.merge("organismes/bnc", "organismes/banque-nationale",
                     dry_run=True, db=tracking_db)
    assert res_d["dry_run"] and res_d["docs_to_reassign"] == 1
    assert "BNC" in res_d["aliases_to_add"]
    # rien n'a bougé
    assert (syn / "organismes" / "bnc" / "fiche.md").exists()


def test_merge_apply_reassigns_and_aliases(tmp_path, monkeypatch, tracking_db):
    syn, res, docs = _setup(tmp_path, monkeypatch)
    _fiche(syn, "organismes", "banque-nationale", "Banque Nationale")
    _fiche(syn, "organismes", "bnc", "BNC", aliases=["B.N.C."])
    tracking_db.upsert_classification("x.pdf", {
        "status": "auto", "entity": "BNC",
        "entity_type": "organismes", "entity_slug": "bnc"})
    # un résumé du perdant
    rd = res / "Documents" / "organismes" / "bnc"
    rd.mkdir(parents=True)
    (rd / "2024-01-01 relevé.md").write_text("résumé", encoding="utf-8")

    out = CE.merge("organismes/bnc", "organismes/banque-nationale",
                   dry_run=False, db=tracking_db)
    assert out["reassigned"] == 1 and "ledger_run" in out
    # DB repointée
    row = tracking_db.get_classification("x.pdf")
    assert row["entity_slug"] == "banque-nationale"
    # aliases ajoutés à la fiche gardée (nom + slug + alias du perdant)
    fm = CE._fiche_frontmatter("organismes", "banque-nationale")
    al = {str(a).lower() for a in fm["aliases"]}
    assert "bnc" in al and "b.n.c." in al
    # résumé déplacé + fiche perdante en corbeille
    assert (res / "Documents" / "organismes" / "banque-nationale"
            / "2024-01-01 relevé.md").exists()
    assert out["from_fiche_trashed"]


def test_merge_moves_raw_documents_and_cleans_dirs(tmp_path, monkeypatch,
                                                   tracking_db):
    # Le cas du bug : les documents bruts sous ~/Documents/<type>/<slug>/
    # doivent suivre la fusion, et le dossier perdant vidé doit disparaître.
    syn, res, docs = _setup(tmp_path, monkeypatch)
    _fiche(syn, "organismes", "banque-de-developpement-du-canada", "BDC")
    _fiche(syn, "organismes", "banque-developpement-canada", "Banque Dev Canada")
    # docs bruts du perdant
    loser = docs / "organismes" / "banque-developpement-canada"
    loser.mkdir(parents=True)
    (loser / "2024-09 paie.pdf").write_bytes(b"a")
    (loser / "2024-10 paie.pdf").write_bytes(b"b")
    (docs / "organismes" / "banque-de-developpement-du-canada").mkdir(parents=True)

    out = CE.merge("organismes/banque-developpement-canada",
                   "organismes/banque-de-developpement-du-canada",
                   dry_run=False, db=tracking_db)
    assert out["documents_moved"] == 2
    keeper = docs / "organismes" / "banque-de-developpement-du-canada"
    assert (keeper / "2024-09 paie.pdf").exists()
    assert (keeper / "2024-10 paie.pdf").exists()
    # dossier perdant vidé puis supprimé
    assert not loser.exists()


def test_rename_reaccents_dirs_db_and_fiche(tmp_path, monkeypatch, tracking_db):
    # Renommer revenu-quebec → revenu-québec : dossiers + DB + fiche + sujets.
    import connaissance.commands.entities as CE2
    syn, res, docs = _setup(tmp_path, monkeypatch)
    _fiche(syn, "organismes", "revenu-quebec", "Revenu Quebec")
    (docs / "organismes" / "revenu-quebec").mkdir(parents=True)
    (docs / "organismes" / "revenu-quebec" / "2024 avis.pdf").write_bytes(b"x")
    tracking_db.upsert_classification(
        "organismes/revenu-quebec/2024 avis.pdf",
        {"status": "auto", "entity": "Revenu Quebec",
         "entity_type": "organismes", "entity_slug": "revenu-quebec"})

    out = CE2.rename("organismes/revenu-quebec", "revenu-québec",
                     dry_run=False, db=tracking_db)
    # 2 fichiers déplacés : le doc brut + le fiche.md (Synthèse)
    assert out["files_moved"] == 2 and "ledger_run" in out
    # dossier renommé
    assert (docs / "organismes" / "revenu-québec" / "2024 avis.pdf").exists()
    assert not (docs / "organismes" / "revenu-quebec").exists()
    # DB : entity_slug + rel_path repointés
    row = tracking_db.get_classification("organismes/revenu-québec/2024 avis.pdf")
    assert row and row["entity_slug"] == "revenu-québec"
    # fiche slug mis à jour
    assert CE2._fiche_frontmatter("organismes", "revenu-québec")["slug"] == "revenu-québec"


def test_rename_dry_run_changes_nothing(tmp_path, monkeypatch, tracking_db):
    import connaissance.commands.entities as CE2
    syn, res, docs = _setup(tmp_path, monkeypatch)
    (docs / "organismes" / "cafe-x").mkdir(parents=True)
    (docs / "organismes" / "cafe-x" / "f.pdf").write_bytes(b"x")
    out = CE2.rename("organismes/cafe-x", "café-x", dry_run=True, db=tracking_db)
    assert out["dry_run"] and out["files_to_move"] == 1
    assert (docs / "organismes" / "cafe-x").exists()        # rien bougé
