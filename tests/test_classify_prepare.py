"""Tests de la brique 2 Phase C (commands/classify.prepare) — offline."""
import json
from pathlib import Path

from connaissance.commands import classify as CMD


def _signals_file(tmp_path):
    sigs = {"documents": [{
        "rel": "Classer/2025-09-29 - Banque Nationale - Confirmation paiement.pdf",
        "type": "pdf", "origin_folder": "Municipalité", "type_hint": "paiement",
        "name_keywords": ["banque", "paiement"],
        "dates": {"from_name": "2025-09-29", "metadata": None,
                  "filesystem_created": None, "filesystem_modified": None},
        "title_meta": None,
        "summary": {"keywords": ["taxes", "paiement"],
                    "sentences": ["Confirmation de paiement des taxes."],
                    "entities": {"amounts": ["1 200 $"], "dates": ["2025-09-29"],
                                 "refs": []}, "chars": 80},
        "born_digital": True, "text_source": "pdf_embedded", "pdf_available": True,
    }]}
    p = tmp_path / "sigs.json"
    p.write_text(json.dumps(sigs), encoding="utf-8")
    return p


def test_prepare_builds_batch_requests(tmp_path, monkeypatch, tracking_db):
    docroot = tmp_path / "Documents"
    (docroot / "organismes" / "banque-nationale").mkdir(parents=True)
    (docroot / "personnes" / "melanie-bazin").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)

    # db de test (registre `entities` vide → known_entities retombe sur les
    # dossiers ci-dessus, isolé de la vraie base).
    res = CMD.prepare(from_signals=str(_signals_file(tmp_path)), db=tracking_db)
    assert res["total"] == 1
    assert res["known_entities_count"] == 2

    reqs = json.loads(Path(res["transit_file"]).read_text(encoding="utf-8"))["requests"]
    req = reqs[0]
    assert req["custom_id"].startswith("cls_")
    assert req["model"] == CMD.DEFAULT_MODEL
    assert req["max_tokens"] == CMD.DEFAULT_MAX_TOKENS
    # Le user porte le hint ; la liste d'entités connues est dans le SYSTEM
    # (cacheable). Le system impose aussi la sortie JSON + taxonomie canonique.
    assert "Proposition heuristique" in req["user"]
    assert "Banque Nationale" in req["system"]
    assert "JSON" in req["system"] and "impots" in req["system"]


def test_prepare_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", tmp_path / "Documents")
    sf = _signals_file(tmp_path)
    res = CMD.prepare(from_signals=str(sf), limit=0)
    assert res["total"] == 0


def test_canonicalize_category_maps_leaks():
    # Synonymes / fuites → canonique ; thèmes fins → divers ; inconnu → None.
    # `finances` est ambigu (banque vs impots) → non mappé → None (mis en revue).
    assert CMD.canonicalize_category("finances") is None
    assert CMD.canonicalize_category("santé") == "sante"      # accent NFC
    assert CMD.canonicalize_category("voyages") == "transport"
    assert CMD.canonicalize_category("cuisine") == "divers"
    assert CMD.canonicalize_category("travail") == "professionnel"
    assert CMD.canonicalize_category("professionnel") == "professionnel"
    assert CMD.canonicalize_category("Banque") == "banque"    # casse
    assert CMD.canonicalize_category("inexistant") is None
    assert CMD.canonicalize_category(None) is None


def test_prepare_injects_excerpt_in_prompt(tmp_path, monkeypatch):
    # Depuis v2.45, le prompt envoie l'EXTRAIT DU TEXTE BRUT (et non plus des
    # mots-clés par fréquence) comme signal premier au classifieur.
    docroot = tmp_path / "Documents"
    (docroot / "personnes" / "guillaume-monteillet").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)
    sigs = {"documents": [{
        "rel": "x/doc.pdf", "type": "pdf", "origin_folder": None,
        "type_hint": None, "name_keywords": [],
        "dates": {"from_name": None, "metadata": None,
                  "filesystem_created": None, "filesystem_modified": None},
        "title_meta": None,
        "excerpt": "Relevé de compte courant Banque Nationale solde 1 234,56 $",
        "summary": {"keywords": [], "sentences": [], "entities": {}}}]}
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps(sigs), encoding="utf-8")
    res = CMD.prepare(from_signals=str(sf))
    req = json.loads(Path(res["transit_file"]).read_text(encoding="utf-8"))["requests"][0]
    assert "Extrait du document" in req["user"]
    assert "Relevé de compte courant Banque Nationale" in req["user"]


def test_prepare_system_carries_shared_rules(tmp_path, monkeypatch):
    # Le bloc partagé pré/final (discipline d'entité + règles de catégorie +
    # entités connues) doit figurer dans le système du prompt de classement.
    docroot = tmp_path / "Documents"
    (docroot / "personnes" / "guillaume-monteillet").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)
    sigs = {"documents": [{
        "rel": "x/doc.pdf", "type": "pdf", "origin_folder": None,
        "type_hint": None, "name_keywords": [],
        "dates": {"from_name": None, "metadata": None,
                  "filesystem_created": None, "filesystem_modified": None},
        "title_meta": None, "excerpt": "",
        "summary": {"keywords": [], "sentences": [], "entities": {}}}]}
    sf = tmp_path / "s.json"
    sf.write_text(json.dumps(sigs), encoding="utf-8")
    res = CMD.prepare(from_signals=str(sf))
    req = json.loads(Path(res["transit_file"]).read_text(encoding="utf-8"))["requests"][0]
    assert "Discipline d'entité" in req["system"]
    assert "BNC » = Banque Nationale" in req["system"]
    # Règles de catégorie communes (mêmes valeurs + priorité dans les 2 passes).
    assert "Catégorie — valeurs autorisées" in req["system"]
    assert "Priorité" in req["system"]


def test_known_entities_deslug(tmp_path, monkeypatch, tracking_db):
    # Registre vide → repli sur les dossiers rangés (dé-sluggés).
    docroot = tmp_path / "Documents"
    (docroot / "organismes" / "air-transat").mkdir(parents=True)
    (docroot / "organismes" / "banque-nationale").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)
    names = CMD.known_entities(tracking_db)
    assert "Air Transat" in names and "Banque Nationale" in names


def test_known_entities_reads_registry(tracking_db):
    # Avec un registre peuplé : known_entities renvoie canonique + aliases.
    tracking_db.upsert_entity("organismes", "banque-nationale", "Banque Nationale",
                              ["BNC", "Banque Nationale du Canada"])
    tracking_db.upsert_entity("personnes", "guillaume-monteillet", "Guillaume Monteillet")
    names = CMD.known_entities(tracking_db)
    assert any("Banque Nationale (aussi : BNC" in n for n in names)
    assert "Guillaume Monteillet" in names


def test_resolve_entity_by_alias(tracking_db):
    tracking_db.upsert_entity("organismes", "banque-nationale", "Banque Nationale",
                              ["BNC", "Banque Nationale du Canada"])
    # match par alias (slug) → canonique
    r = tracking_db.resolve_entity("bnc")
    assert r and r["name"] == "Banque Nationale"
    r2 = tracking_db.resolve_entity("Banque Nationale du Canada")
    assert r2 and r2["slug"] == "banque-nationale"
    assert tracking_db.resolve_entity("Inconnu SARL") is None
