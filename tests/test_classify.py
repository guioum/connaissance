"""Tests du pré-classement heuristique Phase C (core/classify.py)."""
from connaissance.core import classify as C


def _sig(rel, **kw):
    base = {"rel": rel, "type": "pdf", "origin_folder": None,
            "type_hint": None, "name_keywords": [],
            "dates": {"from_name": None, "metadata": None,
                      "filesystem_created": None, "filesystem_modified": None},
            "title_meta": None, "summary": {"keywords": []}}
    base.update(kw)
    return base


def test_well_named_document_high_confidence():
    s = _sig(
        "Classer/2026/Maison2/Municipalité/"
        "2025-09-29 - Banque Nationale - Confirmation paiement - Taxes scolaires.pdf",
        origin_folder="Municipalité",
        dates={"from_name": "2025-09-29", "metadata": None,
               "filesystem_created": None, "filesystem_modified": None},
    )
    r = C.classify(s)
    assert r["date"] == "2025-09-29"
    assert r["entity"] == "Banque Nationale"
    assert r["entity_type"] == "organismes"
    assert r["category"] in ("paiement", "taxes")
    assert r["sujet"] == "maison"
    assert "scolaires" in r["title"].lower()
    assert r["confidence"] == "high"


def test_known_entity_alignment():
    r = C.classify(_sig("x/BNC.pdf"), known_entities=["BNC", "Hydro-Québec"])
    assert r["entity"] == "BNC" and r["entity_known"] is True


def test_entity_strips_type_words():
    # « BNC Sommaire Relevé de compte » → entité « BNC », titre sans « BNC ».
    r = C.classify(_sig("x/BNC Sommaire Relevé de compte.pdf"))
    assert r["entity"] == "BNC"
    assert "bnc" not in r["title"].lower()


def test_entity_from_folder_when_scanner_name():
    # Nom généré par scanner → entité prise du dossier (mot-de-type retiré).
    s = _sig("x/Vidéotron Facture/scanner_2024-03-12.pdf",
             origin_folder="Vidéotron Facture")
    r = C.classify(s)
    assert r["entity"] == "Vidéotron"


def test_entity_from_folder_strips_year_range():
    s = _sig("x/Payes Québecor 2015-2016/scan001.pdf",
             origin_folder="Payes Québecor 2015-2016")
    r = C.classify(s)
    assert r["entity"] == "Québecor"


def test_person_entity_type():
    s = _sig("divers/Mélanie Bazin - autorisation.pdf")
    r = C.classify(s)
    assert r["entity"] == "Mélanie Bazin"
    assert r["entity_type"] == "personnes"


def test_date_priority_name_over_filesystem():
    s = _sig("x/2020-01-01 truc.pdf",
             dates={"from_name": "2020-01-01", "metadata": "2024-05-05T00:00:00",
                    "filesystem_created": "2026-01-01T00:00:00",
                    "filesystem_modified": None})
    r = C.classify(s)
    assert r["date"] == "2020-01-01" and r["date_source"] == "name"


def test_low_confidence_messy_name():
    s = _sig("Classer/vrac/scan0001.pdf",
             dates={"from_name": None, "metadata": None,
                    "filesystem_created": None, "filesystem_modified": None})
    r = C.classify(s)
    assert r["confidence"] == "low"


def test_category_from_keywords_when_no_segments():
    s = _sig("x/document.pdf",
             summary={"keywords": ["hypothèque", "prêt", "taux"]})
    r = C.classify(s)
    assert r["category"] == "hypotheque"


def test_sujet_from_origin_folder():
    assert C.guess_sujet("Travaux et rénovations") == "maison"
    assert C.guess_sujet("2024 Impôts") == "impots"
    assert C.guess_sujet(None) is None
